"""Local HTTP control service for The Hand + The Eye.

The service is the bridge between the phone console and external clients
(the DSH plugin, scripts, a browser). It is deliberately small and
dependency-free: stdlib ``http.server``, bearer-token auth, one action at a
time, and an SSE event stream.

Routes:
    GET  /health                     no auth
    GET  /status                     auth
    GET  /screenshot[?format=png]    auth
    GET  /events                     auth (SSE)
    POST /tap /swipe /key /text      auth
    POST /screenshot                 auth (JSON, optional base64)
    GET  /ocr                        text list + device coords
    POST /find                       auth
    GET  /provision/devices          auth (BLE scan)
    POST /provision/arm              auth (start the hand's BLE window)
    POST /provision                  auth (start BLE provisioning)
    GET  /provision/status           auth
    POST /run                        auth
"""
from __future__ import annotations

import base64
import hmac
import io
import json
import queue
import secrets
import socketserver
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from .device import DeviceError
from .phone import Phone

if TYPE_CHECKING:
    from .vision.frame import Frame
    from .vision.ocr import OcrEngine

SERVICE_VERSION = "0.1.0"
MAX_BODY = 1_048_576
# Only a *dead* capture needs this age guard (a static screen keeps its last
# frame current); 60s leaves room for a slow adb reconnect before callers are
# told the eye is blind instead of being handed a frozen picture.
DEFAULT_MAX_FRAME_AGE_MS = 60_000.0
# Startup capture errors that mean "the device is not here", not "the service is
# broken": restarting the service cannot help, and grab() retries by itself.
DEVICE_HINT_NEEDLES = (
    "adb device",
    "unauthorized",
    "no devices",
    "no adb",
    "connect the phone",
    "no matching adb",
)


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str = "", status: int = 400) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}" if message else code)


