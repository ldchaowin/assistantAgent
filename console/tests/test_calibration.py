import pytest

pytest.importorskip("numpy")

from phone_console.calibration import (
    CalibrationSample,
    calibration_targets,
    map_hid,
    run_calibration,
    solve_axis,
    solve_calibration,
)
from phone_console.cli import main
from phone_console.virtual import VirtualPhone
from phone_console.vision.frame import Frame


def make_phone() -> VirtualPhone:
    return VirtualPhone([Frame(__import__("numpy").zeros((10, 10, 3), dtype="uint8"))])


def test_map_hid_matches_firmware_formula() -> None:
    assert map_hid(0, 1080, 0, 32767) == 0
    assert map_hid(1080, 1080, 0, 32767) == 32767
    assert map_hid(540, 1080, 0, 32767) == pytest.approx(16383.5)


def test_solve_axis_perfect_mapping() -> None:
    new_lo, new_hi = solve_axis(54, 54, 1025, 1025, 1080, 0, 32767)
    assert new_lo == pytest.approx(0, abs=2)
    assert new_hi == pytest.approx(32767, abs=2)


def test_solve_calibration_recovers_simulated_mapping() -> None:
    screen = (1080, 2400)
    old = (0, 32767, 0, 32767)
    a, b = 1.1, -500.0
    samples = []
    for target in calibration_targets(screen):
        observed_x = round(a * map_hid(target["x"], screen[0], old[0], old[1]) + b)
        observed_y = round(a * map_hid(target["y"], screen[1], old[2], old[3]) + b)
        samples.append(
            CalibrationSample(
                target["name"], target["x"], target["y"], observed_x, observed_y
            )
        )
    result = solve_calibration(screen, old, samples)
    assert result.valid is True
    for sample in samples:
        mapped_x = a * map_hid(
            sample.target_x, screen[0], result.new_cal[0], result.new_cal[1]
        ) + b
        mapped_y = a * map_hid(
            sample.target_y, screen[1], result.new_cal[2], result.new_cal[3]
        ) + b
        assert abs(mapped_x - sample.target_x) <= 2
        assert abs(mapped_y - sample.target_y) <= 2


def test_identical_observations_are_rejected() -> None:
    samples = [
        CalibrationSample("tl", 0, 0, 10, 10),
        CalibrationSample("tr", 100, 0, 10, 10),
        CalibrationSample("bl", 0, 100, 10, 10),
        CalibrationSample("br", 100, 100, 10, 10),
    ]
    with pytest.raises(ValueError):
        solve_calibration((100, 100), (0, 32767, 0, 32767), samples)


def test_run_calibration_auto_save() -> None:
    phone = make_phone()
    try:
        result = run_calibration(
            phone, auto=True, save=True, yes=True, output=None
        )
        assert result.valid is True
        assert result.saved is True
        assert tuple(phone.cal) == result.new_cal
    finally:
        phone.close()


def test_calibrate_cli(capsys) -> None:
    code = main(["--mock", "calibrate", "--auto", "--save", "--yes", "--json"])
    assert code == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["saved"] is True
