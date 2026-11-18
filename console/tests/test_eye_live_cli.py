import json
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")
pytest.importorskip("rapidocr_onnxruntime")

from phone_console.cli import main

ASSET = Path(__file__).resolve().parents[2] / "docs" / "assets" / "eye-demo.png"


def parse(capsys) -> dict:
    captured = capsys.readouterr()
    return json.loads(captured.out)


def test_eye_bench_reports_stages_and_cache(capsys) -> None:
    code = main(["eye", "bench", "--image", str(ASSET), "--repeat", "3"])
    payload = parse(capsys)
    assert code == 0
    assert payload["items"] > 0
    assert payload["frame"] == [1080, 2400]
    assert payload["total_ms"]["p50"] > 0
    assert payload["det_ms"]["max"] > 0
    assert payload["cache_hits"]["frame"] >= 1
    assert payload["det_max"] == 1024


def test_eye_bench_line_mode_on_roi(capsys) -> None:
    code = main(
        [
            "eye",
            "bench",
            "--image",
            str(ASSET),
            "--roi",
            "400,1535,230,90",
            "--line",
            "--repeat",
            "2",
        ]
    )
    payload = parse(capsys)
    assert code == 0
    assert payload["line"] is True
    assert payload["det_ms"]["max"] == 0
    assert payload["items"] >= 1


def test_eye_ocr_line_without_roi_is_rejected(capsys) -> None:
    code = main(["eye", "ocr", "--image", str(ASSET), "--line"])
    captured = capsys.readouterr()
    assert code == 2
    assert "--line needs --roi" in captured.err


def test_eye_find_with_small_det(capsys) -> None:
    code = main(
        [
            "eye",
            "find",
            "设置",
            "--image",
            str(ASSET),
            "--screen",
            "1080x2400",
            "--det-max",
            "512",
        ]
    )
    payload = parse(capsys)
    assert code == 0
    assert payload["count"] == 1
    assert payload["stages"]["det"] > 0
    assert abs(payload["match"]["device_center"][0] - 133) <= 25


def test_eye_find_miss_returns_one(capsys) -> None:
    code = main(
        ["eye", "find", "no-such-label-xyz", "--image", str(ASSET), "--no-cache"]
    )
    payload = parse(capsys)
    assert code == 1
    assert payload["count"] == 0
    assert payload["match"] is None


def test_eye_stream_rejects_still_sources(capsys) -> None:
    code = main(["eye", "stream", "--image", str(ASSET), "--seconds", "0.2"])
    captured = capsys.readouterr()
    assert code == 2
    assert "needs --scrcpy" in captured.err


def test_eye_watch_finds_text_in_first_frame(capsys) -> None:
    code = main(
        [
            "eye",
            "watch",
            "设置",
            "--image",
            str(ASSET),
            "--timeout",
            "5",
            "--interval",
            "0.05",
        ]
    )
    payload = parse(capsys)
    assert code == 0
    assert payload["found"] is True
    assert payload["attempts"] == 1
    assert payload["match"]["device_center"]


def test_eye_watch_timeout_returns_one(capsys) -> None:
    code = main(
        [
            "eye",
            "watch",
            "no-such-label-xyz",
            "--image",
            str(ASSET),
            "--timeout",
            "0.3",
            "--interval",
            "0.05",
        ]
    )
    payload = parse(capsys)
    assert code == 1
    assert payload["found"] is False
    assert payload["attempts"] >= 1


def write_adb(tmp_path, body: str):
    script = tmp_path / "adb"
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_adb_status_cli_reports_authorization(tmp_path, capsys) -> None:
    script = write_adb(
        tmp_path, "printf 'List of devices attached\\nSER1\\tunauthorized\\n'"
    )
    code = main(["adb", "status", "--adb-path", str(script)])
    payload = parse(capsys)
    assert code == 1
    assert payload["state"] == "unauthorized"
    assert "允许" in payload["hint"]


def test_adb_pair_and_connect_cli(tmp_path, capsys) -> None:
    script = write_adb(
        tmp_path,
        'case "$1" in\n'
        '  pair) echo "Successfully paired to $2"; exit 0;;\n'
        '  connect) echo "connected to $2"; exit 0;;\n'
        '  devices) printf "List of devices attached\\n192.168.1.23:41235\\tdevice\n"; exit 0;;\n'
        'esac',
    )
    code = main(
        ["adb", "pair", "192.168.1.23:37123", "123456", "--adb-path", str(script)]
    )
    assert code == 0
    pair_payload = parse(capsys)
    assert pair_payload["ok"] is True

    code = main(
        ["adb", "connect", "192.168.1.23:41235", "--adb-path", str(script)]
    )
    payload = parse(capsys)
    assert code == 0
    assert payload["status"]["state"] == "device"


def test_adb_wait_cli_times_out(tmp_path, capsys) -> None:
    script = write_adb(
        tmp_path, "printf 'List of devices attached\\nSER1\\tunauthorized\\n'"
    )
    code = main(
        ["adb", "wait", "--adb-path", str(script), "--timeout", "0.2"]
    )
    payload = parse(capsys)
    assert code == 1
    assert payload["timed_out"] is True
