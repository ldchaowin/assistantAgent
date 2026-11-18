"""iOS "mirror" eye: capture an AirPlay / iPhone Mirroring window on macOS.

Plan B for iPhone automation: the ESP32 hand occupies the iPhone's USB port,
so the screen arrives wirelessly instead. macOS shows the mirrored iPhone in a
window (system "iPhone Mirroring" or UxPlay); this module finds that window and
captures it on a timer so it looks like any other :class:`FrameSource`.

Capture uses ``screencapture -l <windowID>`` (~70-80 ms per frame on an M4),
which inherits the host app's Screen Recording permission - no extra helper app
or second TCC prompt is needed.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .frame import Frame

PLATFORM = "ios"

# Owner/title fragments that identify a mirrored-iPhone window. The system app
# is localized ("iPhone镜像" on a Chinese macOS), UxPlay is third-party.
MIRROR_HINTS = (
    "iphone mirroring",
    "iphone镜像",
    "iphone 镜像",
    "iphone 镜像",
    "uxplay",
)


class MirrorError(RuntimeError):
    """The mirror window (or the Screen Recording permission) is unavailable."""


def _quartz():
    try:
        import Quartz  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on env
        raise MirrorError(
            "缺少 pyobjc Quartz 依赖：.venv/bin/pip install -e 'console[mirror]'"
        ) from exc
    return Quartz


def list_windows(include_all: bool = False) -> list[dict[str, Any]]:
    """On-screen windows via CoreGraphics (id/owner/title/bounds)."""
    quartz = _quartz()
    infos = quartz.CGWindowListCopyWindowInfo(
        quartz.kCGWindowListOptionOnScreenOnly, quartz.kCGNullWindowID
    )
    windows: list[dict[str, Any]] = []
    for info in infos or []:
        if info.get("kCGWindowLayer") != 0 and not include_all:
            continue
        bounds = info.get("kCGWindowBounds") or {}
        width = int(bounds.get("Width", 0) or 0)
        height = int(bounds.get("Height", 0) or 0)
        if width <= 0 or height <= 0:
            continue
        owner = str(info.get("kCGWindowOwnerName") or "")
        title = str(info.get("kCGWindowName") or "")
        windows.append(
            {
                "id": int(info.get("kCGWindowNumber") or 0),
                "owner": owner,
                "title": title,
                "width": width,
                "height": height,
                "bounds": {
                    "x": int(bounds.get("X", 0) or 0),
                    "y": int(bounds.get("Y", 0) or 0),
                    "w": width,
                    "h": height,
                },
            }
        )
    return windows


def _is_mirror_window(window: dict[str, Any]) -> bool:
    haystack = f"{window.get('owner', '')} {window.get('title', '')}".lower()
    return any(hint in haystack for hint in MIRROR_HINTS)


def is_welcome_window(window: dict[str, Any]) -> bool:
    haystack = f"{window.get('owner', '')} {window.get('title', '')}".lower()
    return "欢迎" in haystack or "welcome" in haystack


def find_mirror_window(wanted: str | int | None = None) -> dict[str, Any] | None:
    """Pick the mirrored-iPhone window (largest matching one by default)."""
    windows = list_windows()
    if wanted not in (None, ""):
        text = str(wanted).strip()
        for window in windows:
            if text.isdigit() and window["id"] == int(text):
                return window
            haystack = f"{window['owner']} {window['title']} {window['id']}".lower()
            if text.lower() in haystack:
                return window
        return None
    matches = [window for window in windows if _is_mirror_window(window)]
    if not matches:
        return None
    matches.sort(key=lambda item: item["width"] * item["height"], reverse=True)
    return matches[0]


def mirror_windows() -> list[dict[str, Any]]:
    """All on-screen windows that look like a mirrored iPhone."""
    return [window for window in list_windows() if _is_mirror_window(window)]


def mirror_available() -> bool:
    try:
        _quartz()
    except MirrorError:
        return False
    return True


def _screencapture_path() -> str:
    found = shutil.which("screencapture") or "/usr/sbin/screencapture"
    if not Path(found).is_file():
        raise MirrorError("找不到 screencapture 命令（macOS 系统命令）")
    return found


def capture_window_png(window_id: int, path: str | Path) -> bytes:
    """Capture one window to a PNG file and return its bytes."""
    target = Path(path)
    command = [_screencapture_path(), "-x", "-o", "-l", str(int(window_id)), str(target)]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MirrorError(f"screencapture 失败: {exc}") from exc
    if proc.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise MirrorError(
            "无法截取投屏窗口"
            + (f": {detail}" if detail else "（窗口可能已关闭；确认已允许屏幕录制权限）")
        )
    return target.read_bytes()


def _decode_png(data: bytes, source: str, max_width: int = 0) -> Frame:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        if max_width and image.width > max_width:
            height = max(1, round(image.height * max_width / image.width))
            image = image.resize((int(max_width), height))
        return Frame.from_pil(image, source=source)


class MirrorWindowSource:
    """Live frames of a mirrored-iPhone window, captured on a timer.

    Implements the same interface as the other eye sources:
    ``name / grab / latest / wait_newer / stats / close`` plus
    ``frame_size`` / ``native_size`` for coordinate mapping.
    """

    def __init__(
        self,
        window: str | int | None = None,
        *,
        interval: float = 0.1,
        max_width: int = 0,
        startup_timeout: float = 20.0,
    ) -> None:
        self._wanted = window
        self.interval = max(0.03, float(interval))
        self.max_width = int(max_width or 0)
        self.startup_timeout = max(2.0, float(startup_timeout))
        self._window: dict[str, Any] | None = None
        self._frame: Frame | None = None
        self._cond = threading.Condition()
        self._closing = False
        self._reader: threading.Thread | None = None
        self._frames = 0
        self._capture_ms = 0.0
        self._started_at = 0.0
        self._last_frame_at = 0.0
        self._error: str | None = None
        self._tmpdir: Path | None = None
        self._tmpfile: Path | None = None

    # ------------------------------------------------------------------ setup
    def _resolve_window(self) -> dict[str, Any]:
        window = find_mirror_window(self._wanted)
        if window is None:
            raise MirrorError(
                "没有找到投屏窗口：打开 iPhone Mirroring（系统自带，需解锁 iPhone 并按提示连接），"
                "或用 UxPlay 接收 iPhone 的屏幕镜像；也可用 --window ID 指定窗口"
            )
        self._window = window
        return window

    def _ensure_started(self) -> None:
        with self._cond:
            if self._reader is not None and self._reader.is_alive():
                return
            if self._closing:
                raise MirrorError(f"{self.name}: source is closed")
            self._error = None
            self._frame = None
            self._frames = 0
        self._resolve_window()
        self._tmpdir = Path(tempfile.mkdtemp(prefix="phone-mirror-"))
        self._tmpfile = self._tmpdir / "frame.png"
        self._started_at = time.time()
        self._reader = threading.Thread(target=self._run, name="ios-mirror", daemon=True)
        self._reader.start()
        deadline = time.monotonic() + self.startup_timeout
        with self._cond:
            while self._frame is None:
                if self._error:
                    raise MirrorError(f"{self.name}: {self._error}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MirrorError(f"{self.name}: 没有收到投屏窗口画面")
                self._cond.wait(min(remaining, 0.2))

    # ----------------------------------------------------------------- frames
    def _run(self) -> None:
        while True:
            with self._cond:
                if self._closing:
                    return
                window = self._window
            started = time.perf_counter()
            try:
                if window is None:
                    window = self._resolve_window()
                data = capture_window_png(window["id"], self._tmpfile)
                frame = _decode_png(data, self.name, self.max_width)
            except MirrorError as exc:
                with self._cond:
                    self._error = str(exc)
                    self._cond.notify_all()
                # Window closed / mirroring stopped: try to re-find it later.
                with self._cond:
                    if self._closing:
                        return
                self._window = None
                time.sleep(max(0.2, self.interval))
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            with self._cond:
                self._frame = frame
                self._frames += 1
                self._capture_ms = elapsed_ms
                self._last_frame_at = time.time()
                self._error = None
                self._cond.notify_all()
            time.sleep(max(0.0, self.interval - elapsed_ms / 1000.0))

    @property
    def name(self) -> str:
        window = self._window
        if window is None:
            return "ios:mirror"
        title = window.get("title") or ""
        return f"ios:mirror:{window.get('owner')}" + (f"/{title}" if title else "")

    @property
    def window(self) -> dict[str, Any] | None:
        return self._window

    @property
    def frame_size(self) -> tuple[int, int] | None:
        with self._cond:
            return self._frame.size if self._frame is not None else None

    @property
    def native_size(self) -> tuple[int, int] | None:
        """Mirror-window pixel size (== the captured frame when not downscaled)."""
        return self.frame_size

    # ------------------------------------------------------------------- api
    def grab(self, timeout: float | None = None) -> Frame:
        self._ensure_started()
        deadline = time.monotonic() + (
            self.startup_timeout if timeout is None else float(timeout)
        )
        with self._cond:
            while self._frame is None:
                if self._error:
                    raise MirrorError(f"{self.name}: {self._error}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MirrorError(f"{self.name}: 没有画面")
                self._cond.wait(min(remaining, 0.2))
            return self._frame

    def latest(self) -> Frame | None:
        with self._cond:
            return self._frame

    def wait_newer(self, after: float, timeout: float = 2.0) -> Frame:
        self._ensure_started()
        deadline = time.monotonic() + float(timeout)
        with self._cond:
            while self._frame is None or self._frame.ts <= after:
                if self._error:
                    raise MirrorError(f"{self.name}: {self._error}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MirrorError(f"{self.name}: {timeout:.2f}s 内没有新画面")
                self._cond.wait(min(remaining, 0.2))
            return self._frame

    def stats(self) -> dict[str, Any]:
        with self._cond:
            frames = self._frames
            frame = self._frame
            last = self._last_frame_at
            error = self._error
            window = self._window
            capture_ms = self._capture_ms
        elapsed = max(1e-6, time.time() - self._started_at) if self._started_at else 0.0
        return {
            "name": self.name,
            "backend": "mirror",
            "window": window,
            "frames": frames,
            "fps": round(frames / elapsed, 2) if elapsed and frames else 0.0,
            "capture_ms": round(capture_ms, 1),
            "size": list(frame.size) if frame is not None else None,
            "age_ms": round((time.time() - last) * 1000.0, 1) if last else None,
            "running": bool(self._reader is not None and self._reader.is_alive()),
            "error": error,
        }

    def close(self) -> None:
        with self._cond:
            if self._closing:
                return
            self._closing = True
            reader = self._reader
            self._cond.notify_all()
        if reader is not None and reader.is_alive():
            reader.join(timeout=2.0)
        if self._tmpdir is not None:
            try:
                shutil.rmtree(self._tmpdir, ignore_errors=True)
            except OSError:
                pass
            self._tmpdir = None
            self._tmpfile = None

    def __enter__(self) -> "MirrorWindowSource":
        self._ensure_started()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def capture_mirror_shot(
    window: str | int | None = None, *, timeout: float = 15.0
) -> tuple[Frame, dict[str, Any]]:
    """Capture a single frame from the mirror window."""
    selected = find_mirror_window(window)
    if selected is None:
        raise MirrorError(
            "没有找到投屏窗口：打开 iPhone Mirroring（系统自带）或 UxPlay 后重试"
        )
    with tempfile.TemporaryDirectory(prefix="phone-mirror-") as tmpdir:
        path = Path(tmpdir) / "shot.png"
        data = capture_window_png(selected["id"], path)
    frame = _decode_png(data, "ios:mirror")
    return frame, selected
