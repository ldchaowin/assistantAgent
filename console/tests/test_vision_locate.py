import pytest

pytest.importorskip("numpy")

from phone_console.vision.locate import (
    MatchMode,
    TextLocator,
    map_to_device,
    normalize_text,
)
from phone_console.vision.ocr import OcrItem


def item(text: str, x: float, y: float, score: float = 0.9, w: float = 100, h: float = 40) -> OcrItem:
    return OcrItem(
        text=text,
        score=score,
        box=((x, y), (x + w, y), (x + w, y + h), (x, y + h)),
    )


def test_normalize_drops_whitespace_and_case() -> None:
    assert normalize_text(" 设 置 ") == "设置"
    assert normalize_text("Settings") == "settings"


def test_map_to_device_scales_and_rounds() -> None:
    assert map_to_device((150.0, 220.0), (540, 1200), (1080, 2400)) == (300, 440)
    assert map_to_device((10.0, 10.0), (1080, 2400), (1080, 2400)) == (10, 10)


def test_map_to_device_clamps_to_screen() -> None:
    assert map_to_device((-10.0, 5000.0), (1080, 2400), (1080, 2400)) == (0, 2399)


def test_contains_is_default_and_matches_chinese() -> None:
    locator = TextLocator(screen=(1080, 2400))
    match = locator.find([item("系统设置", 100, 100)], "设置", (540, 1200))
    assert match is not None
    assert match.device_center == (300, 240)


def test_exact_mode_requires_full_text() -> None:
    locator = TextLocator(mode=MatchMode.EXACT)
    assert locator.find([item("系统设置", 0, 0)], "设置", (1080, 2400)) is None
    assert locator.find([item("设置", 0, 0)], "设置", (1080, 2400)) is not None


def test_regex_mode() -> None:
    locator = TextLocator(mode="regex")
    assert locator.find([item("立即更新", 0, 0)], r"^立即", (1080, 2400)) is not None
    with pytest.raises(ValueError):
        locator.find([item("x", 0, 0)], "(", (1080, 2400))


def test_min_score_filters_low_confidence() -> None:
    locator = TextLocator(min_score=0.8)
    assert locator.find([item("设置", 0, 0, score=0.5)], "设置", (1080, 2400)) is None


def test_matches_are_in_reading_order() -> None:
    locator = TextLocator()
    items = [item("设置", 100, 500), item("设置", 100, 100)]
    matches = locator.find_all(items, "设置", (1080, 2400))
    assert [m.center[1] for m in matches] == [120.0, 520.0]
    assert locator.find(items, "设置", (1080, 2400), index=1).center[1] == 520.0


def test_negative_index_and_out_of_range() -> None:
    locator = TextLocator()
    items = [item("设置", 100, 100), item("设置", 100, 500)]
    assert locator.find(items, "设置", (1080, 2400), index=-1).center[1] == 520.0
    with pytest.raises(IndexError):
        locator.find(items, "设置", (1080, 2400), index=5)


def test_whitespace_insensitive_match() -> None:
    locator = TextLocator()
    assert locator.find([item("设 置", 0, 0)], "设置", (1080, 2400)) is not None


def test_empty_keyword_is_rejected() -> None:
    with pytest.raises(ValueError):
        TextLocator().find([], "", (1080, 2400))
