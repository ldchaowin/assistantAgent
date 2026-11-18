"""OCR engines for The Eye.

The default engine runs a **two-stage** pipeline instead of RapidOCR's
one-shot call:

1. detect text boxes on a downscaled copy of the frame (cheap, ``det_max_side``);
2. crop those boxes from the **original, full-resolution** frame;
3. recognize every crop with the recognizer, one small batch at a time.

That keeps recognition quality (the recognizer still sees native-resolution
glyphs) while cutting the expensive detection pass by 3-4x. Two exact-content
caches sit on top:

* a frame cache: an identical frame returns the previous result without any
  model work (screen unchanged while a scenario polls for text);
* a crop cache: only crops whose pixels changed are recognized again, so a
  clock ticking in the status bar does not invalidate the whole screen.

RapidOCR is imported lazily so the base console (hand-only) does not need
numpy/opencv/onnxruntime installed.
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol, Sequence

import numpy as np

# Text detection runs on a copy whose longest side is at most this many pixels.
# 1024 keeps small UI labels detectable on a 1264x2800 phone while being ~4x
# cheaper than native resolution; pass 0 to detect at native size.
DEFAULT_DET_MAX_SIDE = 1024

# Pixels of padding around a detected box when cropping for recognition.
DEFAULT_CROP_PADDING = 2

# The recognizer's own score filter (RapidOCR's ``text_score``).
MIN_TEXT_SCORE = 0.5

# Width of the grayscale signature used for the frame cache. Wide enough that
# a toast or a status-bar change is visible, small enough to hash in <1ms.
CACHE_SIGNATURE_WIDTH = 192


class OcrError(RuntimeError):
    """OCR engine missing or failed."""


@dataclass(frozen=True)
class OcrItem:
    """One detected text box, in frame pixel coordinates."""

    text: str
    score: float
    box: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if not self.box:
            raise ValueError("OCR box must not be empty")

    @property
    def center(self) -> tuple[float, float]:
        xs = [point[0] for point in self.box]
        ys = [point[1] for point in self.box]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    @property
    def top_left(self) -> tuple[float, float]:
        return (
            min(point[0] for point in self.box),
            min(point[1] for point in self.box),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "score": round(float(self.score), 4),
            "box": [[round(float(x), 1), round(float(y), 1)] for x, y in self.box],
            "center": [round(self.center[0], 1), round(self.center[1], 1)],
        }


class OcrEngine(Protocol):
    name: str
    last_elapsed_ms: float
    last_stage_ms: dict[str, float]

    def recognize(
        self, image: np.ndarray, *, single_line: bool | None = None
    ) -> list[OcrItem]: ...

    def warmup(self) -> None: ...


def _lru_get(cache: "OrderedDict[Any, Any]", key: Any) -> Any:
    value = cache.get(key)
    if value is not None:
        cache.move_to_end(key)
    return value


def _lru_put(cache: "OrderedDict[Any, Any]", key: Any, value: Any, limit: int) -> None:
    if limit <= 0:
        return
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


class RapidOcrEngine:
    """RapidOCR (PP-OCR ONNX) wrapper with a fast two-stage pipeline.

    ``engine_kwargs`` are passed straight to ``RapidOCR(...)``; the engine
    forces ``det_limit_type="max"`` so that detection never *upscales* the
    already-sized image we hand it (RapidOCR's default ``min`` mode would
    blow a 1024px-wide phone frame back up to 1632px).
    """

    name = "rapidocr"

    def __init__(
        self,
        *,
        det_max_side: int = DEFAULT_DET_MAX_SIDE,
        threads: int = 4,
        rec_batch_num: int = 1,
        cache: bool = True,
        cache_size: int = 8,
        crop_cache_size: int = 2048,
        single_line: bool = False,
        crop_padding: int = DEFAULT_CROP_PADDING,
        min_text_score: float = MIN_TEXT_SCORE,
        **engine_kwargs: Any,
    ) -> None:
        if det_max_side is not None and det_max_side < 0:
            raise ValueError("det_max_side must be >= 0 (0 = native size)")
        if rec_batch_num < 1:
            raise ValueError("rec_batch_num must be >= 1")
        self.det_max_side = int(det_max_side or 0)
        self.crop_padding = max(0, int(crop_padding))
        self.single_line = bool(single_line)
        self.min_text_score = float(min_text_score)
        # Phone screens are upright, so the 180-degree classifier is wasted
        # work. A fixed small thread pool beats onnxruntime's all-core default
        # for these small models.
        self._engine_kwargs: dict[str, Any] = {
            "use_cls": False,
            "intra_op_num_threads": int(threads),
            "rec_batch_num": int(rec_batch_num),
            "det_limit_type": "max",
            **engine_kwargs,
        }
        self._engine: Any = None
        self._cv2: Any = None
        self._lock = threading.RLock()
        self._cache_enabled = bool(cache)
        self._frame_cache: "OrderedDict[Any, tuple[OcrItem, ...]]" = OrderedDict()
        self._crop_cache: "OrderedDict[Any, tuple[str, float]]" = OrderedDict()
        self._cache_size = int(cache_size)
        self._crop_cache_size = int(crop_cache_size)
        self.last_elapsed_ms: float = 0.0
        self.last_stage_ms: dict[str, float] = {}
        self.hits: dict[str, int] = {"frame": 0, "crop": 0}

    # ------------------------------------------------------------------ setup
    def _load(self) -> None:
        if self._engine is not None:
            return
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:  # pragma: no cover - depends on env
            raise OcrError(
                "RapidOCR is not installed; install the vision extras:\n"
                "  .venv/bin/pip install -e 'console[vision]'"
            ) from exc
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - depends on env
            raise OcrError(
                "OpenCV is not installed; install the vision extras:\n"
                "  .venv/bin/pip install -e 'console[vision]'"
            ) from exc
        self._cv2 = cv2
        self._engine = RapidOCR(**self._engine_kwargs)

    def warmup(self) -> None:
        """Load the models (and run one tiny inference) ahead of the first call."""
        with self._lock:
            self._load()
            image = np.full((240, 480, 3), 255, dtype=np.uint8)
            try:
                self._recognize_boxes(image)
            except Exception:  # noqa: BLE001 - warmup is best effort
                pass
            self.last_elapsed_ms = 0.0
            self.last_stage_ms = {}
            self._frame_cache.clear()

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    # -------------------------------------------------------------- recognize
    def recognize(
        self, image: np.ndarray, *, single_line: bool | None = None
    ) -> list[OcrItem]:
        """OCR one frame, returning boxes in that frame's pixel coordinates.

        ``single_line=True`` skips detection and recognizes the whole image as
        one text line: use it with a tight ``--roi`` around a button or label
        for ~10x lower latency.
        """
        image = self._as_image(image)
        mode = self.single_line if single_line is None else bool(single_line)
        with self._lock:
            self._load()
            key = self._frame_key(image) if self._cache_enabled else None
            if key is not None:
                cached = _lru_get(self._frame_cache, (mode, key))
                if cached is not None:
                    self.hits["frame"] += 1
                    self.last_elapsed_ms = 0.0
                    self.last_stage_ms = {"total": 0.0, "cache": 1.0}
                    return list(cached)
            started = perf_counter()
            if mode:
                items = self._recognize_line(image)
            else:
                items = self._recognize_boxes(image)
            elapsed = (perf_counter() - started) * 1000.0
            self.last_elapsed_ms = elapsed
            self.last_stage_ms["total"] = round(elapsed, 3)
            self.last_stage_ms.setdefault("cache", 0.0)
            if key is not None:
                _lru_put(
                    self._frame_cache,
                    (mode, key),
                    tuple(items),
                    self._cache_size,
                )
            return list(items)

    # ------------------------------------------------------------------ stages
    def _recognize_boxes(self, image: np.ndarray) -> list[OcrItem]:
        started = perf_counter()
        boxes = self._detect(image)
        det_ms = (perf_counter() - started) * 1000.0
        crops, boxes = self._crops(image, boxes)
        crops_started = perf_counter()
        results = self._recognize_crops(crops)
        rec_ms = (perf_counter() - crops_started) * 1000.0
        items: list[OcrItem] = []
        for box, (text, score) in zip(boxes, results):
            if not text or score < self.min_text_score:
                continue
            points = tuple((float(point[0]), float(point[1])) for point in box)
            if len(points) < 4:
                continue
            items.append(OcrItem(text=str(text), score=float(score), box=points))
        self.last_stage_ms = {
            "det": round(det_ms, 3),
            "rec": round(rec_ms, 3),
            "boxes": float(len(boxes)),
            "crops": float(len(crops)),
            "cache": 0.0,
        }
        return items

    def _recognize_line(self, image: np.ndarray) -> list[OcrItem]:
        started = perf_counter()
        results = self._recognize_crops([image])
        rec_ms = (perf_counter() - started) * 1000.0
        self.last_stage_ms = {
            "det": 0.0,
            "rec": round(rec_ms, 3),
            "boxes": 0.0,
            "crops": 1.0,
            "cache": 0.0,
        }
        height, width = image.shape[:2]
        items: list[OcrItem] = []
        for text, score in results:
            if not text or score < self.min_text_score:
                continue
            items.append(
                OcrItem(
                    text=str(text),
                    score=float(score),
                    box=(
                        (0.0, 0.0),
                        (float(width), 0.0),
                        (float(width), float(height)),
                        (0.0, float(height)),
                    ),
                )
            )
        return items

    def _detect(self, image: np.ndarray) -> list[np.ndarray]:
        cv2 = self._cv2
        height, width = image.shape[:2]
        scale = 1.0
        det_image = image
        longest = max(height, width)
        if self.det_max_side and longest > self.det_max_side:
            scale = self.det_max_side / float(longest)
            det_image = cv2.resize(
                image,
                (
                    max(1, int(round(width * scale))),
                    max(1, int(round(height * scale))),
                ),
                interpolation=cv2.INTER_AREA,
            )
        boxes, _ = self._engine.text_det(det_image)
        if boxes is None or len(boxes) < 1:
            return []
        ordered = self._engine.sorted_boxes(boxes)
        if scale != 1.0:
            ordered = [np.asarray(box, dtype=np.float32) / scale for box in ordered]
        return ordered

    def _crops(
        self, image: np.ndarray, boxes: Sequence[np.ndarray]
    ) -> tuple[list[np.ndarray], list[np.ndarray]]:
        height, width = image.shape[:2]
        pad = self.crop_padding
        crops: list[np.ndarray] = []
        kept: list[np.ndarray] = []
        for box in boxes:
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            x0 = max(0, int(min(xs)) - pad)
            y0 = max(0, int(min(ys)) - pad)
            x1 = min(width, int(max(xs)) + 1 + pad)
            y1 = min(height, int(max(ys)) + 1 + pad)
            if x1 - x0 < 2 or y1 - y0 < 4:
                continue
            crops.append(np.ascontiguousarray(image[y0:y1, x0:x1]))
            kept.append(box)
        return crops, kept

    def _recognize_crops(
        self, crops: Sequence[np.ndarray]
    ) -> list[tuple[str, float]]:
        results: list[tuple[str, float] | None] = [None] * len(crops)
        pending: list[np.ndarray] = []
        pending_index: list[int] = []
        pending_keys: list[Any] = []
        for index, crop in enumerate(crops):
            key = self._crop_key(crop)
            if key is not None:
                cached = _lru_get(self._crop_cache, key)
                if cached is not None:
                    self.hits["crop"] += 1
                    results[index] = cached
                    continue
            pending.append(crop)
            pending_index.append(index)
            pending_keys.append(key)
        if pending:
            try:
                recognized, _ = self._engine.text_rec(pending)
            except Exception as exc:  # noqa: BLE001 - surfaced as OcrError
                raise OcrError(f"RapidOCR failed: {exc}") from exc
            for index, key, result in zip(pending_index, pending_keys, recognized):
                text, score = str(result[0]), float(result[1])
                results[index] = (text, score)
                if key is not None:
                    _lru_put(
                        self._crop_cache,
                        key,
                        (text, score),
                        self._crop_cache_size,
                    )
        return [result if result is not None else ("", 0.0) for result in results]

    # ------------------------------------------------------------------ cache
    def _frame_key(self, image: np.ndarray) -> Any:
        cv2 = self._cv2
        height, width = image.shape[:2]
        # A full-frame resize costs ~15ms on a 1264x2800 phone screenshot,
        # which would defeat the point of caching. Subsampling every Nth pixel
        # first brings the signature down to ~1ms while still seeing any UI
        # change larger than a few pixels.
        step = max(1, int(round(min(height, width) / 400.0)))
        sample = image[::step, ::step]
        gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY)
        sample_h, sample_w = gray.shape[:2]
        scale = min(1.0, float(CACHE_SIGNATURE_WIDTH) / max(sample_h, sample_w))
        signature = cv2.resize(
            gray,
            (
                max(1, int(round(sample_w * scale))),
                max(1, int(round(sample_h * scale))),
            ),
            interpolation=cv2.INTER_AREA,
        )
        return (image.shape, signature.tobytes())

    def _crop_key(self, crop: np.ndarray) -> Any:
        if not self._cache_enabled or self._crop_cache_size <= 0:
            return None
        digest = hashlib.blake2b(crop.tobytes(), digest_size=16).digest()
        return (crop.shape, digest)

    def clear_cache(self) -> None:
        with self._lock:
            self._frame_cache.clear()
            self._crop_cache.clear()
            self.hits = {"frame": 0, "crop": 0}

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _as_image(image: np.ndarray) -> np.ndarray:
        array = np.asarray(image)
        if array.ndim != 3 or array.shape[2] not in (3, 4):
            raise ValueError("image must be HxWx3 (BGR) or HxWx4 (BGRA)")
        if array.shape[2] == 4:
            array = array[:, :, :3]
        if array.dtype != np.uint8:
            array = array.astype(np.uint8)
        if array.shape[0] < 1 or array.shape[1] < 1:
            raise ValueError("image must not be empty")
        return array


def offset_items(items: Sequence[OcrItem], dx: int, dy: int) -> list[OcrItem]:
    """Shift boxes from a cropped ROI back into full-frame coordinates."""
    if dx == 0 and dy == 0:
        return list(items)
    return [
        OcrItem(
            text=item.text,
            score=item.score,
            box=tuple((x + dx, y + dy) for x, y in item.box),
        )
        for item in items
    ]


def create_engine(name: str = "rapidocr", **kwargs: Any) -> OcrEngine:
    if name == "rapidocr":
        return RapidOcrEngine(**kwargs)
    raise ValueError(f"unknown OCR engine: {name}")
