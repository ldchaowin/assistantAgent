"""Match OCR text and map frame pixels to phone pixels."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

from .ocr import OcrItem

_WHITESPACE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Drop whitespace and case-fold, so ``设 置`` matches ``设置``."""
    return _WHITESPACE.sub("", text).casefold()


class MatchMode(str, Enum):
    CONTAINS = "contains"
    EXACT = "exact"
    REGEX = "regex"


def map_to_device(
    point: tuple[float, float],
    frame_size: tuple[int, int],
    device_size: tuple[int, int],
) -> tuple[int, int]:
    """Scale a frame-pixel point to phone-physical pixels and clamp it.

    The capture must show the whole screen with the same aspect ratio as the
    device (adb screenshots and scrcpy's default full-frame mode both do).
    """
    frame_w, frame_h = frame_size
    device_w, device_h = device_size
    if frame_w <= 0 or frame_h <= 0 or device_w <= 0 or device_h <= 0:
        raise ValueError("frame and device sizes must be positive")
    x = int(round(point[0] * device_w / frame_w))
    y = int(round(point[1] * device_h / frame_h))
    return (
        min(max(x, 0), device_w - 1),
        min(max(y, 0), device_h - 1),
    )


@dataclass(frozen=True)
class Match:
    keyword: str
    text: str
    score: float
    box: tuple[tuple[float, float], ...]
    center: tuple[float, float]
    device_center: tuple[int, int]
    frame_size: tuple[int, int]
    device_size: tuple[int, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "keyword": self.keyword,
            "text": self.text,
            "score": round(float(self.score), 4),
            "box": [[round(float(x), 1), round(float(y), 1)] for x, y in self.box],
            "center": [round(self.center[0], 1), round(self.center[1], 1)],
            "device_center": list(self.device_center),
            "frame_size": list(self.frame_size),
            "device_size": list(self.device_size),
        }


class TextLocator:
    """Filter OCR items by text/score and return phone coordinates."""

    def __init__(
        self,
        screen: tuple[int, int] | None = None,
        min_score: float = 0.6,
        mode: MatchMode | str = MatchMode.CONTAINS,
    ) -> None:
        if not 0.0 <= min_score <= 1.0:
            raise ValueError("min_score must be in [0, 1]")
        self.screen = (int(screen[0]), int(screen[1])) if screen else None
        if self.screen is not None and (self.screen[0] <= 0 or self.screen[1] <= 0):
            raise ValueError("screen must be positive")
        self.min_score = float(min_score)
        self.mode = MatchMode(mode)

    def _text_matches(self, text: str, keyword: str) -> bool:
        if self.mode is MatchMode.CONTAINS:
            return normalize_text(keyword) in normalize_text(text)
        if self.mode is MatchMode.EXACT:
            return normalize_text(keyword) == normalize_text(text)
        return re.search(keyword, text, re.IGNORECASE) is not None

    def find_all(
        self,
        items: Sequence[OcrItem],
        keyword: str,
        frame_size: tuple[int, int],
    ) -> list[Match]:
        """All matches in reading order (top-to-bottom, left-to-right)."""
        if not keyword:
            raise ValueError("keyword must not be empty")
        if self.mode is MatchMode.REGEX:
            try:
                re.compile(keyword)
            except re.error as exc:
                raise ValueError(f"invalid regex: {exc}") from exc
        frame_w, frame_h = int(frame_size[0]), int(frame_size[1])
        if frame_w <= 0 or frame_h <= 0:
            raise ValueError("frame_size must be positive")
        device_size = self.screen or (frame_w, frame_h)
        found = [
            item
            for item in items
            if item.score >= self.min_score and self._text_matches(item.text, keyword)
        ]
        found.sort(key=lambda item: (item.top_left[1], item.top_left[0]))
        matches: list[Match] = []
        for item in found:
            center = item.center
            matches.append(
                Match(
                    keyword=keyword,
                    text=item.text,
                    score=item.score,
                    box=item.box,
                    center=center,
                    device_center=map_to_device(center, (frame_w, frame_h), device_size),
                    frame_size=(frame_w, frame_h),
                    device_size=device_size,
                )
            )
        return matches

    def find(
        self,
        items: Sequence[OcrItem],
        keyword: str,
        frame_size: tuple[int, int],
        index: int = 0,
    ) -> Match | None:
        matches = self.find_all(items, keyword, frame_size)
        if not matches:
            return None
        if index < 0:
            index += len(matches)
        if not 0 <= index < len(matches):
            raise IndexError(
                f"index {index} out of range; {len(matches)} match(es) for {keyword!r}"
            )
        return matches[index]
