import pytest

from phone_console.vision import adb


def write_script(path, body: str):
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


UNAUTHORIZED = """printf 'List of devices attached\\nSER1\\tunauthorized\\n'"""

MIXED = (
    "printf 'List of devices attached\\nSER1\\tunauthorized\\n"
    "SER2\\tdevice product:x model:y device:z transport_id:1\\n'"
)


def test_resolve_adb_explicit_path(tmp_path) -> None:
    script = write_script(tmp_path / "adb", "exit 0")
    assert adb.resolve_adb(str(script)) == str(script)


def test_resolve_adb_missing_explicit_path() -> None:
    with pytest.raises(adb.AdbError, match="adb not found"):
        adb.resolve_adb("/nonexistent/adb")


def test_resolve_adb_reads_env(monkeypatch, tmp_path) -> None:
    script = write_script(tmp_path / "adb", "exit 0")
    monkeypatch.setenv("PHONE_ADB", str(script))
    assert adb.resolve_adb() == str(script)


def test_resolve_adb_prefers_repo_wrapper(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("PHONE_ADB", raising=False)
    monkeypatch.delenv("ADB", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    wrapper = write_script(tmp_path / "adb-wrapper", "exit 0")
    monkeypatch.setattr(adb, "_REPO_ADB_WRAPPER", wrapper)
    assert adb.resolve_adb() == str(wrapper)


def test_resolve_adb_falls_back_to_repo_platform_tools(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("PHONE_ADB", raising=False)
    monkeypatch.delenv("ADB", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(adb, "_REPO_ADB_WRAPPER", tmp_path / "missing-wrapper")
    if not adb._REPO_ADB.is_file():  # pragma: no cover - depends on checkout
        pytest.skip("repository platform-tools not present")
    assert adb.resolve_adb() == str(adb._REPO_ADB)


def test_resolve_adb_reports_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("PHONE_ADB", raising=False)
    monkeypatch.delenv("ADB", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(adb, "_REPO_ADB_WRAPPER", tmp_path / "missing-wrapper")
    monkeypatch.setattr(adb, "_REPO_ADB", tmp_path / "missing-adb")
    with pytest.raises(adb.AdbError, match="adb not found"):
        adb.resolve_adb()


def test_adb_devices_parses_states(tmp_path) -> None:
    script = write_script(tmp_path / "adb", MIXED)
    devices = adb.adb_devices(str(script))
    assert [device["serial"] for device in devices] == ["SER1", "SER2"]
    assert devices[0]["state"] == "unauthorized"
    assert devices[1]["state"] == "device"
    assert devices[1]["model"] == "y"


def test_pick_device_prefers_online(tmp_path) -> None:
    script = write_script(tmp_path / "adb", MIXED)
    devices = adb.adb_devices(str(script))
    assert adb.pick_device(devices)["serial"] == "SER2"
    assert adb.pick_device(devices, serial="SER1")["state"] == "unauthorized"
    assert adb.pick_device(devices, serial="nope") is None


def test_authorization_hints() -> None:
    assert "允许" in adb.authorization_hint("unauthorized")
    assert "offline" in adb.authorization_hint("offline")
    assert "没有检测到设备" in adb.authorization_hint(None)
    assert "已授权" in adb.authorization_hint("device")


def test_adb_status_unauthorized(tmp_path) -> None:
    script = write_script(tmp_path / "adb", UNAUTHORIZED)
    status = adb.adb_status(str(script))
    assert status["ok"] is False
    assert status["state"] == "unauthorized"
    assert "允许" in status["hint"]


def test_adb_status_online(tmp_path) -> None:
    script = write_script(tmp_path / "adb", MIXED)
    status = adb.adb_status(str(script))
    assert status["ok"] is True
    assert status["serial"] == "SER2"
    assert status["state"] == "device"


def test_adb_pair_success(tmp_path) -> None:
    script = write_script(
        tmp_path / "adb",
        'echo "Successfully paired to $2 [guid=adb-test]"',
    )
    result = adb.adb_pair("192.168.1.23:37123", "123456", adb=str(script))
    assert result["ok"] is True
    assert result["address"] == "192.168.1.23:37123"
    assert "Successfully paired" in result["output"]


def test_adb_pair_failure_reports_hint(tmp_path) -> None:
    script = write_script(
        tmp_path / "adb",
        'echo "Failed to pair: wrong pairing code" >&2; exit 1',
    )
    result = adb.adb_pair("192.168.1.23:37123", "000000", adb=str(script))
    assert result["ok"] is False
    assert "配对失败" in result["hint"]


def test_adb_connect_success(tmp_path) -> None:
    script = write_script(tmp_path / "adb", 'echo "connected to $2"')
    result = adb.adb_connect("192.168.1.23:41235", adb=str(script))
    assert result["ok"] is True
    assert result["address"] == "192.168.1.23:41235"


def test_adb_connect_failure(tmp_path) -> None:
    script = write_script(
        tmp_path / "adb",
        'echo "failed to connect to 192.168.1.23:41235" >&2; exit 1',
    )
    result = adb.adb_connect("192.168.1.23:41235", adb=str(script))
    assert result["ok"] is False
    assert "连接失败" in result["hint"]


def test_wait_for_device_times_out(tmp_path) -> None:
    script = write_script(tmp_path / "adb", UNAUTHORIZED)
    seen: list[str] = []
    status = adb.wait_for_device(
        str(script), timeout=0.2, interval=0.05, on_wait=lambda s: seen.append(s["hint"])
    )
    assert status["ok"] is False
    assert status.get("timed_out") is True
    assert seen, "on_wait should report the reason it is waiting"


def test_wait_for_device_returns_online(tmp_path) -> None:
    script = write_script(tmp_path / "adb", MIXED)
    status = adb.wait_for_device(str(script), timeout=1.0, interval=0.05)
    assert status["ok"] is True
    assert status["state"] == "device"


# --------------------------------------------------- USB-first eye selection
WIRELESS_ONLY = (
    "printf 'List of devices attached\\n"
    "adb-AJTLTEST00000001-AltZY5._adb-tls-connect._tcp\\tdevice\\n'"
)
USB_AND_WIRELESS = (
    "printf 'List of devices attached\\n"
    "adb-AJTLTEST00000001-AltZY5._adb-tls-connect._tcp\\tdevice\\n"
    "AJTLTEST00000001\\tdevice\\n'"
)


def test_is_usb_serial_distinguishes_wireless_links() -> None:
    assert adb.is_usb_serial("AJTLTEST00000001") is True
    assert adb.is_usb_serial("adb-XXX._adb-tls-connect._tcp") is False
    assert adb.is_usb_serial("192.168.1.149:34749") is False
    assert adb.is_usb_serial("") is False


def test_preferred_adb_serial_picks_usb_when_both_are_visible(tmp_path) -> None:
    script = write_script(tmp_path / "adb", USB_AND_WIRELESS)
    assert adb.preferred_adb_serial(str(script)) == "AJTLTEST00000001"


def test_preferred_adb_serial_leaves_wireless_alone(tmp_path) -> None:
    script = write_script(tmp_path / "adb", WIRELESS_ONLY)
    # No USB device: keep the previous "let adb pick its default" behaviour.
    assert adb.preferred_adb_serial(str(script)) is None


def test_preferred_adb_serial_honours_an_explicit_serial(tmp_path) -> None:
    script = write_script(tmp_path / "adb", USB_AND_WIRELESS)
    assert (
        adb.preferred_adb_serial(str(script), serial="adb-XXX._adb-tls-connect._tcp")
        == "adb-XXX._adb-tls-connect._tcp"
    )
