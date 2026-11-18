from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")
pytest.importorskip("rapidocr_onnxruntime")

from phone_console.vision import ImageSource, TextLocator, create_engine

ASSET = Path(__file__).resolve().parents[2] / "docs" / "assets" / "eye-demo.png"


@pytest.fixture(scope="module")
def demo_frame():
    return ImageSource(ASSET).grab()


@pytest.fixture(scope="module")
def demo_items(demo_frame):
    engine = create_engine()
    return engine.recognize(demo_frame.image)


def test_rapidocr_reads_chinese_ui(demo_items) -> None:
    texts = [item.text for item in demo_items]
    assert any("设置" in text for text in texts)
    assert any("立即更新" in text for text in texts)


def test_found_text_maps_to_phone_pixels(demo_frame, demo_items) -> None:
    match = TextLocator(screen=(1080, 2400)).find(demo_items, "设置", demo_frame.size)
    assert match is not None
    assert abs(match.device_center[0] - 133) <= 25
    assert abs(match.device_center[1] - 168) <= 25


def test_roi_crop_finds_button(demo_frame) -> None:
    crop = demo_frame.crop((0, 1450, 1080, 300))
    engine = create_engine(det_limit_side_len=480)
    items = engine.recognize(crop.image)
    assert any("立即更新" in item.text for item in items)
    assert engine.last_elapsed_ms > 0
