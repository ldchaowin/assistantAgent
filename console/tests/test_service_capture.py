from pathlib import Path

import time

import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")

from phone_console.device import Device
from phone_console.factory import build_phone
from phone_console.phone import DevicePhone
from phone_console.service import PhoneService, ServiceError
from phone_console.transport import MockTransport
from phone_console.vision.frame import Frame
from phone_console.vision.sources import ImageSource

ASSET = Path(__file__).resolve().parents[2] / "docs" / "assets" / "eye-demo.png"


def make_frame(age_s: float = 0.0) -> Frame:
    frame = ImageSource(ASSET).grab()
    frame.ts = time.time() - age_s
    return frame


class FakeStream:
    name = "scrcpy:test"

    def __init__(self, frame=None, stats=None) -> None:
        self._frame = frame if frame is not None else ImageSource(ASSET).grab()
        self._stats = dict(stats or {"frames": 7, "fps": 24.5})

    def stats(self) -> dict:
        data = dict(self._stats)
        data.setdefault("running", True)
        data.setdefault(
            "age_ms", round((time.time() - self._frame.ts) * 1000.0, 1)
        )
        return data

    def grab(self):
        return self._frame

    def close(self) -> None:
        return None


class FakeEngine:
    hits = {"frame": 3, "crop": 1}
    last_elapsed_ms = 1.0

    def recognize(self, image):
        from phone_console.vision.ocr import OcrItem

        return [OcrItem("设置", 0.99, ((10, 10), (40, 10), (40, 30), (10, 30)))]


def test_status_reports_static_capture() -> None:
    phone = build_phone(mock=True, image=str(ASSET))
    service = PhoneService(phone)
    try:
        capture = service.status()["capture"]
        assert capture["name"].startswith("image:")
        assert capture["stats"] is None
        assert capture["ocr_cached_frames"] is None
    finally:
        phone.close()


def test_status_reports_stream_stats_and_ocr_cache() -> None:
    phone = DevicePhone(Device(MockTransport()), FakeStream())
    service = PhoneService(phone, engine=FakeEngine())
    try:
        capture = service.status()["capture"]
        assert capture["name"] == "scrcpy:test"
        assert capture["stats"]["frames"] == 7
        assert capture["ocr_cached_frames"] == 3
        assert capture["health"]["degraded"] is False
    finally:
        phone.close()


# ------------------------------------------------------------------ freshness
def test_health_flags_a_stale_capture() -> None:
    phone = DevicePhone(Device(MockTransport()), FakeStream(frame=make_frame(60)))
    service = PhoneService(phone, max_frame_age_ms=1000)
    try:
        health = service.health()
        assert health["ok"] is True
        assert health["capture"]["degraded"] is True
        assert health["capture"]["age_ms"] > 1000
        assert "old" in health["capture"]["reason"]
    finally:
        phone.close()


def test_health_is_fine_for_a_fresh_capture() -> None:
    phone = DevicePhone(Device(MockTransport()), FakeStream())
    service = PhoneService(phone, max_frame_age_ms=1000)
    try:
        health = service.health()
        assert health["capture"]["degraded"] is False
        assert health["capture"]["age_ms"] < 1000
    finally:
        phone.close()


def test_health_flags_a_stream_error_with_a_stale_frame() -> None:
    phone = DevicePhone(
        Device(MockTransport()),
        FakeStream(
            frame=make_frame(60),
            stats={"frames": 9, "error": "StreamError: boom"},
        ),
    )
    service = PhoneService(phone, max_frame_age_ms=1000)
    try:
        health = service.health()
        assert health["capture"]["degraded"] is True
        assert "boom" in health["capture"]["reason"]
        assert "old" in health["capture"]["reason"]
    finally:
        phone.close()


def test_health_ignores_a_restart_while_frames_are_fresh() -> None:
    """A stream hiccup that already recovered is not a reason to restart."""
    phone = DevicePhone(
        Device(MockTransport()),
        FakeStream(stats={"frames": 9, "error": "StreamError: transient"}),
    )
    service = PhoneService(phone, max_frame_age_ms=1000)
    try:
        assert service.health()["capture"]["degraded"] is False
    finally:
        phone.close()


def test_health_ignores_a_startup_error_without_frames() -> None:
    phone = DevicePhone(
        Device(MockTransport()),
        FakeStream(stats={"frames": 0, "error": "StreamError: no device"}),
    )
    service = PhoneService(phone)
    try:
        assert service.health()["capture"]["degraded"] is False
    finally:
        phone.close()


def test_screenshot_and_find_reject_a_stale_frame() -> None:
    phone = DevicePhone(Device(MockTransport()), FakeStream(frame=make_frame(60)))
    service = PhoneService(phone, max_frame_age_ms=1000)
    try:
        with pytest.raises(ServiceError) as shot:
            service.screenshot()
        assert shot.value.code == "STALE_FRAME"
        assert shot.value.status == 503
        with pytest.raises(ServiceError) as found:
            service.find("设置")
        assert found.value.code == "STALE_FRAME"
        assert found.value.status == 503
    finally:
        phone.close()


def test_screenshot_and_find_report_frame_age() -> None:
    phone = DevicePhone(Device(MockTransport()), FakeStream(frame=make_frame(0.5)))
    service = PhoneService(
        phone, engine=FakeEngine(), max_frame_age_ms=60_000
    )
    try:
        payload, _ = service.screenshot()
        assert 400 <= payload["frame_age_ms"] < 60_000
        found = service.find("设置")
        assert 400 <= found["frame_age_ms"] < 60_000
    finally:
        phone.close()


def test_device_unavailable_at_startup_does_not_degrade_into_restarts() -> None:
    """Phone offline when the service starts: report it, but never restart-loop.

    The capture re-arms itself on the next grab (see the stream fatal-retry), so
    it is not the supervisor's job to restart the whole service every tick.
    """
    phone = DevicePhone(
        Device(MockTransport()),
        FakeStream(
            stats={
                "frames": 0,
                "process_alive": False,
                "age_ms": None,
                "effective_age_ms": None,
                "error": "no ADB device: connect the phone and enable USB debugging",
            }
        ),
    )
    service = PhoneService(phone)
    try:
        health = service.health()["capture"]
        assert health["degraded"] is False
        assert health["state"] == "device_unavailable"
    finally:
        phone.close()


def test_internal_startup_error_is_still_degraded() -> None:
    phone = DevicePhone(
        Device(MockTransport()),
        FakeStream(
            stats={
                "frames": 0,
                "process_alive": False,
                "error": "StreamError: PyAV is not installed",
            }
        ),
    )
    service = PhoneService(phone)
    try:
        health = service.health()["capture"]
        assert health["degraded"] is True
        assert health["state"] == "error"
    finally:
        phone.close()
