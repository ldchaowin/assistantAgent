import pytest

from phone_console.device import Device, DeviceError, DeviceTimeout
from phone_console.log import JsonlLogger
from phone_console.transport import MockTransport


@pytest.fixture
def device() -> Device:
    dev = Device(MockTransport(), timeout=0.3)
    yield dev
    dev.close()


class Silent:
    def write(self, data: bytes) -> None:
        pass

    def readline(self, timeout: float) -> bytes | None:
        return None

    def close(self) -> None:
        pass


def test_hello_updates_screen(device: Device) -> None:
    event = device.hello()
    assert event["evt"] == "ready"
    assert event["fw"] == "0.3.1-mock"
    assert device.screen == (1080, 2400)


def test_tap(device: Device) -> None:
    device.hello()
    response = device.tap(100, 200)
    assert response["ok"] is True
    assert device.transport.device.taps == [(100, 200)]


def test_tap_out_of_range(device: Device) -> None:
    device.hello()
    with pytest.raises(DeviceError) as exc:
        device.tap(99999, 10)
    assert exc.value.code == "OUT_OF_RANGE"


def test_configure_and_save(device: Device) -> None:
    device.hello()
    response = device.configure(screen=(1440, 3200), cal=(10, 32760, 5, 32760))
    assert response["ok"] is True
    assert device.screen == (1440, 3200)
    assert device.cal == (10, 32760, 5, 32760)
    assert device.save()["ok"] is True


def test_swipe(device: Device) -> None:
    device.hello()
    response = device.swipe(100, 100, 100, 900, ms=300)
    assert response["ok"] is True
    assert device.transport.device.swipes == [(100, 100, 100, 900, 300)]


def test_key_and_text(device: Device) -> None:
    device.hello()
    assert device.key("enter")["ok"] is True
    assert device.transport.device.keys == ["ENTER"]
    assert device.text("Abc123")["ok"] is True
    assert device.transport.device.texts == ["Abc123"]


def test_key_rejects_unknown(device: Device) -> None:
    device.hello()
    with pytest.raises(DeviceError) as exc:
        device.key("NOPE")
    assert exc.value.code == "BAD_VALUE"


def test_ping_and_status(device: Device) -> None:
    device.hello()
    pong = device.ping()
    assert pong["evt"] == "pong"
    assert pong["rtt_ms"] >= 0
    status = device.status()
    assert status["state"] == "READY"


def test_stop(device: Device) -> None:
    device.hello()
    assert device.stop()["ok"] is True


def test_timeout() -> None:
    dev = Device(Silent(), timeout=0.05)  # type: ignore[arg-type]
    with pytest.raises(DeviceTimeout):
        dev.tap(1, 1)
    dev.close()


def test_jsonl_logging(tmp_path, device: Device) -> None:
    log_path = tmp_path / "run.jsonl"
    logger = JsonlLogger(log_path)
    device.logger = logger
    device.hello()
    device.tap(10, 20)
    device.close()
    text = log_path.read_text(encoding="utf-8")
    assert '"cmd": "tap"' in text
    assert '"ok": true' in text


def test_fault_requires_hello(device: Device) -> None:
    device.hello()
    device.transport.device.state = "FAULT"  # type: ignore[attr-defined]
    with pytest.raises(DeviceError) as exc:
        device.tap(1, 1)
    assert exc.value.code == "NOT_READY"
    device.hello()
    assert device.tap(1, 1)["ok"] is True


class NoisyTransport:
    """Serial-like transport that emits a boot log line before the response."""

    def __init__(self) -> None:
        from phone_console.transport import MockTransport

        self.inner = MockTransport()
        self._noise = [b"ESP-ROM:esp32s3-20210327\r\n", b"\r\n"]

    def write(self, data: bytes) -> None:
        self.inner.write(data)

    def readline(self, timeout: float):
        if self._noise:
            return self._noise.pop(0)
        return self.inner.readline(timeout)

    def close(self) -> None:
        self.inner.close()


def test_device_skips_boot_noise() -> None:
    from phone_console.device import Device

    device = Device(NoisyTransport())
    try:
        hello = device.hello()
        assert hello["fw"] == "0.3.1-mock"
    finally:
        device.close()


def test_hello_retries_once_after_timeout() -> None:
    from phone_console.device import Device, DeviceTimeout

    class FlakyTransport:
        def __init__(self):
            self.reconnects = 0

        def write(self, data):
            return None

        def readline(self, timeout):
            return None

        def reconnect(self):
            self.reconnects += 1

        def close(self):
            return None

    transport = FlakyTransport()
    device = Device(transport)
    calls = {"n": 0}

    def fake_event(cmd, event_name, timeout, **params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeviceTimeout("lost ready")
        return {"evt": "ready", "fw": "0.3.2"}

    device._event = fake_event
    event = device.hello()
    assert event["fw"] == "0.3.2"
    assert transport.reconnects == 1
    assert calls["n"] == 2
