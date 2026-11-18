import json
import os
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")
pytest.importorskip("rapidocr_onnxruntime")

from PIL import Image

from phone_console.cli import main

ASSET = Path(__file__).resolve().parents[2] / "docs" / "assets" / "eye-demo.png"


def test_eye_find_with_image(capsys) -> None:
    code = main(["eye", "find", "设置", "--image", str(ASSET), "--screen", "540x1200"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["match"]["text"] == "设置"
    assert payload["frame"] == [1080, 2400]
    assert payload["screen"] == [540, 1200]
    assert abs(payload["match"]["device_center"][0] - 66) <= 2
    assert abs(payload["match"]["device_center"][1] - 84) <= 2


def test_eye_tap_with_mock(capsys) -> None:
    code = main(
        [
            "--mock",
            "eye",
            "tap",
            "立即更新",
            "--image",
            str(ASSET),
            "--roi",
            "0,1450,1080,300",
            "--fast",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tap"]["ok"] is True
    assert abs(payload["match"]["device_center"][0] - 512) <= 10
    assert abs(payload["match"]["device_center"][1] - 1579) <= 10


def test_eye_find_missing_text_returns_1(capsys) -> None:
    code = main(
        [
            "eye",
            "find",
            "不存在的按钮",
            "--image",
            str(ASSET),
            "--roi",
            "0,0,1080,400",
            "--fast",
        ]
    )
    assert code == 1
    assert "not found" in capsys.readouterr().err


def test_eye_shot_with_fake_adb(tmp_path, monkeypatch) -> None:
    png = tmp_path / "phone.png"
    Image.new("RGB", (33, 44), (1, 2, 3)).save(png)
    script = tmp_path / "adb"
    script.write_text('#!/bin/sh\ncat "$FAKE_PNG"\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_PNG", str(png))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    out = tmp_path / "out" / "shot.png"
    code = main(["eye", "shot", "--out", str(out)])
    assert code == 0
    assert out.is_file()
    assert Image.open(out).size == (33, 44)
