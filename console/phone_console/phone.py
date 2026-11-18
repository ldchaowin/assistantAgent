"""A phone is a device plus a frame source: The Hand + The Eye."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .device import Device

if TYPE_CHECKING:
    from .vision.frame import Frame
    from .vision.sources import FrameSource


@runtime_checkable
class Phone(Protocol):
    """What the scenario runner needs: HID actions plus the latest frame."""

    @property
    def screen(self) -> tuple[int, int]: ...

    @property
    def cal(self) -> tuple[int, int, int, int]: ...

    def hello(self) -> dict: ...

    def status(self) -> dict: ...

    def ping(self) -> dict: ...

    def stop(self) -> dict: ...

    def configure(
        self,
        screen: tuple[int, int] | None = None,
        cal: tuple[int, int, int, int] | None = None,
    ) -> dict: ...

    def save(self) -> dict: ...

    def tap(self, x: int, y: int, hold_ms: int | None = None) -> dict: ...

    def swipe(
        self, x1: int, y1: int, x2: int, y2: int, ms: int = 400
    ) -> dict: ...

    def key(self, code: str) -> dict: ...

    def text(self, value: str) -> dict: ...

    def grab(self) -> "Frame": ...

    def close(self) -> None: ...


class DevicePhone:
    """Pair an existing :class:`Device` with a frame source."""

    platform = "android"

    def __init__(self, device: Device, source: "FrameSource") -> None:
        self.device = device
        self.source = source

    @property
    def screen(self) -> tuple[int, int]:
        return self.device.screen

    @property
    def cal(self) -> tuple[int, int, int, int]:
        return self.device.cal

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

    def tap(self, x: int, y: int, hold_ms: int | None = None) -> dict:
        return self.device.tap(x, y, hold_ms)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400) -> dict:
        return self.device.swipe(x1, y1, x2, y2, ms)

    def key(self, code: str) -> dict:
        return self.device.key(code)

    def text(self, value: str) -> dict:
        return self.device.text(value)

    def grab(self) -> "Frame":
        return self.source.grab()

    def close(self) -> None:
        try:
            self.device.close()
        finally:
            self.source.close()


class RecordingPhone:
    """Wrap a phone and record every grabbed frame and HID action.

    Useful for capturing a real-device session for inspection; the recorded
    frames can then be turned into a frame-sequence scenario for CI.
    """

    @property
    def platform(self) -> str:
        return getattr(self.inner, "platform", "android")

    def __init__(self, inner: Phone, directory: str | Path) -> None:
        self.inner = inner
        self.dir = Path(directory)
        (self.dir / "frames").mkdir(parents=True, exist_ok=True)
        self._frames = 0
        self._actions = 0
        self._closed = False
        self._frame_log = (self.dir / "frames.jsonl").open("w", encoding="utf-8")
        self._action_log = (self.dir / "actions.jsonl").open("w", encoding="utf-8")

    @property
    def screen(self) -> tuple[int, int]:
        return self.inner.screen

    @property
    def cal(self) -> tuple[int, int, int, int]:
        return self.inner.cal

    def hello(self) -> dict:
        return self.inner.hello()

    def status(self) -> dict:
        return self.inner.status()

    def ping(self) -> dict:
        return self.inner.ping()

    def stop(self) -> dict:
        return self.inner.stop()

    def configure(
        self,
        screen: tuple[int, int] | None = None,
        cal: tuple[int, int, int, int] | None = None,
    ) -> dict:
        return self.inner.configure(screen=screen, cal=cal)

    def save(self) -> dict:
        return self.inner.save()

    def grab(self) -> "Frame":
        frame = self.inner.grab()
        path = self.dir / "frames" / f"{self._frames:04d}.png"
        frame.save(path)
        self._frame_log.write(
            json.dumps(
                {"index": self._frames, "ts": frame.ts, "path": str(path)},
                ensure_ascii=False,
            )
            + "\n"
        )
        self._frame_log.flush()
        self._frames += 1
        return frame

    def _record(self, action: str, params: dict[str, Any], response: dict) -> None:
        self._action_log.write(
            json.dumps(
                {
                    "index": self._actions,
                    "action": action,
                    "params": params,
                    "response": response,
                    "ts": time.time(),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        self._action_log.flush()
        self._actions += 1

    def tap(self, x: int, y: int, hold_ms: int | None = None) -> dict:
        response = self.inner.tap(x, y, hold_ms)
        self._record("tap", {"x": x, "y": y, "hold_ms": hold_ms}, response)
        return response

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400) -> dict:
        response = self.inner.swipe(x1, y1, x2, y2, ms)
        self._record(
            "swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms}, response
        )
        return response

    def key(self, code: str) -> dict:
        response = self.inner.key(code)
        self._record("key", {"code": code}, response)
        return response

    def text(self, value: str) -> dict:
        response = self.inner.text(value)
        self._record("text", {"value": value}, response)
        return response

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._frame_log.close()
        self._action_log.close()
        self.inner.close()


class NullSource:
    """Placeholder for scenarios that only use HID actions."""

    @property
    def name(self) -> str:
        return "none"

    def grab(self) -> Any:
        raise RuntimeError(
            "this scenario needs a frame source; pass --image, --frames, or --adb"
        )

    def close(self) -> None:
        return None
