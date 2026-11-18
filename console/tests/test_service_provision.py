import json
import threading
import time

import pytest

from phone_console import provisioning
from phone_console.client import PhoneClient, PhoneClientError
from phone_console.factory import build_phone
from phone_console.service import create_server

TOKEN = "prov-token"


@pytest.fixture
def api():
    phone = build_phone(mock=True)
    server = create_server(phone, host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
    )
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield PhoneClient(f"http://{host}:{port}", token=TOKEN)
    finally:
        server.shutdown()
        server.server_close()
        phone.close()


def wait_state(api, wanted=("done", "failed"), timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = api.provision_status()
        if status["state"] in wanted:
            return status
        time.sleep(0.05)
    raise AssertionError(f"provisioning did not reach {wanted}: {status}")


def test_devices_lists_ble_targets(api, monkeypatch) -> None:
    monkeypatch.setattr(
        provisioning,
        "scan_devices",
        lambda timeout=8.0, prefix="PROV_": [
            {"name": "PROV_A1B2", "address": "AA:BB", "rssi": -30}
        ],
    )
    payload = api.provision_devices()
    assert payload["ok"] is True
    assert payload["devices"][0]["name"] == "PROV_A1B2"


def test_status_starts_idle(api) -> None:
    payload = api.provision_status()
    assert payload["ok"] is True
    assert payload["state"] == "idle"
    assert payload["running"] is False


def test_provision_job_succeeds(api, monkeypatch) -> None:
    seen = {}

    def fake_provision(ssid, password, device=None, pop=None, timeout=180.0):
        seen.update(ssid=ssid, device=device, pop=pop)
        return {
            "ok": True,
            "device": device,
            "ssid": ssid,
            "elapsed_ms": 12.3,
            "output": "Provisioning was successful",
        }

    monkeypatch.setattr(provisioning, "provision", fake_provision)
    started = api.start_provision("Office", "secret123", device="PROV_A1B2", pop="hand-A1B2")
    assert started["ok"] is True
    status = wait_state(api)
    assert status["state"] == "done"
    assert status["result"]["device"] == "PROV_A1B2"
    assert seen == {"ssid": "Office", "device": "PROV_A1B2", "pop": "hand-A1B2"}
    assert "password" not in json.dumps(status)


def test_provision_job_failure(api, monkeypatch) -> None:
    monkeypatch.setattr(
        provisioning,
        "provision",
        lambda *a, **k: {"ok": False, "output": "boom: apply failed"},
    )
    api.start_provision("Office", "secret123")
    status = wait_state(api)
    assert status["state"] == "failed"
    assert "boom" in status["result"]["output_tail"]


def test_provision_job_error(api, monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise provisioning.ProvisionError("bluetooth unavailable")

    monkeypatch.setattr(provisioning, "provision", explode)
    api.start_provision("Office", "secret123")
    status = wait_state(api)
    assert status["state"] == "failed"
    assert "bluetooth unavailable" in status["error"]


def test_second_job_is_busy(api, monkeypatch) -> None:
    release = threading.Event()

    def slow_provision(*args, **kwargs):
        release.wait(timeout=5)
        return {"ok": True, "output": "Provisioning was successful"}

    monkeypatch.setattr(provisioning, "provision", slow_provision)
    api.start_provision("Office", "secret123")
    with pytest.raises(PhoneClientError) as excinfo:
        api.start_provision("Office", "secret123")
    assert excinfo.value.code == "BUSY"
    release.set()
    assert wait_state(api)["state"] == "done"


def test_missing_password_is_rejected(api) -> None:
    with pytest.raises(PhoneClientError) as excinfo:
        api.start_provision("Office", "")
    assert excinfo.value.code == "BAD_REQUEST"


def test_arm_provisioning_forwards_to_device(api) -> None:
    payload = api.arm_provisioning()
    assert payload["ok"] is True


def test_arm_tolerates_lost_ack(monkeypatch) -> None:
    from phone_console.device import DeviceError
    from phone_console.factory import build_phone
    from phone_console.service import PhoneService

    phone = build_phone(mock=True)

    class BoomDevice:
        def request(self, *args, **kwargs):
            raise DeviceError("TIMEOUT", "no response")

        def close(self):
            return None

    phone.device = BoomDevice()
    service = PhoneService(phone)
    try:
        result = service.arm_provisioning()
        assert result["ok"] is True
        assert "warning" in result
    finally:
        phone.close()


def test_provision_retries_transient_ble_disconnect(api, monkeypatch) -> None:
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "output": "bleak.exc.BleakError: disconnected"}
        return {"ok": True, "output": "Provisioning was successful"}

    monkeypatch.setattr(provisioning, "provision", flaky)
    api.start_provision("Office", "secret123", device="PROV_A1B2")
    status = wait_state(api, timeout=8)
    assert status["state"] == "done"
    assert calls["n"] == 2
    assert status["result"]["attempts"] == 2
