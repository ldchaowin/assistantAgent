"""Debug overlay for OCR results."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .frame import Frame
from .locate import Match
from .ocr import OcrItem


def _font() -> object:
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=18)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def annotate(
    frame: Frame,
    items: Sequence[OcrItem],
    path: str | Path,
    matches: Sequence[Match] = (),
) -> Path:
    """Write a copy of the frame with OCR boxes, indices and match centers."""
    from PIL import ImageDraw

    image = frame.to_pil()
    draw = ImageDraw.Draw(image)
    font = _font()

    for index, item in enumerate(items):
        box = list(item.box) + [item.box[0]]
        draw.line(box, fill=(0, 200, 0), width=2)
        x, y = item.top_left
        draw.text((x, max(0.0, y - 20)), str(index), fill=(0, 140, 0), font=font)

    for match in matches:
        x, y = match.center
        radius = 14
        draw.ellipse(
            [x - radius, y - radius, x + radius, y + radius],
            outline=(255, 0, 0),
            width=3,
        )
        draw.line(
            [(x - radius - 6, y), (x + radius + 6, y)], fill=(255, 0, 0), width=2
        )
        draw.line(
            [(x, y - radius - 6), (x, y + radius + 6)], fill=(255, 0, 0), width=2
        )

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target)
    return target
