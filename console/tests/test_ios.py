"""iOS eye/hand tests: discovery, AVFoundation capture, CLI, service, doctor."""
from __future__ import annotations

import io
import json
import plistlib
import time
import socket
import struct
import threading
from pathlib import Path

import pytest

numpy = pytest.importorskip("numpy")
PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from phone_console.device import Device, DeviceError  # noqa: E402
from phone_console.ios import (  # noqa: E402
    ABS_FIRMWARE_HINT,
    IosPhone,
    configure_hand_for_ios,
    go_back,
    go_home,
    hand_checks,
    show_app_switcher,
)
from phone_console.service import PhoneService  # noqa: E402
from phone_console.transport import MockTransport  # noqa: E402
from phone_console.vision import ios as ios_mod  # noqa: E402
from phone_console.vision.frame import Frame  # noqa: E402
from phone_console.vision.ios import (  # noqa: E402
    IosError,
    IosMjpegSource,
    IosStillSource,
    capture_once,
    ios_status,
    list_capture_devices,
    open_ios_source,
    parse_ffmpeg_devices,
    pick_capture_device,
    tool_availability,
    usbmux_list,
    wait_for_ios,
)


# ---------------------------------------------------------------- fixtures
def make_frame(width: int = 32, height: int = 64, color=(200, 10, 10)) -> Frame:
    image = numpy.zeros((height, width, 3), dtype=numpy.uint8)
    image[:, :] = color
    return Frame(image, source="test")


