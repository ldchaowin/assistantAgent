import json

import pytest

np = pytest.importorskip("numpy")

from phone_console.acceptance import run_acceptance, write_report
from phone_console.cli import main
from phone_console.device import DeviceError
from phone_console.virtual import VirtualPhone
from phone_console.vision.frame import Frame


def make_phone() -> VirtualPhone:
    return VirtualPhone([Frame(np.zeros((10, 10, 3), dtype=np.uint8))])


class TapFailPhone(VirtualPhone):
    def tap(self, x, y, hold_ms=None):
        raise DeviceError("OUT_OF_RANGE", "injected")


def test_acceptance_mock_quick() -> None:
    phone = make_phone()
    try:
        report = run_acceptance(phone, "mock", quick=True, output=None)
    finally:
        phone.close()
    assert report.ok is True
    by_id = {item.id: item for item in report.items}
    assert by_id["T3"].status == "pass"
    assert by_id["T4"].status == "pass"
    assert by_id["T5"].status == "pass"
    assert by_id["T10"].status == "pass"
    assert by_id["T4m"].status == "skip"  # manual item on mock
    assert by_id["T11"].status == "skip"


def test_acceptance_reports_tap_failure() -> None:
    phone = TapFailPhone([Frame(np.zeros((10, 10, 3), dtype=np.uint8))])
    try:
        report = run_acceptance(phone, "mock", quick=True, output=None)
    finally:
        phone.close()
    assert report.ok is False
    assert any(item.status == "fail" for item in report.items)


def test_acceptance_report_files(tmp_path) -> None:
    phone = make_phone()
    try:
        report = run_acceptance(phone, "mock", quick=True, output=None)
    finally:
        phone.close()
    json_path, html_path = write_report(report, tmp_path / "accept")
    assert json_path.is_file()
    assert "Phone acceptance report" in html_path.read_text(encoding="utf-8")


def test_accept_cli(capsys, tmp_path) -> None:
    code = main(
        [
            "--mock",
            "accept",
            "--json",
            "--report-dir",
            str(tmp_path / "accept"),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["summary"]["pass"] >= 5
    assert (tmp_path / "accept" / "report.html").is_file()
