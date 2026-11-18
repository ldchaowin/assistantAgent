import numpy as np
import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")
pytest.importorskip("rapidocr_onnxruntime")
pytest.importorskip("cv2")

from pathlib import Path

from phone_console.vision import ImageSource, TextLocator, create_engine
from phone_console.vision.ocr import RapidOcrEngine

ASSET = Path(__file__).resolve().parents[2] / "docs" / "assets" / "eye-demo.png"


@pytest.fixture(scope="module")
def demo_frame():
    return ImageSource(ASSET).grab()


def test_two_stage_reads_chinese_ui(demo_frame) -> None:
    engine = create_engine()
    items = engine.recognize(demo_frame.image)
    texts = [item.text for item in items]
    assert any("设置" in text for text in texts)
    assert any("立即更新" in text for text in texts)
    assert engine.last_stage_ms["det"] > 0
    assert engine.last_stage_ms["rec"] > 0
    assert engine.last_stage_ms["boxes"] >= len(items)


def test_coordinates_match_native_detection(demo_frame) -> None:
    fast = create_engine()
    native = create_engine(det_max_side=0)
    fast_items = fast.recognize(demo_frame.image)
    native_items = native.recognize(demo_frame.image)
    locator = TextLocator(screen=(1080, 2400))
    fast_match = locator.find(fast_items, "设置", demo_frame.size)
    native_match = locator.find(native_items, "设置", demo_frame.size)
    assert fast_match is not None and native_match is not None
    assert abs(fast_match.device_center[0] - native_match.device_center[0]) <= 6
    assert abs(fast_match.device_center[1] - native_match.device_center[1]) <= 6
    assert abs(fast_match.device_center[0] - 133) <= 25
    assert abs(fast_match.device_center[1] - 168) <= 25


def test_detection_runs_on_downscaled_copy(demo_frame) -> None:
    engine = create_engine(det_max_side=512)
    engine.warmup()
    calls: list[tuple[int, int]] = []
    real = engine._engine.text_det

    def spy(image):
        calls.append(tuple(image.shape[:2]))
        return real(image)

    engine._engine.text_det = spy
    engine.recognize(demo_frame.image)
    assert calls, "detection was not called"
    assert max(calls[0]) <= 512


def test_native_detection_keeps_full_size(demo_frame) -> None:
    engine = create_engine(det_max_side=0)
    engine.warmup()
    calls: list[tuple[int, int]] = []
    real = engine._engine.text_det

    def spy(image):
        calls.append(tuple(image.shape[:2]))
        return real(image)

    engine._engine.text_det = spy
    engine.recognize(demo_frame.image)
    assert calls[0][0] == demo_frame.height


def test_line_mode_skips_detection(demo_frame) -> None:
    engine = create_engine()
    items = engine.recognize(demo_frame.image)
    target = next(item for item in items if "立即更新" in item.text)
    x0, y0 = target.top_left
    x1 = max(point[0] for point in target.box)
    y1 = max(point[1] for point in target.box)
    roi = demo_frame.crop(
        (max(0, int(x0) - 6), max(0, int(y0) - 6), int(x1 - x0) + 12, int(y1 - y0) + 12)
    )
    line_engine = create_engine(single_line=True)
    line_items = line_engine.recognize(roi.image)
    assert line_items, "line mode returned no text"
    assert "立即更新" in line_items[0].text
    assert line_engine.last_stage_ms["det"] == 0
    assert line_engine.last_stage_ms["crops"] == 1


def test_frame_cache_skips_model_work(demo_frame) -> None:
    engine = create_engine()
    engine.warmup()
    first = engine.recognize(demo_frame.image)
    assert first
    assert engine.hits["frame"] == 0

    def boom(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("detection ran on an unchanged frame")

    engine._engine.text_det = boom
    cached = engine.recognize(demo_frame.image)
    assert [item.text for item in cached] == [item.text for item in first]
    assert engine.hits["frame"] == 1
    assert engine.last_stage_ms["cache"] == 1.0
    assert engine.last_elapsed_ms == 0.0


def test_crop_cache_only_recognizes_changed_regions(demo_frame) -> None:
    engine = create_engine()
    engine.warmup()
    image = demo_frame.image.copy()
    engine.recognize(image)
    assert engine.hits["crop"] == 0

    recognized: list[int] = []
    real = engine._engine.text_rec

    def spy(crops):
        recognized.append(len(crops))
        return real(crops)

    engine._engine.text_rec = spy
    changed = image.copy()
    changed[0:80, 0:320] = 255 - changed[0:80, 0:320]
    engine.recognize(changed)
    assert engine.hits["crop"] >= 8
    assert sum(recognized) <= 6


def test_cache_can_be_disabled(demo_frame) -> None:
    engine = create_engine(cache=False)
    engine.warmup()
    engine.recognize(demo_frame.image)
    calls: list[int] = []
    real = engine._engine.text_det

    def spy(image):
        calls.append(1)
        return real(image)

    engine._engine.text_det = spy
    engine.recognize(demo_frame.image)
    assert len(calls) == 1
    assert engine.hits["frame"] == 0


def test_clear_cache_resets_hits(demo_frame) -> None:
    engine = create_engine()
    engine.warmup()
    engine.recognize(demo_frame.image)
    engine.recognize(demo_frame.image)
    assert engine.hits["frame"] == 1
    engine.clear_cache()
    assert engine.hits == {"frame": 0, "crop": 0}
    engine.recognize(demo_frame.image)
    assert engine.hits["frame"] == 0


def test_rejects_non_image_input() -> None:
    engine = create_engine(cache=False)
    with pytest.raises(ValueError):
        engine.recognize(np.zeros((10, 10), dtype=np.uint8))


def test_rejects_negative_det_max_side() -> None:
    with pytest.raises(ValueError):
        RapidOcrEngine(det_max_side=-1)


def test_roi_offset_keeps_frame_coordinates(demo_frame) -> None:
    from phone_console.vision.ocr import offset_items

    engine = create_engine()
    roi_box = (0, 1450, 1080, 300)
    roi = demo_frame.crop(roi_box)
    items = engine.recognize(roi.image)
    assert any("立即更新" in item.text for item in items)
    shifted = offset_items(items, roi_box[0], roi_box[1])
    assert all(item.center[1] >= 1450 for item in shifted)