def png_bytes(width: int = 24, height: int = 48) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 200, 10)).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(width: int = 32, height: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 10, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


class FakeSource:
    """Minimal frame source with a fixed frame size (no capture backend)."""

    name = "ios:avfoundation:iPhone"

    def __init__(self) -> None:
        self.frame = make_frame()
        self._frame_size = self.frame.size

    @property
    def frame_size(self):
        return self._frame_size

    def grab(self):
        return self.frame

    def stats(self):
        return {"name": self.name, "backend": "avfoundation", "frames": 3}

    def close(self) -> None:
        return None


# ------------------------------------------------------------- device list
def test_parse_ffmpeg_devices_separates_video_and_audio() -> None:
    output = """[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] Capture screen 0
[AVFoundation indev @ 0x1] [1] 李登超的 iPhone
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] Mac mini Microphone
[AVFoundation indev @ 0x1] [1] 李登超的 iPhone Microphone
[in#0 @ 0x1] Error opening input: Input/output error
"""
    devices = parse_ffmpeg_devices(output)
    assert devices == [
        {"index": 0, "name": "Capture screen 0"},
        {"index": 1, "name": "李登超的 iPhone"},
    ]


def test_parse_ffmpeg_devices_ignores_unrelated_output() -> None:
    assert parse_ffmpeg_devices("ffmpeg version 9\n[in#0] Error\n") == []


def test_pick_capture_device_prefers_iphone_name() -> None:
    devices = [
        {"index": 0, "name": "Capture screen 0"},
        {"index": 1, "name": "Capture screen 1"},
        {"index": 2, "name": "李登超的 iPhone"},
    ]
    assert pick_capture_device(devices) == {"index": 2, "name": "李登超的 iPhone"}


def test_pick_capture_device_accepts_second_screen_device() -> None:
    devices = [
        {"index": 0, "name": "Capture screen 0"},
        {"index": 1, "name": "Capture screen 1"},
    ]
    assert pick_capture_device(devices) == {"index": 1, "name": "Capture screen 1"}


def test_pick_capture_device_ambiguous_screens_returns_none() -> None:
    devices = [
        {"index": 0, "name": "Capture screen 0"},
        {"index": 1, "name": "Capture screen 1"},
        {"index": 2, "name": "Capture screen 2"},
    ]
    assert pick_capture_device(devices) is None


def test_pick_capture_device_explicit_index_and_name() -> None:
    devices = [
        {"index": 0, "name": "Capture screen 0"},
        {"index": 1, "name": "Capture screen 1"},
    ]
    assert pick_capture_device(devices, 1) == devices[1]
    assert pick_capture_device(devices, "screen 1") == devices[1]
    assert pick_capture_device(devices, "nope") is None


def test_pick_capture_device_single_external_device() -> None:
    devices = [{"index": 0, "name": "FaceTime HD Camera"}]
    assert pick_capture_device(devices) == devices[0]


# ----------------------------------------------------------------- usbmuxd
def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def start_fake_usbmuxd(path: Path, devices: list[dict]) -> threading.Thread:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def run() -> None:
        try:
            conn, _ = server.accept()
            header = _recv_exact(conn, 16)
            length = struct.unpack("<IIII", header)[0]
            _recv_exact(conn, length - 16)
            payload = plistlib.dumps(
                {"MessageType": "Result", "DeviceList": devices}
            )
            reply = struct.pack("<IIII", 16 + len(payload), 1, 8, 1) + payload
            conn.sendall(reply)
            conn.close()
        finally:
            server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_usbmux_list_parses_devices() -> None:
    import os
    import tempfile

    tmpdir = Path(tempfile.mkdtemp(prefix="ios-"))
    socket_path = tmpdir / "usbmuxd.sock"
    thread = start_fake_usbmuxd(
        socket_path,
        [
            {
                "DeviceID": 7,
                "Properties": {
                    "ConnectionType": "USB",
                    "DeviceID": 7,
                    "ProductID": 4776,
                    "SerialNumber": "00008120-000A1234ABCD",
                    "LocationID": 123,
                },
            }
        ],
    )
    try:
        devices = usbmux_list(str(socket_path), timeout=3.0)
    finally:
        thread.join(timeout=2.0)
        socket_path.unlink(missing_ok=True)
        tmpdir.rmdir()
    assert devices == [
        {
            "device_id": 7,
            "serial": "00008120-000A1234ABCD",
            "connection_type": "USB",
            "product_id": 4776,
            "location_id": 123,
        }
    ]


def test_usbmux_list_missing_socket_has_hint(tmp_path: Path) -> None:
    with pytest.raises(IosError) as info:
        usbmux_list(str(tmp_path / "missing.sock"), timeout=0.5)
    assert "usbmuxd" in str(info.value)


# ------------------------------------------------------------------ status
def _fake_native(ok: bool = False, error: str | None = None) -> dict:
    native = {
        "ok": ok,
        "built": True,
        "helper": "IOSEye",
        "mode": "probe",
        "camera_auth": 3 if ok else 0,
        "device": "iPhone" if ok else None,
        "width": 1170 if ok else 0,
        "height": 2532 if ok else 0,
        "error": error,
    }
    if error:
        native["hint"] = "允许 IOSEye 访问相机"
    return native


def _disable_native_probe(monkeypatch) -> None:
    monkeypatch.setattr(ios_mod, "ios_eye_probe", lambda timeout=25.0: _fake_native(False, "test: no helper"))


def _fake_tools(**overrides):
    tools = {
        "platform": "darwin",
        "native_eye": {"helper": "IOSEye", "app": "/tmp/none", "built": True,
                       "clang": "/usr/bin/clang"},
        "ffmpeg": "/opt/homebrew/bin/ffmpeg",
        "pymobiledevice3": None,
        "idevicescreenshot": None,
        "usbmuxd": True,
    }
    tools.update(overrides)
    return tools


def test_ios_status_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        ios_mod, "tool_availability", lambda ffmpeg=None: _fake_tools()
    )
    monkeypatch.setattr(ios_mod, "ios_eye_probe", lambda timeout=25.0: _fake_native(True))
    monkeypatch.setattr(
        ios_mod,
        "usbmux_list",
        lambda **kwargs: [{"device_id": 1, "serial": "UDID", "connection_type": "USB"}],
    )
    monkeypatch.setattr(
        ios_mod,
        "list_capture_devices",
        lambda ffmpeg=None, timeout=12.0: [
            {"index": 0, "name": "Capture screen 0"},
            {"index": 1, "name": "iPhone"},
        ],
    )
    monkeypatch.setattr(ios_mod, "pymobiledevice3_devices", lambda timeout=8.0: [])
    status = ios_status(probe_capture=True)
    assert status["ok"] is True
    assert status["state"] == "ready"
    assert status["platform"] == "ios"
    assert status["capture_device"]["index"] == 1
    assert status["serial"] == "UDID"
    assert status["hint"]


