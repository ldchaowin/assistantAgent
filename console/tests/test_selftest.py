from phone_console.device import Device
from phone_console.selftest import grid_points, replay, run_grid, run_stress
from phone_console.transport import MockTransport


def test_grid_points_are_inside_margins() -> None:
    points = grid_points(5, 5, 1000, 2000, margin=0.1)
    assert len(points) == 25
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    assert min(xs) >= 100 and max(xs) <= 900
    assert min(ys) >= 200 and max(ys) <= 1800


def test_run_grid_taps_every_point() -> None:
    device = Device(MockTransport())
    device.hello()
    points = run_grid(device, 2, 2, dwell=0, output=lambda _: None)
    assert len(points) == 4
    assert len(device.transport.device.taps) == 4
    device.close()


def test_run_stress_reports_success() -> None:
    device = Device(MockTransport())
    device.hello()
    result = run_stress(device, n=5, interval=0, output=lambda _: None)
    assert result["ok"] == 5
    assert result["fail"] == 0
    assert result["success_rate"] == 1.0
    device.close()


def test_replay_filters_non_actions(tmp_path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_text(
        '{"cmd":"tap","x":10,"y":20}\n'
        '{"cmd":"status"}\n'
        '{"cmd":"key","code":"ENTER"}\n',
        encoding="utf-8",
    )
    device = Device(MockTransport())
    device.hello()
    records = replay(device, path)
    assert len(records) == 2
    assert device.transport.device.taps == [(10, 20)]
    assert device.transport.device.keys == ["ENTER"]
    device.close()
