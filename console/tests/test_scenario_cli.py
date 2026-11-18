import json
from pathlib import Path

import pytest

from phone_console.cli import main

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "scenarios"


def test_run_dry_run_prints_steps(capsys) -> None:
    code = main(["run", str(SCENARIOS / "settings-demo.json"), "--dry-run"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "settings-demo"
    assert len(payload["steps"]) == 6


def test_run_settings_demo_on_virtual_phone(capsys, tmp_path) -> None:
    pytest.importorskip("rapidocr_onnxruntime")
    run_dir = tmp_path / "run"
    record_dir = tmp_path / "record"
    code = main(
        [
            "--mock",
            "run",
            str(SCENARIOS / "settings-demo.json"),
            "--run-dir",
            str(run_dir),
            "--record",
            str(record_dir),
        ]
    )
    assert code == 0
    assert "PASS" in capsys.readouterr().out
    assert (run_dir / "report.jsonl").is_file()
    assert (run_dir / "scenario.json").is_file()
    assert (record_dir / "actions.jsonl").is_file()
    assert list((record_dir / "frames").glob("*.png"))


def test_run_json_output(capsys, tmp_path) -> None:
    pytest.importorskip("rapidocr_onnxruntime")
    code = main(
        [
            "--mock",
            "run",
            str(SCENARIOS / "settings-demo.json"),
            "--json",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert len(payload["steps"]) == 6
    assert payload["steps"][0]["action"] == "tap_text"


def test_run_image_overrides_scenario_frames(capsys, tmp_path) -> None:
    pytest.importorskip("rapidocr_onnxruntime")
    scenario = tmp_path / "image.json"
    scenario.write_text(
        json.dumps(
            {
                "name": "image",
                "frames": "missing-frames",
                "steps": [{"action": "assert_text", "text": "设置"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    code = main(
        [
            "--mock",
            "run",
            str(scenario),
            "--image",
            str(ROOT / "docs" / "assets" / "eye-demo.png"),
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert code == 0
    assert "PASS" in capsys.readouterr().out


def test_run_failure_returns_1_and_saves_artifacts(capsys, tmp_path) -> None:
    pytest.importorskip("rapidocr_onnxruntime")
    scenario = tmp_path / "fail.json"
    scenario.write_text(
        json.dumps(
            {
                "name": "fail",
                "defaults": {"timeout": 0.1, "retries": 0, "interval": 0.01},
                "steps": [{"action": "tap_text", "text": "不存在的按钮"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    code = main(
        [
            "--mock",
            "run",
            str(scenario),
            "--image",
            str(ROOT / "docs" / "assets" / "eye-demo.png"),
            "--run-dir",
            str(run_dir),
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.out
    assert "not found" in captured.out
    assert (run_dir / "failure-01.png").is_file()


def test_run_invalid_scenario_returns_2(capsys, tmp_path) -> None:
    scenario = tmp_path / "bad.json"
    scenario.write_text('{"steps": []}', encoding="utf-8")
    assert main(["run", str(scenario)]) == 2
    assert "steps" in capsys.readouterr().err


def test_run_adb_with_mock_is_rejected(capsys) -> None:
    code = main(
        [
            "--mock",
            "run",
            str(SCENARIOS / "settings-demo.json"),
            "--adb",
        ]
    )
    assert code == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_run_frames_without_mock_is_rejected(capsys) -> None:
    code = main(
        [
            "run",
            str(SCENARIOS / "settings-demo.json"),
            "--frames",
            str(SCENARIOS / "frames" / "settings"),
        ]
    )
    assert code == 2
    assert "only for --mock" in capsys.readouterr().err


def test_run_key_only_without_frame_source(capsys, tmp_path) -> None:
    scenario = tmp_path / "keys.json"
    scenario.write_text(
        json.dumps({"name": "keys", "steps": [{"action": "key", "code": "BACK"}]}),
        encoding="utf-8",
    )
    code = main(
        [
            "--mock",
            "run",
            str(scenario),
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert code == 0
    assert "PASS" in capsys.readouterr().out