def test_ios_status_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        ios_mod, "tool_availability", lambda ffmpeg=None: _fake_tools()
    )
    _disable_native_probe(monkeypatch)
    monkeypatch.setattr(ios_mod, "usbmux_list", lambda **kwargs: [])
    monkeypatch.setattr(
        ios_mod, "list_capture_devices", lambda *a, **k: []
    )
    monkeypatch.setattr(ios_mod, "pymobiledevice3_devices", lambda timeout=8.0: [])
    status = ios_status(probe_capture=True)
    assert status["ok"] is False
    assert status["state"] == "missing"
    assert "信任" in status["hint"]


def test_ios_status_connected_but_no_capture(monkeypatch) -> None:
    monkeypatch.setattr(
        ios_mod, "tool_availability", lambda ffmpeg=None: _fake_tools()
    )
    _disable_native_probe(monkeypatch)
    monkeypatch.setattr(
        ios_mod,
        "usbmux_list",
        lambda **kwargs: [{"device_id": 1, "serial": "UDID", "connection_type": "USB"}],
    )
    monkeypatch.setattr(
        ios_mod,
        "list_capture_devices",
        lambda *a, **k: [{"index": 0, "name": "Capture screen 0"}],
    )
    monkeypatch.setattr(ios_mod, "pymobiledevice3_devices", lambda timeout=8.0: [])
    status = ios_status(probe_capture=True)
    assert status["state"] == "connected"
    assert status["ok"] is False
    assert "信任" in status["hint"]


def test_ios_status_enriches_device_info(monkeypatch) -> None:
    monkeypatch.setattr(
        ios_mod, "tool_availability", lambda ffmpeg=None: _fake_tools()
    )
    _disable_native_probe(monkeypatch)
    monkeypatch.setattr(
        ios_mod,
        "usbmux_list",
        lambda **kwargs: [{"device_id": 1, "serial": "UDID", "connection_type": "USB"}],
    )
    monkeypatch.setattr(
        ios_mod,
        "pymobiledevice3_devices",
        lambda timeout=8.0: [
            {
                "serial": "UDID",
                "name": "李登超的 iPhone",
                "product_type": "iPhone17,1",
                "ios_version": "26.1",
            }
        ],
    )
    monkeypatch.setattr(
        ios_mod,
        "list_capture_devices",
        lambda ffmpeg=None, timeout=12.0: [{"index": 1, "name": "iPhone"}],
    )
    status = ios_status()
    assert status["devices"][0]["name"] == "李登超的 iPhone"
    assert status["devices"][0]["ios_version"] == "26.1"


def test_wait_for_ios_times_out(monkeypatch) -> None:
    missing = {
        "ok": False,
        "state": "missing",
        "hint": "no iphone",
        "devices": [],
        "capture_devices": [],
        "capture_device": None,
    }
    monkeypatch.setattr(ios_mod, "ios_status", lambda **kwargs: dict(missing))
    waits: list[dict] = []
    status = wait_for_ios(timeout=0.05, interval=0.01, on_wait=waits.append)
    assert status["timed_out"] is True
    assert waits


def test_wait_for_ios_returns_when_ready(monkeypatch) -> None:
    calls = {"n": 0}
    ready = {
        "ok": True,
        "state": "ready",
        "hint": "ok",
        "devices": [],
        "capture_devices": [],
        "capture_device": {"index": 1, "name": "iPhone"},
    }

    def fake_status(**kwargs):
        calls["n"] += 1
        status = dict(ready)
        status["ok"] = calls["n"] >= 2
        return status

    monkeypatch.setattr(ios_mod, "ios_status", fake_status)
    status = wait_for_ios(timeout=2.0, interval=0.01)
    assert status["ok"] is True
    assert calls["n"] >= 2


