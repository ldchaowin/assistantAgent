"""High-level device API over a Transport."""
from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

from . import protocol
from .log import JsonlLogger
from .protocol import ProtocolError
from .transport import TransportError

if TYPE_CHECKING:
    from .transport import Transport


class DeviceError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


class DeviceTimeout(DeviceError):
    def __init__(self, message: str = "no response") -> None:
        super().__init__("TIMEOUT", message)


class Device:
    def __init__(
        self,
        transport: "Transport",
        timeout: float = 1.0,
        logger: JsonlLogger | None = None,
    ) -> None:
        self.transport = transport
        self.timeout = timeout
        self.logger = logger
        self.screen = (1080, 2400)
        self.cal = (0, 32767, 0, 32767)
        self.events: list[dict[str, Any]] = []
        self.unsolicited: list[dict[str, Any]] = []
        self._rid = 0

    def _next_id(self) -> int:
        self._rid += 1
        if self._rid > 2_000_000_000:
            self._rid = 1
        return self._rid

    @staticmethod
    def _decode(line: bytes) -> dict[str, Any] | None:
        """Ignore boot-log noise on a freshly opened UART."""
        try:
            return protocol.decode(line)
        except ProtocolError:
            return None

    def _write(self, payload: dict[str, Any]) -> None:
        data = protocol.encode(payload)
        try:
            self.transport.write(data)
            return
        except TransportError as exc:
            reconnect = getattr(self.transport, "reconnect", None)
            if not callable(reconnect):
                raise DeviceError("DISCONNECT", str(exc)) from exc
        try:
            reconnect()  # type: ignore[operator]
            self.transport.write(data)
        except (TransportError, OSError) as exc:
            raise DeviceError("DISCONNECT", str(exc)) from exc

    def _log(self, **record: Any) -> None:
        if self.logger is not None:
            self.logger.log(**record)

    def request(self, cmd: str, timeout: float | None = None, **params: Any) -> dict:
        rid = self._next_id()
        timeout = self.timeout if timeout is None else timeout
        payload = {"id": rid, "cmd": cmd, **params}
        started = time.monotonic()
        self._write(payload)
        deadline = started + timeout

        while time.monotonic() < deadline:
            try:
                line = self.transport.readline(
                    max(0.005, deadline - time.monotonic())
                )
            except TransportError as exc:
                raise DeviceError("DISCONNECT", str(exc)) from exc
            if line is None:
                continue
            message = self._decode(line)
            if message is None:
                continue
            if protocol.is_event(message):
                self.events.append(message)
                continue
            if message.get("id") != rid:
                self.unsolicited.append(message)
                continue
            latency = (time.monotonic() - started) * 1000.0
            self._log(
                id=rid,
                cmd=cmd,
                params=params,
                response=message,
                latency_ms=round(latency, 2),
            )
            if message.get("ok") is True:
                return message
            code, text = protocol.error_of(message)
            raise DeviceError(code, text)

        self._log(id=rid, cmd=cmd, params=params, error="timeout")
        raise DeviceTimeout(f"no response to {cmd} (id={rid})")

    def _event(
        self, cmd: str, event_name: str, timeout: float, **params: Any
    ) -> dict:
        rid = self._next_id()
        payload = {"id": rid, "cmd": cmd, **params}
        started = time.monotonic()
        self._write(payload)
        deadline = started + timeout

        while time.monotonic() < deadline:
            try:
                line = self.transport.readline(
                    max(0.005, deadline - time.monotonic())
                )
            except TransportError as exc:
                raise DeviceError("DISCONNECT", str(exc)) from exc
            if line is None:
                continue
            message = self._decode(line)
            if message is None:
                continue
            if protocol.is_event(message):
                if message.get("evt") == event_name:
                    self._log(
                        id=rid,
                        cmd=cmd,
                        params=params,
                        event=message,
                        latency_ms=round((time.monotonic() - started) * 1000.0, 2),
                    )
                    return message
                self.events.append(message)
                continue
            if message.get("id") == rid and message.get("ok") is False:
                code, text = protocol.error_of(message)
                raise DeviceError(code, text)

        raise DeviceTimeout(f"event {event_name} not received for {cmd}")

    # ------------------------------------------------------------------ commands
    def hello(self) -> dict:
        try:
            event = self._event("hello", "ready", timeout=2.0)
        except DeviceTimeout:
            # The ready event can be lost when another client takes over the
            # single TCP slot; retry on a fresh connection.
            reconnect = getattr(self.transport, "reconnect", None)
            if not callable(reconnect):
                raise
            reconnect()
            event = self._event("hello", "ready", timeout=2.0)
        screen = event.get("screen")
        cal = event.get("cal")
        if isinstance(screen, list) and len(screen) == 2:
            self.screen = (int(screen[0]), int(screen[1]))
        if isinstance(cal, list) and len(cal) == 4:
            self.cal = tuple(int(v) for v in cal)  # type: ignore[assignment]
        return event

    def ping(self) -> dict:
        ts = int(time.time() * 1000)
        started = time.monotonic()
        event = self._event("ping", "pong", timeout=1.0, ts=ts)
        return {**event, "rtt_ms": round((time.monotonic() - started) * 1000.0, 2)}

    def configure(
        self,
        screen: tuple[int, int] | None = None,
        cal: tuple[int, int, int, int] | None = None,
    ) -> dict:
        if screen is None and cal is None:
            raise ValueError("screen or cal is required")
        payload: dict[str, Any] = {}
        if screen is not None:
            payload["screen"] = {"w": int(screen[0]), "h": int(screen[1])}
        if cal is not None:
            payload["cal"] = {
                "x0": int(cal[0]),
                "x1": int(cal[1]),
                "y0": int(cal[2]),
                "y1": int(cal[3]),
            }
        response = self.request("cfg", **payload)
        if screen is not None:
            self.screen = (int(screen[0]), int(screen[1]))
        if cal is not None:
            self.cal = tuple(int(v) for v in cal)  # type: ignore[assignment]
        return response

    def save(self) -> dict:
        return self.request("save")

    def status(self) -> dict:
        return self.request("status")

    def stop(self) -> dict:
        return self.request("stop", timeout=1.5)

    def tap(self, x: int, y: int, hold_ms: int | None = None) -> dict:
        params: dict[str, Any] = {"x": int(x), "y": int(y)}
        if hold_ms is not None:
            params["hold_ms"] = int(hold_ms)
        return self.request("tap", timeout=max(1.0, (hold_ms or 0) / 1000.0 + 0.7), **params)

    def move(self, x: int, y: int) -> dict:
        """Move the absolute pointer without clicking (firmware >= 0.4.0).

        Old firmware answers UNKNOWN_CMD; callers that only want a warm-up
        should tolerate that.
        """
        return self.request("move", x=int(x), y=int(y))

    def swipe(
        self, x1: int, y1: int, x2: int, y2: int, ms: int = 400
    ) -> dict:
        timeout = max(1.5, int(ms) / 1000.0 + 1.0)
        return self.request(
            "swipe",
            timeout=timeout,
            x1=int(x1),
            y1=int(y1),
            x2=int(x2),
            y2=int(y2),
            ms=int(ms),
        )

    def key(self, code: str) -> dict:
        return self.request("key", code=str(code).upper())

    def text(self, value: str) -> dict:
        timeout = max(1.0, len(value) * 0.03 + 0.8)
        return self.request("text", timeout=timeout, s=value)

    def wait_event(
        self,
        name: str,
        timeout: float = 20.0,
        match: Any = None,
    ) -> dict:
        """Wait for an async event (e.g. ``wifi``) on the control channel."""
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            try:
                line = self.transport.readline(
                    max(0.005, deadline - time.monotonic())
                )
            except TransportError as exc:
                raise DeviceError("DISCONNECT", str(exc)) from exc
            if line is None:
                continue
            message = self._decode(line)
            if message is None:
                continue
            if not protocol.is_event(message):
                self.unsolicited.append(message)
                continue
            self.events.append(message)
            if message.get("evt") != name:
                continue
            if match is None or match(message):
                return message
        raise DeviceTimeout(f"event {name} not received")

    # ------------------------------------------------------------------ wifi
    def wifi_status(self) -> dict:
        return self.request("wifi", op="status")

    def wifi_set(
        self,
        ssid: str,
        password: str,
        port: int | None = None,
        timeout: float = 2.0,
    ) -> dict:
        params: dict[str, Any] = {"ssid": ssid, "pass": password}
        if port is not None:
            params["port"] = int(port)
        return self.request("wifi", timeout=timeout, op="set", **params)

    def wifi_clear(self) -> dict:
        return self.request("wifi", op="clear")

    def close(self) -> None:
        self.transport.close()
        if self.logger is not None:
            self.logger.close()
