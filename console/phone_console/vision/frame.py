"""Frame container for The Eye (screen capture -> OCR -> coordinates)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class Frame:
    """A captured screen image.

    ``image`` is HxWx3 uint8 in **BGR** order, matching OpenCV/RapidOCR.
    Use :meth:`from_pil` / :meth:`to_pil` when working with Pillow.
    """

    image: np.ndarray
    ts: float = field(default_factory=time.time)
    source: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.image, np.ndarray):
            raise TypeError("image must be a numpy array")
        if self.image.ndim == 2:
            self.image = np.stack([self.image] * 3, axis=-1)
        if self.image.ndim != 3 or self.image.shape[2] not in (3, 4):
            raise ValueError("image must be HxWx3 (BGR) or HxWx4 (BGRA)")
        if self.image.shape[2] == 4:
            self.image = self.image[:, :, :3]
        if self.image.dtype != np.uint8:
            self.image = self.image.astype(np.uint8)
        self.image = np.ascontiguousarray(self.image)
        if self.image.shape[0] < 1 or self.image.shape[1] < 1:
            raise ValueError("image must not be empty")

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    @property
    def age_ms(self) -> float:
        """Milliseconds since this frame was captured."""
        return round(max(0.0, (time.time() - self.ts) * 1000.0), 1)

    @classmethod
    def from_pil(
        cls, image: Any, source: str = "", ts: float | None = None
    ) -> "Frame":
        """Build a BGR frame from a Pillow image."""
        rgb = np.asarray(image.convert("RGB"))
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        return cls(bgr, ts=time.time() if ts is None else ts, source=source)

    def to_pil(self) -> Any:
        """Return a Pillow RGB image (copy)."""
        from PIL import Image

        return Image.fromarray(np.ascontiguousarray(self.image[:, :, ::-1]))

    def save(self, path: str | Path) -> Path:
        """Write the frame as PNG/JPEG based on the suffix."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.to_pil().save(target)
        return target

    def crop(self, box: tuple[int, int, int, int]) -> "Frame":
        """Return a copy cropped to ``(x, y, w, h)`` in frame pixels."""
        x, y, w, h = (int(value) for value in box)
        if w <= 0 or h <= 0:
            raise ValueError("crop width/height must be positive")
        if x < 0 or y < 0 or x + w > self.width or y + h > self.height:
            raise ValueError(
                f"crop {x},{y},{w},{h} outside frame {self.width}x{self.height}"
            )
        return Frame(
            self.image[y : y + h, x : x + w].copy(),
            ts=self.ts,
            source=self.source,
        )
