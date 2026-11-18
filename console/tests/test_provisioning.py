import json
import sys

import pytest

from phone_console import provisioning
from phone_console.cli import main


def test_device_name_and_pop_helpers() -> None:
    assert provisioning.device_name_for("hand-A1B2") == "PROV_A1B2"
    assert provisioning.device_name_for("hand-1A2B") == "PROV_1A2B"
    assert provisioning.default_pop("hand-A1B2") == "hand-A1B2"
    assert provisioning.pop_from_device_name("PROV_A1B2") == "hand-A1B2"
    with pytest.raises(ValueError):
        provisioning.device_name_for("")


def test_parse_output() -> None:
    assert provisioning._parse_output("==== Provisioning was successful ====") is True
    assert provisioning._parse_output("---- Provisioning failed! ----") is False
    assert provisioning._parse_output("something else") is False


def fake_client(tmp_path, body: str, name: str = "esp_prov.py"):
    script = tmp_path / name
    script.write_text(
        "import sys\n"
        "print('args:', ' '.join(sys.argv[1:]))\n" + body + "\n",
        encoding="utf-8",
    )
    return script


def test_provision_success(tmp_path, monkeypatch) -> None:
    script = fake_client(tmp_path, "print('==== Provisioning was successful ====')")
    monkeypatch.setattr(provisioning, "ESP_PROV", script)
    result = provisioning.provision("Office", "secret123", device="PROV_A1B2", pop="hand-A1B2")
    assert result["ok"] is True
    assert result["device"] == "PROV_A1B2"
    assert "--service_name PROV_A1B2" in result["output"]
    assert "--pop hand-A1B2" in result["output"]
    assert "--ssid Office" in result["output"]


def test_provision_failure(tmp_path, monkeypatch) -> None:
    script = fake_client(tmp_path, "print('---- Provisioning failed! ----')")
    monkeypatch.setattr(provisioning, "ESP_PROV", script)
    result = provisioning.provision("Office", "secret123", device="PROV_A1B2")
    assert result["ok"] is False


def test_provision_timeout(tmp_path, monkeypatch) -> None:
    script = fake_client(tmp_path, "import time; time.sleep(5)")
    monkeypatch.setattr(provisioning, "ESP_PROV", script)
    with pytest.raises(provisioning.ProvisionError, match="timed out"):
        provisioning.provision("Office", "secret123", timeout=0.3)


def test_cli_devices(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        provisioning,
        "scan_devices",
        lambda timeout=6.0, prefix="PROV_": [
            {"name": "PROV_A1B2", "address": "AA:BB", "rssi": -40}
        ],
    )
    assert main(["wifi", "devices"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["devices"][0]["name"] == "PROV_A1B2"


def test_cli_provision_uses_default_pop(monkeypatch, capsys) -> None:
    captured = {}

    def fake_provision(ssid, password, device=None, pop=None, timeout=180.0):
        captured.update(ssid=ssid, password=password, device=device, pop=pop)
        return {"ok": True, "device": device, "ssid": ssid, "output": ""}

    monkeypatch.setattr(provisioning, "provision", fake_provision)
    assert (
        main(
            [
                "wifi",
                "provision",
                "--ssid",
                "Office",
                "--password",
                "secret123",
                "--device",
                "PROV_A1B2",
            ]
        )
        == 0
    )
    assert captured["pop"] == "hand-A1B2"
    assert captured["device"] == "PROV_A1B2"
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_cli_provision_multiple_devices(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        provisioning,
        "scan_devices",
        lambda timeout=6.0, prefix="PROV_": [
            {"name": "PROV_1111", "address": "A", "rssi": -40},
            {"name": "PROV_2222", "address": "B", "rssi": -50},
        ],
    )
    assert main(["wifi", "provision", "--ssid", "Office", "--password", "secret123"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert len(payload["devices"]) == 2


def test_provision_derives_pop_from_device_name(tmp_path, monkeypatch) -> None:
    script = fake_client(tmp_path, "print('==== Provisioning was successful ====')")
    monkeypatch.setattr(provisioning, "ESP_PROV", script)
    result = provisioning.provision("Office", "secret123", device="PROV_A1B2")
    assert result["ok"] is True
    assert "--pop hand-A1B2" in result["output"]