class BusyError(ServiceError):
    def __init__(self, message: str = "phone is busy") -> None:
        super().__init__("BUSY", message, 409)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.add(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(channel)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for channel in subscribers:
            try:
                channel.put_nowait(event)
            except queue.Full:
                pass


class PhoneService:
    def __init__(
        self,
        phone: Phone,
        token: str = "",
        screenshot_dir: str | Path | None = None,
        engine: "OcrEngine | None" = None,
        lock_timeout: float = 1.0,
        platform: str | None = None,
        hand_optional: bool | None = None,
        max_frame_age_ms: float | None = None,
    ) -> None:
        self.phone = phone
        self.token = token
        self.platform = platform or getattr(phone, "platform", "android")
        # iOS eye-only debugging: the hand may not be plugged in, but capture
        # and OCR must still work. Tap/key/text keep requiring the hand.
        self.hand_optional = (
            self.platform == "ios" if hand_optional is None else bool(hand_optional)
        )
        self.hand_error: str | None = None
        # Last known hand liveness, so image endpoints don't re-probe a hand
        # that just went away (each probe pays a DNS/connect round trip).
        self.hand_ready: bool | None = None
        self.hand_checked_at = 0.0
        self.hand_probe_interval_s = 30.0
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self.engine = engine
        self.lock_timeout = lock_timeout
        self.max_frame_age_ms = (
            DEFAULT_MAX_FRAME_AGE_MS
            if max_frame_age_ms is None
            else max(0.0, float(max_frame_age_ms))
        )
        self.lock = threading.Lock()
        self.events = EventBus()
        self.started = time.time()
        self._provision_lock = threading.Lock()
        self._provision_thread: threading.Thread | None = None
        self._provision_state: dict[str, Any] = {
            "state": "idle",
            "ssid": "",
            "device": None,
            "started_at": 0.0,
            "result": None,
            "error": None,
        }

    def _ensure_ready(self, optional: bool = False) -> bool:
        """Clear a firmware FAULT caused by the 30s heartbeat timeout.

        Returns False instead of raising when ``optional`` is set and the hand
        is unreachable (iOS eye-only mode). Records the outcome so callers can
        skip repeated probes while the hand is known to be away.
        """
        try:
            self.phone.hello()
        except DeviceError as exc:
            self.hand_ready = False
            self.hand_checked_at = time.time()
            if not optional:
                raise
            self.hand_error = str(exc)
            return False
        self.hand_ready = True
        self.hand_checked_at = time.time()
        self.hand_error = None
        return True

    def _hand_recently_missing(self) -> bool:
        """Whether the hand failed its last probe within the retry window."""
        return (
            self.hand_ready is False
            and (time.time() - self.hand_checked_at) < self.hand_probe_interval_s
        )

    @contextmanager
    def _locked(self, timeout: float | None = None):
        if not self.lock.acquire(timeout=self.lock_timeout if timeout is None else timeout):
            raise BusyError()
        try:
            yield
        finally:
            self.lock.release()

    # ------------------------------------------------------------------ core
    def _cached_frames(self) -> int | None:
        hits = getattr(self.engine, "hits", None)
        if not isinstance(hits, dict):
            return None
        try:
            return int(hits.get("frame", 0))
        except (TypeError, ValueError):
            return None

    def _capture(self) -> dict[str, Any]:
        cached = self._cached_frames()
        source = getattr(self.phone, "source", None)
        if source is None:
            return {
                "name": type(self.phone).__name__,
                "stats": None,
                "ocr_cached_frames": cached,
            }
        stats = getattr(source, "stats", None)
        try:
            stream_stats = stats() if callable(stats) else None
        except Exception:  # noqa: BLE001 - diagnostics must never break /status
            stream_stats = None
        return {
            "name": getattr(source, "name", type(source).__name__),
            "stats": stream_stats,
            "ocr_cached_frames": cached,
        }

    # ------------------------------------------------------------- freshness
    @staticmethod
    def _frame_age_ms(frame: Any) -> float:
        ts = getattr(frame, "ts", None)
        if not isinstance(ts, (int, float)) or not ts:
            return 0.0
        return round(max(0.0, (time.time() - float(ts)) * 1000.0), 1)

    def _reject_stale(self, age_ms: float, capture: dict[str, Any] | None = None) -> None:
        """Refuse a picture that may no longer match the screen.

        A live capture child with an old frame is *not* a failure: Android only
        emits encoded frames when the screen changes, so a static screen keeps
        the last frame current. Only a dead child (or a missing first frame)
        makes the picture untrustworthy.
        """
        stats = capture.get("stats") if isinstance(capture, dict) else None
        if age_ms is None:
            raise ServiceError(
                "STALE_FRAME",
                "the capture stream has not produced its first frame yet. "
                "Check GET /status (capture.health), then retry.",
                503,
            )
        if isinstance(stats, dict) and stats.get("process_alive") is True:
            return
        limit = self.max_frame_age_ms
        if not limit or age_ms <= limit:
            return
        raise ServiceError(
            "STALE_FRAME",
            f"the capture stream stopped {age_ms / 1000:.1f}s ago "
            f"(limit {limit / 1000:.1f}s) and the last frame is not current. "
            "Check GET /status (capture.health), then restart the service or "
            "reconnect adb/scrcpy.",
            503,
        )

    def _capture_health(self, capture: dict[str, Any]) -> dict[str, Any]:
        """Classify capture health for /health, /status and the supervisor.

        ``degraded`` means "a restart can help" — never merely "the screen has
        not changed for a while". ``state`` adds the finer distinction the UI
        needs: ``not_ready`` (no first frame yet), ``error``, ``stopped``,
        ``static`` (live child, unchanged screen) or ``streaming``.
        """
        stats = capture.get("stats") if isinstance(capture, dict) else None
        if not isinstance(stats, dict):
            return {"degraded": False, "state": "unknown", "age_ms": None, "reason": None}
        # What /screenshot would hand back right now: the decoded frame, or the
        # adb fallback when the stream stalled and the fallback is fresher.
        age_ms = stats.get("effective_age_ms")
        if age_ms is None:
            age_ms = stats.get("age_ms")
        limit = self.max_frame_age_ms
        error = stats.get("error")
        process_alive = stats.get("process_alive")
        if process_alive is None:
            # Sources without a process flag (older/stub capture) only expose
            # the frame age, so keep the conservative age-only rule for them.
            if not limit or age_ms is None or age_ms <= limit:
                return {"degraded": False, "state": "unknown", "age_ms": age_ms, "reason": None}
            suffix = f"last frame is {age_ms / 1000:.1f}s old (limit {limit / 1000:.1f}s)"
            if error:
                reason = f"capture stream error: {str(error).splitlines()[0][:200]} ({suffix})"
            else:
                reason = suffix
            return {"degraded": True, "state": "stale", "age_ms": age_ms, "reason": reason}
        if age_ms is None:
            if error:
                text = str(error)
                detail = text.splitlines()[0][:200]
                if any(needle in text.lower() for needle in DEVICE_HINT_NEEDLES):
                    # The phone/adb is away. Restarting the service cannot fix
                    # that, and the capture retries by itself on the next grab.
                    return {
                        "degraded": False,
                        "state": "device_unavailable",
                        "age_ms": None,
                        "reason": f"capture device unavailable: {detail}",
                    }
                return {
                    "degraded": True,
                    "state": "error",
                    "age_ms": None,
                    "reason": f"capture stream error: {detail}",
                }
            # No frame yet: scrcpy starts lazily, and a fresh restart needs a
            # moment. Liveness is "not ready", not a reason to restart again.
            return {"degraded": False, "state": "not_ready", "age_ms": None, "reason": None}
        if process_alive is True:
            # The capture child is running: an old frame just means the screen
            # did not change, so this is a static screen, not a failure.
            state = "static" if limit and age_ms > limit else "streaming"
            return {"degraded": False, "state": state, "age_ms": age_ms, "reason": None}
        if error:
            return {
                "degraded": True,
                "state": "error",
                "age_ms": age_ms,
                "reason": f"capture stream error: {str(error).splitlines()[0][:200]}",
            }
        return {
            "degraded": True,
            "state": "stopped",
            "age_ms": age_ms,
            "reason": "capture process is not running: reconnecting adb/scrcpy",
        }

    def health(self) -> dict[str, Any]:
        """Liveness + capture freshness for the supervisor.

        Deliberately does not touch the hand or take the service lock: this is
        polled every few seconds and must stay cheap and non-blocking.
        """
        capture = self._capture()
        probe = self._capture_health(capture)
        return {
            "ok": True,
            "service": "phone",
            "version": SERVICE_VERSION,
            "platform": self.platform,
            "auth": bool(self.token),
            "capture": {"name": capture.get("name"), **probe},
        }

    def status(self) -> dict[str, Any]:
        with self._locked(0.5):
            ready = self._ensure_ready(optional=self.hand_optional)
            status = self.phone.status() if ready else {}
            screen = list(self.phone.screen)
            hand_error = self.hand_error
        capture = self._capture()
        capture["health"] = self._capture_health(capture)
        return {
            "ok": True,
            "platform": self.platform,
            "screen": screen,
            "status": status,
            "hand": {"ok": ready, "error": hand_error},
            "capture": capture,
            "uptime_s": round(time.time() - self.started, 1),
            "version": SERVICE_VERSION,
        }

    def _save_frame(self, frame: "Frame", prefix: str = "shot") -> Path | None:
        if self.screenshot_dir is None:
            return None
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        return frame.save(self.screenshot_dir / f"{prefix}-{stamp}.png")

    def _png_bytes(self, frame: "Frame", max_width: int | None = None) -> tuple[bytes, "Frame"]:
        from .vision.frame import Frame

        image = frame
        if max_width and frame.width > max_width:
            width = int(max_width)
            height = max(1, round(frame.height * width / frame.width))
            image = Frame.from_pil(
                frame.to_pil().resize((width, height)), source=frame.source
            )
        buffer = io.BytesIO()
        image.to_pil().save(buffer, format="PNG")
        return buffer.getvalue(), image

    def screenshot(
        self, include_base64: bool = False, max_width: int | None = None
    ) -> tuple[dict[str, Any], bytes]:
        with self._locked():
            # The eye is adb/scrcpy; the hand only matters for input. Probing
            # the hand here made screenshots wait on a hand reconnect (seconds)
            # for a picture that does not need it at all.
            frame = self.phone.grab()
            age_ms = self._frame_age_ms(frame)
            self._reject_stale(age_ms, self._capture())
            path = self._save_frame(frame)
            data, image = self._png_bytes(frame, max_width)
        payload: dict[str, Any] = {
            "ok": True,
            "path": str(path) if path else None,
            "width": image.width,
            "height": image.height,
            "frame_width": frame.width,
            "frame_height": frame.height,
            "frame_age_ms": age_ms,
            "bytes": len(data),
        }
        if include_base64:
            payload["base64"] = base64.b64encode(data).decode("ascii")
        self.events.publish(
            {
                "type": "screenshot",
                "path": payload["path"],
                "width": image.width,
                "height": image.height,
            }
        )
        return payload, data

    def _engine(self) -> "OcrEngine":
        if self.engine is None:
            from .vision.ocr import create_engine

            self.engine = create_engine()
        return self.engine

    def ocr_texts(
        self,
        min_score: float = 0.4,
        roi: Any = None,
    ) -> dict[str, Any]:
        """Every text on the current screen with its phone coordinates.

        Listing once is far cheaper than enumerating with repeated finds, which
        is what an agent otherwise does when it wants to "read" the screen.
        """
        from .scenario import parse_roi
        from .vision.locate import map_to_device
        from .vision.ocr import offset_items

        roi_box = parse_roi(roi) if roi is not None else None
        with self._locked():
            if not self._hand_recently_missing():
                self._ensure_ready(optional=True)
            frame = self.phone.grab()
            age_ms = self._frame_age_ms(frame)
            self._reject_stale(age_ms, self._capture())
            path = self._save_frame(frame, "ocr")
            ocr_frame = frame.crop(roi_box) if roi_box else frame
            engine = self._engine()
            items = engine.recognize(ocr_frame.image)
            if roi_box:
                items = offset_items(items, roi_box[0], roi_box[1])
            elapsed = engine.last_elapsed_ms
            screen = list(self.phone.screen)
            size = frame.size
            device = self.phone.screen
        texts = []
        for item in items:
            score = float(item.score)
            if score < float(min_score):
                continue
            center = item.center
            device_center = map_to_device(center, size, device)
            texts.append(
                {
                    "text": item.text,
                    "score": round(score, 4),
                    "center": [round(float(center[0]), 1), round(float(center[1]), 1)],
                    "device_center": list(device_center),
                }
            )
        return {
            "ok": True,
            "frame": list(size),
            "screen": screen,
            "screenshot_path": str(path) if path else None,
            "frame_age_ms": age_ms,
            "elapsed_ms": round(elapsed, 2),
            "items": texts,
        }

    def find(
        self,
        text: str,
        min_score: float = 0.6,
        mode: str = "contains",
        roi: Any = None,
        index: int = 0,
    ) -> dict[str, Any]:
        if not isinstance(text, str) or not text:
            raise ServiceError("BAD_REQUEST", "text must be a non-empty string")
        from .scenario import parse_roi
        from .vision.locate import TextLocator
        from .vision.ocr import offset_items

        roi_box = parse_roi(roi) if roi is not None else None
        with self._locked():
            # OCR only needs the eye. A fresh hand hello keeps `screen` (used to
            # map frame pixels to phone pixels) accurate, but must not block on
            # a hand that failed its last probe.
            if not self._hand_recently_missing():
                self._ensure_ready(optional=True)
            frame = self.phone.grab()
            age_ms = self._frame_age_ms(frame)
            self._reject_stale(age_ms, self._capture())
            path = self._save_frame(frame, "find")
            ocr_frame = frame.crop(roi_box) if roi_box else frame
            engine = self._engine()
            items = engine.recognize(ocr_frame.image)
            if roi_box:
                items = offset_items(items, roi_box[0], roi_box[1])
            elapsed = engine.last_elapsed_ms
            locator = TextLocator(
                screen=self.phone.screen, min_score=float(min_score), mode=str(mode)
            )
            match = locator.find(items, text, frame.size, index=int(index))
            screen = list(self.phone.screen)
        payload: dict[str, Any] = {
            "ok": True,
            "found": match is not None,
            "keyword": text,
            "frame": list(frame.size),
            "screen": screen,
            "screenshot_path": str(path) if path else None,
            "frame_age_ms": age_ms,
            "elapsed_ms": round(elapsed, 2),
            "items": len(items),
        }
        if match is not None:
            payload["match"] = match.as_dict()
        self.events.publish(
            {
                "type": "find",
                "keyword": text,
                "found": match is not None,
                "device_center": list(match.device_center) if match else None,
            }
        )
        return payload

    def tap(self, x: int, y: int, hold_ms: int | None = None) -> dict[str, Any]:
        with self._locked():
            self._ensure_ready()
            response = self.phone.tap(
                int(x), int(y), int(hold_ms) if hold_ms is not None else None
            )
        self.events.publish(
            {"type": "action", "action": "tap", "x": int(x), "y": int(y), "response": response}
        )
        return {"ok": True, "action": "tap", "response": response}

    def swipe(
        self, x1: int, y1: int, x2: int, y2: int, ms: int = 400
    ) -> dict[str, Any]:
        with self._locked():
            self._ensure_ready()
            response = self.phone.swipe(
                int(x1), int(y1), int(x2), int(y2), int(ms)
            )
        self.events.publish(
            {
                "type": "action",
                "action": "swipe",
                "from": [int(x1), int(y1)],
                "to": [int(x2), int(y2)],
                "ms": int(ms),
                "response": response,
            }
        )
        return {"ok": True, "action": "swipe", "response": response}

    def key(self, code: str) -> dict[str, Any]:
        with self._locked():
            self._ensure_ready()
            response = self.phone.key(str(code))
        self.events.publish(
            {"type": "action", "action": "key", "code": str(code), "response": response}
        )
        return {"ok": True, "action": "key", "response": response}

    def text(self, value: str) -> dict[str, Any]:
        with self._locked():
            self._ensure_ready()
            response = self.phone.text(str(value))
        self.events.publish(
            {"type": "action", "action": "text", "value": str(value), "response": response}
        )
        return {"ok": True, "action": "text", "response": response}

    # ---------------------------------------------------------- provisioning
    def provision_devices(self, timeout: float = 8.0) -> dict[str, Any]:
        from .provisioning import scan_devices

        try:
            devices = scan_devices(timeout=float(timeout))
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            raise ServiceError("PROVISION_UNAVAILABLE", str(exc), 502) from exc
        return {"ok": True, "devices": devices}

    def arm_provisioning(self) -> dict[str, Any]:
        """Ask a connected hand to start its BLE provisioning window."""
        device = getattr(self.phone, "device", None)
        if device is None or not hasattr(device, "request"):
            raise ServiceError(
                "UNAVAILABLE", "this phone does not support BLE provisioning", 409
            )
        try:
            with self._locked(2.0):
                return device.request("wifi", op="prov", timeout=2.0)
        except DeviceError as exc:
            # Starting BLE provisioning restarts the WiFi station, so the TCP
            # acknowledgement is often lost even though the device did start
            # advertising. Treat it as best-effort; the BLE scan is the truth.
            time.sleep(1.5)
            return {"ok": True, "warning": f"no ack: {exc}"}

    def provision_status(self) -> dict[str, Any]:
        with self._provision_lock:
            snapshot = dict(self._provision_state)
        started = float(snapshot.pop("started_at", 0.0) or 0.0)
        snapshot["running"] = snapshot.get("state") == "running"
        snapshot["elapsed_s"] = round(time.time() - started, 1) if started else 0.0
        snapshot["ok"] = True
        return snapshot

    def start_provision(
        self,
        ssid: str,
        password: str,
        device: str | None = None,
        pop: str | None = None,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        if not isinstance(ssid, str) or not ssid.strip():
            raise ServiceError("BAD_REQUEST", "ssid is required")
        if not isinstance(password, str) or not password:
            raise ServiceError("BAD_REQUEST", "password is required")
        with self._provision_lock:
            if (
                self._provision_thread is not None
                and self._provision_thread.is_alive()
            ):
                raise BusyError("provisioning is already running")
            self._provision_state = {
                "state": "running",
                "ssid": ssid.strip(),
                "device": device or None,
                "started_at": time.time(),
                "result": None,
                "error": None,
            }
            thread = threading.Thread(
                target=self._provision_job,
                args=(ssid.strip(), password, device or None, pop or None, float(timeout)),
                name="phone-provision",
                daemon=True,
            )
            self._provision_thread = thread
            thread.start()
        return {"ok": True, "state": "running"}

    def _provision_job(
        self,
        ssid: str,
        password: str,
        device: str | None,
        pop: str | None,
        timeout: float,
    ) -> None:
        from .provisioning import pop_from_device_name, provision

        if not pop and device:
            pop = pop_from_device_name(device)

        state = "failed"
        error: str | None = None
        summary: dict[str, Any] | None = None
        attempts = 0
        try:
            while attempts < 2:
                attempts += 1
                result = provision(
                    ssid,
                    password,
                    device=device,
                    pop=pop,
                    timeout=timeout,
                )
                output = (result.get("output") or "").lower()
                transient = "disconnected" in output or "bleak" in output
                summary = {
                    "device": result.get("device"),
                    "ssid": result.get("ssid"),
                    "elapsed_ms": result.get("elapsed_ms"),
                    "attempts": attempts,
                    "output_tail": (result.get("output") or "")[-2000:],
                }
                if result.get("ok"):
                    state = "done"
                    break
                if attempts < 2 and transient:
                    with self._provision_lock:
                        self._provision_state["result"] = summary
                    time.sleep(2.0)
                    continue
                break
            if state != "done":
                error = "provisioning failed"
        except Exception as exc:  # noqa: BLE001 - surfaced through /provision/status
            error = str(exc)
        with self._provision_lock:
            self._provision_state.update(
                {
                    "state": state,
                    "result": summary,
                    "error": error,
                    "finished_at": time.time(),
                }
            )
        self.events.publish(
            {
                "type": "provision",
                "state": state,
                "ssid": ssid,
                "device": device,
                "error": error,
            }
        )

    def run(self, scenario: str, run_dir: str | None = None) -> dict[str, Any]:
        from .scenario import Scenario, ScenarioRunner, default_run_dir

        parsed = Scenario.load(scenario)
        target = Path(run_dir) if run_dir else default_run_dir(parsed.name)
        with self._locked(timeout=2.0):
            result = ScenarioRunner(
                self.phone,
                parsed,
                engine=self.engine,
                run_dir=target,
                output=None,
            ).run()
        payload = result.to_dict()
        self.events.publish(
            {
                "type": "run",
                "name": result.name,
                "ok": result.ok,
                "run_dir": result.run_dir,
            }
        )
        return payload


class PhoneRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"PhoneService/{SERVICE_VERSION}"

    @property
    def service(self) -> PhoneService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None

    # ------------------------------------------------------------------ auth
    def _provided_token(self) -> str:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[7:].strip()
        header = self.headers.get("X-Phone-Token", "")
        if header:
            return header.strip()
        return parse_qs(urlparse(self.path).query).get("token", [""])[0]

    def _auth(self) -> bool:
        token = self.service.token
        if not token:
            return True
        if hmac.compare_digest(self._provided_token(), token):
            return True
        self._send_json(
            401,
            {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "bad token"}},
        )
        return False

    # ------------------------------------------------------------------ http
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_error(self, exc: Exception) -> None:
        if isinstance(exc, BusyError):
            status = 409
        elif isinstance(exc, ServiceError):
            status = exc.status
        elif isinstance(exc, DeviceError):
            status = 502
        elif isinstance(exc, (ValueError, TypeError, KeyError, IndexError)):
            status = 400
        elif isinstance(exc, OSError):
            status = 502
        else:
            status = 500
        code = getattr(exc, "code", None) or (
            "BAD_REQUEST" if status == 400 else "INTERNAL"
        )
        message = getattr(exc, "message", "") or str(exc)
        self._send_json(status, {"ok": False, "error": {"code": code, "message": message}})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ServiceError("BAD_REQUEST", "body too large", 413)
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError("BAD_JSON", str(exc)) from exc
        if not isinstance(value, dict):
            raise ServiceError("BAD_JSON", "body must be a JSON object")
        return value

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/health":
            self._send_json(200, self.service.health())
            return
        if path == "/events":
            if not self._auth():
                return
            self._events()
            return
        if not self._auth():
            return
        try:
            if path == "/status":
                self._send_json(200, self.service.status())
            elif path == "/provision/devices":
                query = parse_qs(parsed.query)
                timeout = float(query.get("timeout", ["8"])[0])
                self._send_json(200, self.service.provision_devices(timeout=timeout))
            elif path == "/provision/status":
                self._send_json(200, self.service.provision_status())
            elif path == "/ocr":
                query = parse_qs(parsed.query)
                min_score = float(query.get("min_score", ["0.4"])[0])
                roi = query.get("roi", [None])[0]
                self._send_json(200, self.service.ocr_texts(min_score=min_score, roi=roi))
            elif path == "/screenshot":
                query = parse_qs(parsed.query)
                include_base64 = query.get("base64", ["0"])[0] in {"1", "true", "yes"}
                max_width = query.get("max_width", [None])[0]
                payload, data = self.service.screenshot(
                    include_base64=include_base64,
                    max_width=int(max_width) if max_width else None,
                )
                if query.get("format", ["png"])[0] == "json":
                    self._send_json(200, payload)
                else:
                    self._send(200, data, "image/png")
            else:
                self._send_json(
                    404, {"ok": False, "error": {"code": "NOT_FOUND", "message": path}}
                )
        except Exception as exc:  # noqa: BLE001 - mapped to HTTP
            self._send_error(exc)

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        if not self._auth():
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/shutdown":
            # Used by the plugin supervisor to restart a service whose capture
            # went stale. Reply first, then stop serve_forever().
            self._send_json(200, {"ok": True, "shutting_down": True})
            print("phone service: shutdown requested over HTTP", flush=True)
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        try:
            body = self._read_json()
            if path == "/tap":
                self._send_json(
                    200,
                    self.service.tap(
                        int(body["x"]), int(body["y"]), body.get("hold_ms")
                    ),
                )
            elif path == "/swipe":
                self._send_json(
                    200,
                    self.service.swipe(
                        int(body["x1"]),
                        int(body["y1"]),
                        int(body["x2"]),
                        int(body["y2"]),
                        int(body.get("ms", 400)),
                    ),
                )
            elif path == "/key":
                self._send_json(200, self.service.key(str(body["code"])))
            elif path == "/text":
                value = body.get("value", body.get("s"))
                if not isinstance(value, str) or not value:
                    raise ServiceError("BAD_REQUEST", "value is required")
                self._send_json(200, self.service.text(value))
            elif path == "/screenshot":
                max_width = body.get("max_width")
                payload, _ = self.service.screenshot(
                    include_base64=bool(body.get("include_base64", False)),
                    max_width=int(max_width) if max_width else None,
                )
                self._send_json(200, payload)
            elif path == "/provision/arm":
                self._send_json(200, self.service.arm_provisioning())
            elif path == "/provision":
                self._send_json(
                    200,
                    self.service.start_provision(
                        str(body.get("ssid", "")),
                        str(body.get("password", "")),
                        device=body.get("device"),
                        pop=body.get("pop"),
                        timeout=float(body.get("timeout", 180)),
                    ),
                )
            elif path == "/find":
                text = body.get("text", body.get("keyword"))
                if not isinstance(text, str) or not text:
                    raise ServiceError("BAD_REQUEST", "text is required")
                self._send_json(
                    200,
                    self.service.find(
                        text,
                        min_score=float(body.get("min_score", 0.6)),
                        mode=str(body.get("mode", "contains")),
                        roi=body.get("roi"),
                        index=int(body.get("index", 0)),
                    ),
                )
            elif path == "/run":
                self._send_json(
                    200,
                    self.service.run(
                        str(body["scenario"]), body.get("run_dir")
                    ),
                )
            else:
                self._send_json(
                    404, {"ok": False, "error": {"code": "NOT_FOUND", "message": path}}
                )
        except Exception as exc:  # noqa: BLE001 - mapped to HTTP
            self._send_error(exc)

    # ------------------------------------------------------------------ SSE
    def _events(self) -> None:
        channel = self.service.events.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = channel.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                data = json.dumps(event, ensure_ascii=False, default=str)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.service.events.unsubscribe(channel)
            self.close_connection = True


class PhoneHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: PhoneService) -> None:
        self.service = service
        super().__init__(address, PhoneRequestHandler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind() calls socket.getfqdn(), which can block for
        # tens of seconds on machines with slow reverse DNS. We only need a
        # local service name, so skip it.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def create_server(
    phone: Phone,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str = "",
    screenshot_dir: str | Path | None = None,
    engine: "OcrEngine | None" = None,
    platform: str | None = None,
    hand_optional: bool | None = None,
    max_frame_age_ms: float | None = None,
) -> PhoneHTTPServer:
    service = PhoneService(
        phone,
        token=token,
        screenshot_dir=screenshot_dir,
        engine=engine,
        platform=platform,
        hand_optional=hand_optional,
        max_frame_age_ms=max_frame_age_ms,
    )
    return PhoneHTTPServer((host, port), service)


def new_token() -> str:
    return secrets.token_urlsafe(24)


def serve_forever(server: PhoneHTTPServer) -> None:
    try:
        server.serve_forever()
    finally:
        server.server_close()
        server.service.phone.close()
