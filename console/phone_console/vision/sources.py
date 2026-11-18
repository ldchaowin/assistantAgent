"""Frame sources: still images and adb screenshots.

Live scrcpy / screenrecord sources live in :mod:`phone_console.vision.stream`
and implement the same :class:`FrameSource` interface.
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from .adb import AdbError, authorization_hint, resolve_adb
from .frame import Frame

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@runtime_checkable
class FrameSource(Protocol):
    """Anything that can produce the latest screen frame."""

    @property
    def name(self) -> str: ...

    def grab(self) -> Frame: ...

    def close(self) -> None: ...


def _decode_image(data: bytes, source: str) -> Frame:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return Frame.from_pil(image, source=source)


class ImageSource:
    """A still PNG/JPG screenshot. Repeated grabs return the same frame."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._frame: Frame | None = None

    @property
    def name(self) -> str:
        return f"image:{self.path}"

    def grab(self) -> Frame:
        if self._frame is None:
            if not self.path.is_file():
                raise FileNotFoundError(f"image not found: {self.path}")
            from PIL import Image

            with Image.open(self.path) as image:
                self._frame = Frame.from_pil(image, source=self.name)
        return self._frame

    def close(self) -> None:
        return None


class AdbSource:
    """One-shot screenshots through ``adb exec-out screencap -p``.

    This is the zero-extra-service path: it works with any Android device that
    has USB debugging enabled, but each grab costs hundreds of milliseconds.
    Use it for still-frame OCR and calibration, not for 10 Hz closed loops.
    """

    def __init__(
        self,
        serial: str | None = None,
        adb: str | None = None,
        timeout: float = 8.0,
    ) -> None:
        try:
            resolved = resolve_adb(adb)
        except AdbError as exc:
            raise RuntimeError(str(exc)) from exc
        self.adb = resolved
        self.serial = serial or None
        self.timeout = timeout

    @property
    def name(self) -> str:
        return f"adb:{self.serial}" if self.serial else "adb:default"

    def _command(self) -> list[str]:
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += ["exec-out", "screencap", "-p"]
        return cmd

    def grab(self) -> Frame:
        try:
            proc = subprocess.run(
                self._command(),
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"adb screencap timed out after {self.timeout:.1f}s"
            ) from exc
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()
            if "unauthorized" in detail or "no devices" in detail:
                detail = f"{detail} ({authorization_hint('unauthorized')})"
            raise RuntimeError(f"adb screencap failed: {detail or proc.returncode}")
        data = proc.stdout
        # A few adb/device combinations prepend CRLF; trim to the PNG header.
        offset = data.find(PNG_SIGNATURE)
        if offset < 0:
            raise RuntimeError("adb screencap returned no PNG data")
        return _decode_image(data[offset:], self.name)

    def close(self) -> None:
        return None