# ---------------------------------------------------------- live AVFoundation
def make_fake_ffmpeg(tmp_path: Path, frames: list[bytes], mode: str = "ok") -> str:
    script = tmp_path / "fake-ffmpeg"
    if mode == "fail":
        body = (
            "import sys\n"
            "sys.stderr.write('Input/output error: device not found\\n')\n"
            "sys.stderr.flush()\n"
            "sys.exit(3)\n"
        )
    elif mode == "once":
        import base64

        parts = ["import sys, time, base64\n"]
        for frame in frames:
            encoded = base64.b64encode(frame).decode("ascii")
            parts.append(f"_f = base64.b64decode({encoded!r})\n")
            parts.append("sys.stdout.buffer.write(_f)\n")
            parts.append("sys.stdout.buffer.flush()\n")
            parts.append("time.sleep(0.05)\n")
        parts.append("sys.exit(0)\n")
        body = "".join(parts)
    else:
        import base64

        parts = ["import sys, time, base64\n"]
        for frame in frames:
            encoded = base64.b64encode(frame).decode("ascii")
            parts.append(f"_f = base64.b64decode({encoded!r})\n")
            parts.append("sys.stdout.buffer.write(_f)\n")
            parts.append("sys.stdout.buffer.flush()\n")
            parts.append("time.sleep(0.15)\n")
        parts.append("time.sleep(10)\n")
        body = "".join(parts)
    script.write_text("#!/usr/bin/env python3\n" + body)
    script.chmod(0o755)
    return str(script)


def test_ios_mjpeg_source_decodes_frames(tmp_path: Path) -> None:
    ffmpeg = make_fake_ffmpeg(tmp_path, [jpeg_bytes(32, 64), jpeg_bytes(32, 64)])
    source = IosMjpegSource(
        {"index": 1, "name": "iPhone"},
        ffmpeg=ffmpeg,
        startup_timeout=6.0,
    )
    try:
        first = source.grab()
        assert first.size == (32, 64)
        second = source.wait_newer(first.ts, timeout=3.0)
        assert second.ts > first.ts
        stats = source.stats()
        assert stats["backend"] == "avfoundation"
        assert stats["device"]["name"] == "iPhone"
        assert stats["frames"] >= 2
        assert stats["running"] is True
        assert "avfoundation" in source.name
        assert source.latest() is not None
    finally:
        source.close()


