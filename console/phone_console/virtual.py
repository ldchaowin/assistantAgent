"""Virtual phone: frame sequence + MockDevice, for hardware-free scenarios."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .device import Device
from .transport import MockTransport

if TYPE_CHECKING:
    from .vision.frame import Frame

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


class VirtualPhone:
    """A phone whose screen is a fixed sequence of frames.

    Each successful action advances one frame. ``grab`` (used by
    ``wait_for_text`` / ``assert_text``) does not advance, so scenario frames
    should line up with actions:

    ``tap_text`` on frame 0 -> tap succeeds -> frame 1 -> ``assert_text`` sees 1.
    """

    platform = "mock"

    def __init__(
        self,
        frames: Iterable["Frame"],
        timeout: float = 1.0,
        start: int = 0,
        advance: bool = True,
    ) -> None:
        self.frames = list(frames)
        if not self.frames:
            raise ValueError("VirtualPhone needs at least one frame")
        if not 0 <= start < len(self.frames):
            raise ValueError(f"start {start} outside 0..{len(self.frames) - 1}")
        self.index = start
        self.advance = advance
        self.transport = MockTransport()
        self.device = Device(self.transport, timeout=timeout)
        self.device.hello()

    @classmethod
    def from_dir(
        cls,
        path: str | Path,
        timeout: float = 1.0,
        start: int = 0,
    ) -> "VirtualPhone":
        directory = Path(path)
        if not directory.is_dir():
            raise FileNotFoundError(f"frame directory not found: {directory}")
        files = sorted(
            item
            for item in directory.iterdir()
            if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
        )
        if not files:
            raise FileNotFoundError(f"no images in frame directory: {directory}")
        from .vision.sources import ImageSource

        frames = [ImageSource(item).grab() for item in files]
        return cls(frames, timeout=timeout, start=start)

    # ------------------------------------------------------------------ state
    @property
    def screen(self) -> tuple[int, int]:
        return self.device.screen

    @property
    def cal(self) -> tuple[int, int, int, int]:
        return self.device.cal

    @property
    def mock(self):
        return self.transport.device

    @property
    def current(self) -> "Frame":
        return self.frames[self.index]

    def reset(self, index: int = 0) -> None:
        if not 0 <= index < len(self.frames):
            raise ValueError(f"index {index} outside 0..{len(self.frames) - 1}")
        self.index = index
        self.device.hello()

    def _advance(self) -> None:
        if self.advance and self.index < len(self.frames) - 1:
            self.index += 1

    # ------------------------------------------------------------------ phone
    def hello(self) -> dict:
        return self.device.hello()

    def status(self) -> dict:
        return self.device.status()

    def ping(self) -> dict:
        return self.device.ping()

    def stop(self) -> dict:
        return self.device.stop()

    def configure(
        self,
        screen: tuple[int, int] | None = None,
        cal: tuple[int, int, int, int] | None = None,
    ) -> dict:
        return self.device.configure(screen=screen, cal=cal)

    def save(self) -> dict:
        return self.device.save()

    def grab(self) -> "Frame":
        return self.frames[self.index]

    def tap(self, x: int, y: int, hold_ms: int | None = None) -> dict:
        response = self.device.tap(x, y, hold_ms)
        self._advance()
        return response

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400) -> dict:
        response = self.device.swipe(x1, y1, x2, y2, ms)
        self._advance()
        return response

    def key(self, code: str) -> dict:
        response = self.device.key(code)
        self._advance()
        return response

    def text(self, value: str) -> dict:
        response = self.device.text(value)
        self._advance()
        return response

    def close(self) -> None:
        self.device.close()
