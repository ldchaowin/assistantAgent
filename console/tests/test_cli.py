from phone_console.cli import main


def test_status_with_mock(capsys) -> None:
    assert main(["--mock", "status"]) == 0
    out = capsys.readouterr().out
    assert '"state": "READY"' in out


def test_tap_with_mock(capsys) -> None:
    assert main(["--mock", "tap", "100", "200"]) == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out


def test_bad_cal_returns_error(capsys) -> None:
    assert main(["--mock", "config", "--cal", "1,2,3"]) == 2
    assert "cal must" in capsys.readouterr().err


def test_selftest_grid_with_mock(capsys) -> None:
    assert main(["--mock", "selftest", "--cols", "2", "--rows", "2", "--dwell", "0"]) == 0
    out = capsys.readouterr().out
    assert "grid 2x2" in out