def test_ios_mjpeg_source_restarts_after_ffmpeg_exit(tmp_path: Path) -> None:
    ffmpeg = make_fake_ffmpeg(tmp_path, [jpeg_bytes(32, 64)], mode="once")
    source = IosMjpegSource(
        {"index": 1, "name": "iPhone"}, ffmpeg=ffmpeg, startup_timeout=4.0
    )
    try:
        first = source.grab()
        assert first.size == (32, 64)
        # The child exits after one frame; wait for that, then the next grab
        # respawns ffmpeg and waits for a fresh frame.
        import time

        deadline = time.monotonic() + 3.0
        while source.stats()["running"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert source.stats()["running"] is False
        second = source.grab()
        assert second.ts >= first.ts
        assert source.stats()["restarts"] >= 1
    finally:
        source.close()


def test_ios_mjpeg_source_reports_startup_failure(tmp_path: Path) -> None:
    ffmpeg = make_fake_ffmpeg(tmp_path, [], mode="fail")
    source = IosMjpegSource({"index": 1, "name": "iPhone"}, ffmpeg=ffmpeg, startup_timeout=2.0)
    try:
        with pytest.raises(IosError) as info:
            source.grab()
    finally:
        source.close()
    message = str(info.value)
    assert "iPhone" in message
    assert "画面" in message
    assert "Input/output error" in message


def test_open_ios_source_falls_back_to_still(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise IosError("no capture device")

    monkeypatch.setattr(ios_mod, "ios_eye_built", lambda: False)
    monkeypatch.setattr(ios_mod, "resolve_clang", lambda: None)
    monkeypatch.setattr(ios_mod, "IosMjpegSource", boom)
    source = open_ios_source(prefer="auto")
    try:
        assert isinstance(source, IosStillSource)
    finally:
        source.close()


# ------------------------------------------------------------- still capture
def test_ios_still_source_falls_back(monkeypatch) -> None:
    from phone_console.vision import ios as module

    monkeypatch.setattr(
        module,
        "capture_pymobiledevice3",
        lambda udid=None, timeout=30.0: (_ for _ in ()).throw(IosError("devmode off")),
    )
    monkeypatch.setattr(
        module,
        "capture_idevicescreenshot",
        lambda udid=None, timeout=20.0: png_bytes(24, 48),
    )
    source = module.IosStillSource(backends=["pymobiledevice3", "idevicescreenshot"])
    frame = source.grab()
    assert frame.size == (24, 48)
    assert source.name == "ios:still:idevicescreenshot"
    assert source.frame_size == (24, 48)
    assert "pymobiledevice3" in (source.stats()["backends"])


def test_ios_still_source_aggregates_errors(monkeypatch) -> None:
    from phone_console.vision import ios as module

    monkeypatch.setattr(
        module,
        "capture_pymobiledevice3",
        lambda udid=None, timeout=30.0: (_ for _ in ()).throw(IosError("no devmode")),
    )
    monkeypatch.setattr(
        module,
        "capture_idevicescreenshot",
        lambda udid=None, timeout=20.0: (_ for _ in ()).throw(IosError("no ddi")),
    )
    source = module.IosStillSource(backends=["pymobiledevice3", "idevicescreenshot"])
    with pytest.raises(IosError) as info:
        source.grab()
    message = str(info.value)
    assert "no devmode" in message
    assert "no ddi" in message
    assert "iOS 截图失败" in message


def test_capture_once_reports_backend(monkeypatch) -> None:
    from phone_console.vision import ios as module

    monkeypatch.setattr(
        module, "capture_pymobiledevice3", lambda udid=None, timeout=30.0: png_bytes()
    )
    frame, backend = capture_once(backend="pymobiledevice3")
    assert backend == "pymobiledevice3"
    assert frame.size == (24, 48)


def test_tool_availability_shape() -> None:
    tools = tool_availability()
    assert set(tools) >= {"ffmpeg", "pymobiledevice3", "idevicescreenshot", "usbmuxd"}


# --------------------------------------------------------------- hand / phone
def test_ios_phone_screen_comes_from_frame() -> None:
    phone = IosPhone(Device(MockTransport()), FakeSource())
    try:
        assert phone.platform == "ios"
        assert phone.screen == (32, 64)
    finally:
        phone.close()


def test_ios_phone_warm_pointer_then_tap() -> None:
    device = Device(MockTransport())
    phone = IosPhone(device, FakeSource())
    try:
        result = phone.tap(10, 20)
        assert result["ok"] is True
        commands = [call["cmd"] for call in device.transport.device.calls]
        assert commands[0] == "move"
        assert "tap" in commands
        assert device.transport.device.moves == [(0, 0)]
        # Only warmed once.
        phone.tap(11, 21)
        assert [call["cmd"] for call in device.transport.device.calls].count("move") == 1
    finally:
        phone.close()


def test_ios_gestures_use_screen_edges() -> None:
    device = Device(MockTransport())
    phone = IosPhone(device, FakeSource())
    try:
        go_home(phone)
        go_back(phone)
        show_app_switcher(phone)
        swipes = device.transport.device.swipes
        assert len(swipes) == 3
        # Home: from bottom edge upwards.
        assert swipes[0][1] == 64 - 2
        assert swipes[0][3] < swipes[0][1]
        # Back: from the left edge rightwards.
        assert swipes[1][0] == 2
        assert swipes[1][2] > swipes[1][0]
        # App switcher: slow, long swipe.
        assert swipes[2][4] >= 600
    finally:
        phone.close()


def test_hand_checks_warns_on_touch_firmware() -> None:
    checks = hand_checks({"hid": "touch", "screen": [1080, 2400]}, screen=(1179, 2556))
    by_name = {check["name"]: check for check in checks}
    assert by_name["hand-hid"]["status"] == "warn"
    assert "build-abs" in by_name["hand-hid"]["detail"]
    assert by_name["hand-screen"]["status"] == "warn"


def test_hand_checks_pass_on_abs_firmware() -> None:
    checks = hand_checks({"hid": "absmouse", "screen": [1179, 2556]}, screen=(1179, 2556))
    by_name = {check["name"]: check for check in checks}
    assert by_name["hand-hid"]["status"] == "pass"
    assert by_name["hand-screen"]["status"] == "pass"


def test_configure_hand_for_ios_sets_screen_and_warns() -> None:
    from phone_console.factory import build_phone

    phone = build_phone(mock=True)
    try:
        result = configure_hand_for_ios(
            phone, screen=(1179, 2556), margin=0.02, save=True, warmup=True
        )
        assert result["screen"] == [1179, 2556]
        assert result["saved"] is True
        assert result["warnings"] and "build-abs" in result["warnings"][0]
        assert result["cal"] == [round(0.02 * 32767), 32767 - round(0.02 * 32767),
                                 round(0.02 * 32767), 32767 - round(0.02 * 32767)]
        device = phone.device
        assert tuple(device.screen) == (1179, 2556)
        assert result["pointer"]["ok"] is True
    finally:
        phone.close()


# --------------------------------------------------------------------- CLI
def _run_cli(argv: list[str]) -> int:
    from phone_console.cli import main

    return main(argv)


def test_cli_ios_status_ready(monkeypatch, capsys) -> None:
    payload = {
        "ok": True,
        "platform": "ios",
        "state": "ready",
        "serial": "UDID",
        "devices": [],
        "capture_devices": [{"index": 1, "name": "iPhone"}],
        "capture_device": {"index": 1, "name": "iPhone"},
        "tools": _fake_tools(),
        "hint": "ok",
    }
    monkeypatch.setattr(ios_mod, "ios_status", lambda **kwargs: dict(payload))
    assert _run_cli(["ios", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "ready"


def test_cli_ios_status_missing_returns_1(monkeypatch, capsys) -> None:
    payload = {
        "ok": False,
        "platform": "ios",
        "state": "missing",
        "devices": [],
        "capture_devices": [],
        "capture_device": None,
        "tools": _fake_tools(),
        "hint": "no iphone",
    }
    monkeypatch.setattr(ios_mod, "ios_status", lambda **kwargs: dict(payload))
    assert _run_cli(["ios", "status"]) == 1
    assert "no iphone" in capsys.readouterr().err


def test_cli_ios_devices(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        ios_mod, "usbmux_list", lambda **kwargs: [{"device_id": 1, "serial": "UDID"}]
    )
    monkeypatch.setattr(
        ios_mod, "list_capture_devices", lambda *a, **k: [{"index": 1, "name": "iPhone"}]
    )
    assert _run_cli(["ios", "devices"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["capture_device"]["name"] == "iPhone"


def test_cli_ios_shot(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        ios_mod, "capture_once", lambda *a, **k: (make_frame(24, 48), "avfoundation")
    )
    out = tmp_path / "ios.png"
    assert _run_cli(["ios", "shot", "--out", str(out)]) == 0
    assert out.is_file()
    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "avfoundation"
    assert payload["frame"] == [24, 48]


def test_cli_eye_shot_ios(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        ios_mod, "capture_once", lambda *a, **k: (make_frame(24, 48), "pymobiledevice3")
    )
    out = tmp_path / "eye-ios.png"
    assert _run_cli(["eye", "shot", "--out", str(out), "--ios"]) == 0
    assert out.is_file()
    assert "ios:pymobiledevice3" in capsys.readouterr().out


def test_cli_ios_hand_configures_mock_hand(capsys) -> None:
    assert (
        _run_cli(
            [
                "--mock",
                "ios",
                "hand",
                "--no-capture",
                "--screen",
                "1179x2556",
                "--margin",
                "0",
                "--save",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["screen"] == [1179, 2556]
    assert payload["saved"] is True


def test_cli_ios_home_uses_hand(capsys) -> None:
    assert _run_cli(["--mock", "ios", "home"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "home"
    assert payload["screen"] == [1080, 2400]


# ----------------------------------------------------------------- service
def test_service_ios_eye_only_status_and_screenshot(tmp_path) -> None:
    phone = IosPhone(Device(MockTransport()), FakeSource())

    def no_hand():
        raise DeviceError("DISCONNECT", "no hand")

    phone.hello = no_hand  # type: ignore[method-assign]
    service = PhoneService(phone, screenshot_dir=tmp_path)
    try:
        status = service.status()
        assert status["ok"] is True
        assert status["platform"] == "ios"
        assert status["hand"]["ok"] is False
        assert "no hand" in status["hand"]["error"]
        assert status["screen"] == [32, 64]
        payload, data = service.screenshot()
        assert data.startswith(b"\x89PNG")
        assert payload["width"] == 32
        assert payload["frame_width"] == 32
    finally:
        phone.close()


def test_service_ios_actions_still_need_hand() -> None:
    phone = IosPhone(Device(MockTransport()), FakeSource())

    def no_hand():
        raise DeviceError("DISCONNECT", "no hand")

    phone.hello = no_hand  # type: ignore[method-assign]
    service = PhoneService(phone)
    try:
        with pytest.raises(DeviceError):
            service.tap(10, 10)
    finally:
        phone.close()


# ------------------------------------------------------------------ doctor
def _ready_status():
    return {
        "ok": True,
        "platform": "ios",
        "state": "ready",
        "serial": "UDID",
        "devices": [
            {"device_id": 1, "serial": "UDID", "name": "iPhone", "ios_version": "26.1"}
        ],
        "capture_devices": [{"index": 1, "name": "iPhone"}],
        "capture_device": {"index": 1, "name": "iPhone"},
        "tools": _fake_tools(pymobiledevice3="/usr/local/bin/pymobiledevice3"),
        "native": _fake_native(True),
        "hint": "ok",
    }


def test_doctor_ios_ready(monkeypatch) -> None:
    from phone_console.doctor import run_doctor

    monkeypatch.setattr(ios_mod, "ios_status", lambda **kwargs: _ready_status())
    report = run_doctor(None, "mock", platform="ios", output=None)
    names = {check.name: check for check in report.checks}
    assert report.platform == "ios"
    assert names["ios-tools"].status == "pass"
    assert names["ios-device"].status == "pass"
    assert names["ios-capture"].status == "pass"
    assert names["hand"].status == "skip"
    assert report.ok is True


def test_doctor_ios_missing_device(monkeypatch) -> None:
    from phone_console.doctor import run_doctor

    status = dict(_ready_status())
    status.update({"ok": False, "state": "missing", "devices": [], "capture_device": None,
                   "capture_devices": [], "hint": "no iphone",
                   "native": _fake_native(False, "iPhone screen not found")})
    monkeypatch.setattr(ios_mod, "ios_status", lambda **kwargs: status)
    report = run_doctor(None, "mock", platform="ios", output=None)
    names = {check.name: check for check in report.checks}
    assert names["ios-device"].status == "fail"
    assert names["ios-capture"].status == "warn"
    assert report.ok is False


def test_doctor_ios_with_mock_hand_warns_about_firmware(monkeypatch) -> None:
    from phone_console.factory import build_phone
    from phone_console.doctor import run_doctor

    monkeypatch.setattr(ios_mod, "ios_status", lambda **kwargs: _ready_status())
    phone = build_phone(mock=True)
    try:
        report = run_doctor(phone, "mock", platform="ios", output=None)
    finally:
        phone.close()
    names = {check.name: check for check in report.checks}
    assert names["hand-hid"].status == "warn"
    assert ABS_FIRMWARE_HINT.format(hid="mock") in names["hand-hid"].detail


# ------------------------------------------------------- native IOSEye eye
def _fake_launcher(monkeypatch, frames: list[bytes] | None = None, status_error: str | None = None):
    """Patch _launch_app so IosNativeSource talks to an in-process fake helper."""
    import os as _os
    import socket as _socket
    import threading as _threading

    def launch(config, app=None):
        status_path = Path(config["status_file"])
        socket_path = Path(config["socket"]) if config.get("socket") else None
        mode = config.get("mode")
        status = {
            "ok": status_error is None,
            "mode": mode,
            "pid": 999_999_999,
            "helper": "IOSEye",
            "helper_version": "1.0.0",
            "device": "iPhone" if status_error is None else None,
            "width": 32,
            "height": 64,
            "camera_auth": 3 if status_error is None else 0,
            "error": status_error,
            "frames": 0,
        }
        if mode == "stream" and socket_path is not None and status_error is None:
            server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            server.bind(str(socket_path))
            server.listen(1)

            def serve():
                conn, _ = server.accept()
                try:
                    for frame in frames or [jpeg_bytes(32, 64), jpeg_bytes(32, 64)]:
                        conn.sendall(struct.pack(">I", len(frame)) + frame)
                    time.sleep(2.0)
                finally:
                    conn.close()
                    server.close()

            _threading.Thread(target=serve, daemon=True).start()
        status_path.write_text(json.dumps(status), encoding="utf-8")

    monkeypatch.setattr(ios_mod, "_launch_app", launch)
    monkeypatch.setattr(ios_mod, "ios_eye_built", lambda: True)


def test_native_live_source_reads_frames(monkeypatch, tmp_path) -> None:
    _fake_launcher(monkeypatch)
    source = ios_mod.IosNativeSource(startup_timeout=8.0)
    try:
        first = source.grab()
        assert first.size == (32, 64)
        second = source.wait_newer(first.ts, timeout=3.0)
        assert second.ts >= first.ts
        assert source.native_size == (32, 64)
        stats = source.stats()
        assert stats["backend"] == "native"
        assert stats["device"] == "iPhone"
        assert stats["frames"] >= 2
    finally:
        source.close()


def test_native_live_source_surfaces_camera_permission(monkeypatch) -> None:
    _fake_launcher(monkeypatch, status_error="camera permission not granted (status=0)")
    source = ios_mod.IosNativeSource(startup_timeout=3.0)
    try:
        with pytest.raises(IosError) as info:
            source.grab()
    finally:
        source.close()
    assert "相机权限" in str(info.value) or "camera" in str(info.value).lower()


def test_capture_native_shot(monkeypatch) -> None:
    def launch(config, app=None):
        Path(config["status_file"]).write_text(
            json.dumps({
                "ok": True, "mode": "shot", "pid": 999_999_999, "device": "iPhone",
                "width": 32, "height": 64, "camera_auth": 3, "error": None,
            }),
            encoding="utf-8",
        )
        Path(config["out"]).write_bytes(png_bytes(32, 64))

    monkeypatch.setattr(ios_mod, "_launch_app", launch)
    monkeypatch.setattr(ios_mod, "ios_eye_built", lambda: True)
    frame, status = ios_mod.capture_native_shot(timeout=5.0)
    assert frame.size == (32, 64)
    assert status["device"] == "iPhone"


def test_ios_status_ready_via_native(monkeypatch) -> None:
    monkeypatch.setattr(ios_mod, "tool_availability", lambda ffmpeg=None: _fake_tools())
    monkeypatch.setattr(ios_mod, "ios_eye_probe", lambda timeout=25.0: _fake_native(True))
    monkeypatch.setattr(
        ios_mod,
        "usbmux_list",
        lambda **kwargs: [{"device_id": 1, "serial": "UDID", "connection_type": "USB"}],
    )
    monkeypatch.setattr(ios_mod, "pymobiledevice3_devices", lambda timeout=8.0: [])
    status = ios_status()
    assert status["ok"] is True
    assert status["state"] == "ready"
    assert status["capture_backend"] == "native"
    assert status["native"]["device"] == "iPhone"


def test_status_hint_for_camera_denied() -> None:
    hint = ios_mod._status_hint({"ok": False, "camera_auth": 0,
                                 "error": "camera permission not granted (status=0)"})
    assert "相机权限" in hint
    hint2 = ios_mod._status_hint({"ok": False, "error": "iPhone screen not found. ..."})
    assert "QuickTime" in hint2


def test_ios_eye_build_metadata() -> None:
    tools = ios_mod.tool_availability()
    native = tools["native_eye"]
    assert native["helper"] == "IOSEye"
    assert "app" in native
    assert isinstance(native["built"], bool)


@pytest.mark.skipif(
    not ios_mod.ios_eye_built() or ios_mod.resolve_clang() is None,
    reason="IOSEye helper not built (needs macOS + Command Line Tools)",
)
def test_native_helper_synthetic_stream() -> None:
    """End-to-end socket test of the real helper in synthetic mode (no camera)."""
    import os as _os

    _os.environ["PHONE_IOS_EYE_SYNTHETIC"] = "1"
    try:
        source = ios_mod.IosNativeSource(max_fps=10, startup_timeout=10)
        try:
            first = source.grab(timeout=10)
            assert first.size == (320, 640)
            second = source.wait_newer(first.ts, timeout=3)
            assert second.ts >= first.ts
        finally:
            source.close()
    finally:
        _os.environ.pop("PHONE_IOS_EYE_SYNTHETIC", None)
    assert ios_mod.helper_pids() == []
