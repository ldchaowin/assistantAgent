import json

import pytest

np = pytest.importorskip("numpy")

from phone_console.cli import main
from phone_console.doctor import run_doctor, write_report
from phone_console.virtual import VirtualPhone
from phone_console.vision.frame import Frame


def make_phone() -> VirtualPhone:
    return VirtualPhone([Frame(np.zeros((10, 10, 3), dtype=np.uint8))])


def test_doctor_mock_reports_checks() -> None:
    phone = make_phone()
    try:
        report = run_doctor(phone, "mock", check_adb=False, output=None)
    finally:
        phone.close()
    assert report.ok is True
    names = {check.name for check in report.checks}
    assert {"python", "package", "hello", "status", "ping", "screen", "calibration", "vision"} <= names
    calibration = next(check for check in report.checks if check.name == "calibration")
    assert calibration.status == "warn"  # default cal


def test_doctor_without_phone_fails() -> None:
    report = run_doctor(None, "mock", check_adb=False, output=None)
    assert report.ok is False
    assert any(check.status == "fail" for check in report.checks)


def test_doctor_json_report(tmp_path) -> None:
    phone = make_phone()
    try:
        report = run_doctor(phone, "mock", check_adb=False, output=None)
    finally:
        phone.close()
    path = write_report(report, tmp_path / "doctor.json")
    payload = json.loads(path.read_text())
    assert payload["ok"] is True
    assert payload["mode"] == "mock"


def test_doctor_cli(capsys, tmp_path) -> None:
    out = tmp_path / "doctor.json"
    code = main(["--mock", "doctor", "--json", "--skip-adb", "--out", str(out)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert out.is_file()
