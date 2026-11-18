import json

import pytest

from phone_console.cli import main
from phone_console.device import Device, DeviceError
from phone_console.transport import MockTransport


def new_device() -> Device:
    device = Device(MockTransport())
    device.hello()
    return device


def test_hello_reports_wifi_state() -> None:
    device = new_device()
    try:
        hello = device.hello()
        assert hello["wifi"]["state"] == "off"
        assert hello["wifi"]["port"] == 9000
    finally:
        device.close()


def test_wifi_set_connect_status_clear() -> None:
    device = new_device()
    try:
        response = device.wifi_set("Office", "password123")
        assert response["ok"] is True
        event = device.wait_event("wifi", timeout=2.0)
        assert event["wifi"]["state"] == "connected"
        assert event["wifi"]["ssid"] == "Office"
        assert event["wifi"]["ip"] == "192.168.1.50"
        assert device.wifi_status()["wifi"]["ssid"] == "Office"
        assert device.wifi_clear()["ok"] is True
        assert device.wifi_status()["wifi"]["state"] == "off"
    finally:
        device.close()


def test_wifi_rejects_short_password() -> None:
    device = new_device()
    try:
        with pytest.raises(DeviceError, match="BAD_VALUE"):
            device.wifi_set("Office", "short")
    finally:
        device.close()


def test_wifi_rejects_long_ssid() -> None:
    device = new_device()
    try:
        with pytest.raises(DeviceError, match="BAD_VALUE"):
            device.wifi_set("x" * 33, "password123")
    finally:
        device.close()


def test_wifi_cli_set(capsys) -> None:
    code = main(["--mock", "wifi", "set", "Office", "password123"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["wifi"]["state"] == "connected"
    assert payload["events"][-1]["state"] == "connected"


def test_wifi_cli_status(capsys) -> None:
    assert main(["--mock", "wifi", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wifi"]["state"] == "off"


def test_wifi_cli_clear(capsys) -> None:
    assert main(["--mock", "wifi", "clear"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["wifi"]["state"] == "off"


def test_wifi_cli_bad_password_exits_2(capsys) -> None:
    assert main(["--mock", "wifi", "set", "Office", "short"]) == 2
    captured = capsys.readouterr()
    assert "BAD_VALUE" in captured.err
