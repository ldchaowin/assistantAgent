import json

import pytest

np = pytest.importorskip("numpy")

from phone_console.device import DeviceError
from phone_console.scenario import Scenario, ScenarioRunner
from phone_console.virtual import VirtualPhone
from phone_console.vision.frame import Frame
from phone_console.vision.ocr import OcrItem

DEFAULTS = {
    "timeout": 1.0,
    "retries": 1,
    "interval": 0.0,
    "min_score": 0.6,
    "mode": "contains",
    "retry_on_timeout": False,
}


class FakeOcrEngine:
    name = "fake"
    last_elapsed_ms = 1.0

    def __init__(self, texts=("Go",)):
        self.items = [
            OcrItem(text, 0.99, ((10, 10), (40, 10), (40, 30), (10, 30)))
            for text in texts
        ]

    def recognize(self, image):
        return list(self.items)


def make_scenario(steps, **overrides) -> Scenario:
    defaults = dict(DEFAULTS)
    defaults.update(overrides.pop("defaults", {}))
    return Scenario(
        name=overrides.pop("name", "test"),
        steps=steps,
        defaults=defaults,
        **overrides,
    )


def make_phone(cls=VirtualPhone, frames: int = 3, timeout: float = 0.2):
    return cls(
        [Frame(np.zeros((100, 100, 3), dtype=np.uint8)) for _ in range(frames)],
        timeout=timeout,
    )


class FaultAfterHello(VirtualPhone):
    def hello(self):
        response = super().hello()
        self.mock.inject_fault()
        return response


class DropAfterHello(VirtualPhone):
    def hello(self):
        response = super().hello()
        self.transport.drop_responses = 1
        return response


class BusyAfterHello(VirtualPhone):
    def hello(self):
        response = super().hello()
        self.mock.inject_busy(1)
        return response


class DisconnectAfterHello(VirtualPhone):
    def hello(self):
        response = super().hello()
        self.transport.write_error = ConnectionError("simulated disconnect")
        return response


def run(phone, scenario, tmp_path, engine=None):
    return ScenarioRunner(
        phone,
        scenario,
        engine=engine,
        run_dir=tmp_path / "run",
        output=None,
    ).run()


