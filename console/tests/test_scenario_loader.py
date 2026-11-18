import json
from pathlib import Path

import pytest

from phone_console.scenario import (
    Scenario,
    ScenarioError,
    default_run_dir,
    parse_roi,
    slugify,
)


def write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_valid_scenario(tmp_path) -> None:
    path = write(
        tmp_path,
        {
            "name": "demo",
            "description": "desc",
            "screen": [1080, 2400],
            "frames": "frames",
            "defaults": {"timeout": 2, "retries": 1, "mode": "exact"},
            "steps": [
                {"action": "tap_text", "text": "设置"},
                {"action": "swipe", "x1": 1, "y1": 2, "x2": 3, "y2": 4},
            ],
        },
    )
    scenario = Scenario.load(path)
    assert scenario.name == "demo"
    assert scenario.screen == (1080, 2400)
    assert scenario.frames_path == (tmp_path / "frames").resolve()
    assert scenario.defaults["timeout"] == 2.0
    assert scenario.defaults["mode"] == "exact"
    assert scenario.steps[1]["ms"] == 400
    assert scenario.to_dict()["name"] == "demo"


def test_missing_file() -> None:
    with pytest.raises(ScenarioError, match="not found"):
        Scenario.load("/nonexistent/scenario.json")


def test_invalid_json(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ScenarioError, match="invalid JSON"):
        Scenario.load(path)


def test_steps_must_be_non_empty(tmp_path) -> None:
    with pytest.raises(ScenarioError, match="steps"):
        Scenario.load(write(tmp_path, {"steps": []}))


def test_unknown_action_is_rejected(tmp_path) -> None:
    with pytest.raises(ScenarioError, match="unknown action"):
        Scenario.load(write(tmp_path, {"steps": [{"action": "teleport"}]}))


def test_text_step_requires_text(tmp_path) -> None:
    with pytest.raises(ScenarioError, match="non-empty text"):
        Scenario.load(write(tmp_path, {"steps": [{"action": "tap_text"}]}))


def test_text_step_requires_value(tmp_path) -> None:
    with pytest.raises(ScenarioError, match="value"):
        Scenario.load(write(tmp_path, {"steps": [{"action": "text"}]}))


def test_invalid_mode_is_rejected(tmp_path) -> None:
    with pytest.raises(ScenarioError, match="mode"):
        Scenario.load(
            write(
                tmp_path,
                {"steps": [{"action": "tap_text", "text": "x", "mode": "fuzzy"}]},
            )
        )


def test_invalid_retry_on_timeout_is_rejected(tmp_path) -> None:
    with pytest.raises(ScenarioError, match="retry_on_timeout"):
        Scenario.load(
            write(
                tmp_path,
                {
                    "steps": [
                        {"action": "tap_text", "text": "x", "retry_on_timeout": "yes"}
                    ]
                },
            )
        )


def test_invalid_roi_is_rejected(tmp_path) -> None:
    with pytest.raises(ScenarioError, match="roi"):
        Scenario.load(
            write(
                tmp_path,
                {"steps": [{"action": "tap_text", "text": "x", "roi": [1, 2, 0, 4]}]},
            )
        )


def test_invalid_screen_is_rejected(tmp_path) -> None:
    with pytest.raises(ScenarioError, match="screen"):
        Scenario.load(write(tmp_path, {"screen": [0, 100], "steps": [{"action": "sleep", "ms": 1}]}))


def test_invalid_defaults_are_rejected(tmp_path) -> None:
    with pytest.raises(ScenarioError, match="min_score"):
        Scenario.load(
            write(
                tmp_path,
                {
                    "defaults": {"min_score": 2},
                    "steps": [{"action": "sleep", "ms": 1}],
                },
            )
        )


def test_parse_roi_accepts_list_and_string() -> None:
    assert parse_roi([1, 2, 3, 4]) == (1, 2, 3, 4)
    assert parse_roi("1,2,3,4") == (1, 2, 3, 4)
    with pytest.raises(ScenarioError):
        parse_roi("1,2,3")


def test_slugify_and_run_dir() -> None:
    assert slugify("Price Protect / Demo") == "price-protect-demo"
    assert slugify("") == "scenario"
    run_dir = default_run_dir("My Demo")
    assert run_dir.parent == Path("runs")
    assert run_dir.name.endswith("-my-demo")