def test_tap_text_then_assert(tmp_path) -> None:
    scenario = make_scenario(
        [
            {"action": "tap_text", "text": "Go"},
            {"action": "assert_text", "text": "Done"},
        ]
    )
    phone = make_phone(frames=2)
    result = run(phone, scenario, tmp_path, FakeOcrEngine(["Go", "Done"]))
    assert result.ok is True
    assert len(result.steps) == 2
    assert phone.mock.taps == [(270, 480)]
    assert phone.index == 1
    report = (tmp_path / "run" / "report.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(report[0])["type"] == "step"
    assert json.loads(report[-1])["type"] == "summary"
    assert json.loads(report[-1])["ok"] is True


def test_assert_text_with_roi_offsets_to_full_frame(tmp_path) -> None:
    class CropRelativeOcr(FakeOcrEngine):
        def recognize(self, image):
            return [OcrItem("Go", 0.99, ((0, 0), (30, 0), (30, 20), (0, 20)))]

    scenario = make_scenario(
        [{"action": "assert_text", "text": "Go", "roi": [10, 10, 50, 50]}]
    )
    result = run(make_phone(), scenario, tmp_path, CropRelativeOcr())
    assert result.ok is True
    assert result.steps[0].detail["center"] == [25.0, 20.0]


def test_tap_text_not_found_retries_and_saves_artifact(tmp_path) -> None:
    scenario = make_scenario([{"action": "tap_text", "text": "Missing"}])
    phone = make_phone()
    result = run(phone, scenario, tmp_path, FakeOcrEngine(["Go"]))
    assert result.ok is False
    assert result.steps[0].attempts == 2
    assert "TEXT_NOT_FOUND" in result.steps[0].error
    assert (tmp_path / "run" / "failure-01.png").is_file()
    assert (tmp_path / "run" / "failure-01.json").is_file()


def test_wait_for_text_timeout(tmp_path) -> None:
    scenario = make_scenario(
        [{"action": "wait_for_text", "text": "Missing", "timeout": 0.05, "interval": 0.01}]
    )
    result = run(make_phone(), scenario, tmp_path, FakeOcrEngine([]))
    assert result.ok is False
    assert "TEXT_TIMEOUT" in result.steps[0].error


def test_assert_text_failure_code(tmp_path) -> None:
    scenario = make_scenario(
        [{"action": "assert_text", "text": "Missing", "timeout": 0.01}]
    )
    result = run(make_phone(), scenario, tmp_path, FakeOcrEngine([]))
    assert result.ok is False
    assert "ASSERT_FAILED" in result.steps[0].error


def test_retry_after_timeout_when_enabled(tmp_path) -> None:
    scenario = make_scenario(
        [{"action": "tap_text", "text": "Go", "retry_on_timeout": True}]
    )
    phone = make_phone(DropAfterHello, frames=2, timeout=0.05)
    result = run(phone, scenario, tmp_path, FakeOcrEngine(["Go"]))
    assert result.ok is True
    assert result.steps[0].attempts == 2
    assert len(phone.mock.taps) == 2
    assert phone.index == 1


def test_timeout_is_not_retried_by_default(tmp_path) -> None:
    scenario = make_scenario([{"action": "tap_text", "text": "Go"}])
    phone = make_phone(DropAfterHello, frames=2, timeout=0.05)
    result = run(phone, scenario, tmp_path, FakeOcrEngine(["Go"]))
    assert result.ok is False
    assert result.steps[0].attempts == 1
    assert "TIMEOUT" in result.steps[0].error
    # The first tap did execute; retrying could double-tap, so we stop.
    assert len(phone.mock.taps) == 1


def test_retry_after_busy(tmp_path) -> None:
    scenario = make_scenario([{"action": "tap_text", "text": "Go"}])
    phone = make_phone(BusyAfterHello, frames=2)
    result = run(phone, scenario, tmp_path, FakeOcrEngine(["Go"]))
    assert result.ok is True
    assert result.steps[0].attempts == 2
    assert len(phone.mock.taps) == 1


def test_fault_is_not_retried(tmp_path) -> None:
    scenario = make_scenario([{"action": "tap_text", "text": "Go"}])
    phone = make_phone(FaultAfterHello, frames=2)
    result = run(phone, scenario, tmp_path, FakeOcrEngine(["Go"]))
    assert result.ok is False
    assert result.steps[0].attempts == 1
    assert "NOT_READY" in result.steps[0].error
    assert phone.mock.taps == []


def test_disconnect_is_not_retried(tmp_path) -> None:
    scenario = make_scenario([{"action": "tap_text", "text": "Go"}])
    phone = make_phone(DisconnectAfterHello, frames=2)
    result = run(phone, scenario, tmp_path, FakeOcrEngine(["Go"]))
    assert result.ok is False
    assert result.steps[0].attempts == 1
    assert "DISCONNECT" in result.steps[0].error


def test_swipe_key_sleep_without_vision(tmp_path) -> None:
    scenario = make_scenario(
        [
            {"action": "swipe", "x1": 1, "y1": 2, "x2": 3, "y2": 4, "ms": 100},
            {"action": "key", "code": "BACK"},
            {"action": "sleep", "ms": 1},
        ]
    )
    phone = make_phone(frames=4)
    result = run(phone, scenario, tmp_path)
    assert result.ok is True
    assert phone.mock.swipes == [(1, 2, 3, 4, 100)]
    assert phone.mock.keys == ["BACK"]


def test_text_action(tmp_path) -> None:
    scenario = make_scenario([{"action": "text", "value": "hello"}])
    phone = make_phone(frames=2)
    result = run(phone, scenario, tmp_path)
    assert result.ok is True
    assert phone.mock.texts == ["hello"]
    assert phone.index == 1


def test_direct_tap(tmp_path) -> None:
    scenario = make_scenario([{"action": "tap", "x": 12, "y": 34}])
    phone = make_phone()
    result = run(phone, scenario, tmp_path)
    assert result.ok is True
    assert phone.mock.taps == [(12, 34)]


def test_screen_mismatch_warns(tmp_path) -> None:
    scenario = make_scenario([{"action": "sleep", "ms": 1}], screen=(100, 100))
    result = run(make_phone(), scenario, tmp_path)
    assert result.ok is True
    assert result.warnings


def test_hello_failure_is_reported(tmp_path) -> None:
    class BrokenPhone:
        screen = (1080, 2400)

        def hello(self):
            raise DeviceError("FAULT", "boom")

        def close(self):
            pass

    scenario = make_scenario([{"action": "sleep", "ms": 1}])
    result = ScenarioRunner(
        BrokenPhone(), scenario, run_dir=tmp_path / "run", output=None
    ).run()
    assert result.ok is False
    assert "HELLO_FAILED" in result.error
    assert result.steps == []


def test_scenario_copy_is_saved(tmp_path) -> None:
    scenario = make_scenario([{"action": "sleep", "ms": 1}])
    run(make_phone(), scenario, tmp_path)
    assert (tmp_path / "run" / "scenario.json").is_file()
