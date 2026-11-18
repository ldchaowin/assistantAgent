import base64
import http.client
import json
import threading
import time

import pytest

np = pytest.importorskip("numpy")

from phone_console.service import EventBus, create_server
from phone_console.virtual import VirtualPhone
from phone_console.vision.frame import Frame
from phone_console.vision.ocr import OcrItem

TOKEN = "test-token"


class FakeEngine:
    name = "fake"
    last_elapsed_ms = 1.0

    def __init__(self, items):
        self.items = list(items)

    def recognize(self, image):
        return list(self.items)


@pytest.fixture
def service(tmp_path):
    phone = VirtualPhone(
        [Frame(np.zeros((100, 100, 3), dtype=np.uint8)) for _ in range(4)],
        timeout=0.2,
    )
    engine = FakeEngine(
        [OcrItem("设置", 0.99, ((10, 10), (40, 10), (40, 30), (10, 30)))]
    )
    server = create_server(
        phone,
        host="127.0.0.1",
        port=0,
        token=TOKEN,
        screenshot_dir=tmp_path / "shots",
        engine=engine,
    )
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
    )
    thread.start()
    try:
        yield server, phone
    finally:
        server.shutdown()
        server.server_close()
        phone.close()


def request(server, method, path, body=None, token=TOKEN):
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    data = response.read()
    content_type = response.getheader("Content-Type", "")
    conn.close()
    if "application/json" in content_type:
        return response.status, json.loads(data)
    return response.status, data


def test_health_is_public(service) -> None:
    server, _ = service
    status, payload = request(server, "GET", "/health", token="")
    assert status == 200
    assert payload["ok"] is True
    assert payload["service"] == "phone"
    assert payload["capture"]["degraded"] is False


def test_status_requires_token(service) -> None:
    server, _ = service
    status, _ = request(server, "GET", "/status", token="")
    assert status == 401
    status, payload = request(server, "GET", "/status")
    assert status == 200
    assert payload["screen"] == [1080, 2400]


def test_tap_records_action(service) -> None:
    server, phone = service
    status, payload = request(server, "POST", "/tap", {"x": 12, "y": 34})
    assert status == 200
    assert payload["ok"] is True
    assert phone.mock.taps == [(12, 34)]


def test_screenshot_png_and_json(service) -> None:
    pytest.importorskip("PIL")
    server, _ = service
    status, data = request(server, "GET", "/screenshot")
    assert status == 200
    assert data.startswith(b"\x89PNG")

    status, payload = request(
        server, "POST", "/screenshot", {"include_base64": True, "max_width": 50}
    )
    assert status == 200
    assert payload["width"] == 50
    assert payload["height"] == 50
    assert base64.b64decode(payload["base64"]).startswith(b"\x89PNG")


def test_find_returns_device_center(service) -> None:
    server, _ = service
    status, payload = request(server, "POST", "/find", {"text": "设置"})
    assert status == 200
    assert payload["found"] is True
    assert payload["match"]["device_center"] == [270, 480]
    assert payload["screenshot_path"]


def test_find_uses_screen_from_hello(service) -> None:
    server, phone = service
    phone.mock.screen = (1264, 2800)
    status, payload = request(server, "POST", "/find", {"text": "设置"})
    assert status == 200
    assert payload["screen"] == [1264, 2800]
    assert payload["match"]["device_center"] == [316, 560]


def test_run_endpoint(service, tmp_path) -> None:
    server, _ = service
    scenario = tmp_path / "svc.json"
    scenario.write_text(
        json.dumps({"name": "svc", "steps": [{"action": "sleep", "ms": 1}]}),
        encoding="utf-8",
    )
    status, payload = request(
        server, "POST", "/run", {"scenario": str(scenario), "run_dir": str(tmp_path / "run")}
    )
    assert status == 200
    assert payload["ok"] is True
    assert (tmp_path / "run" / "report.jsonl").is_file()


def test_busy_returns_409(service) -> None:
    server, phone = service
    phone_service = server.service
    assert phone_service.lock.acquire(timeout=0.1)
    try:
        status, payload = request(server, "POST", "/tap", {"x": 1, "y": 2})
    finally:
        phone_service.lock.release()
    assert status == 409
    assert payload["error"]["code"] == "BUSY"


def test_tap_recovers_from_fault(service) -> None:
    server, phone = service
    phone.mock.inject_fault()
    assert phone.mock.state == "FAULT"
    status, payload = request(server, "POST", "/tap", {"x": 5, "y": 6})
    assert status == 200
    assert payload["ok"] is True
    assert phone.mock.state == "READY"


def test_bad_body_returns_400(service) -> None:
    server, _ = service
    status, payload = request(server, "POST", "/tap", {"x": 1})
    assert status == 400
    assert payload["ok"] is False


def test_event_bus() -> None:
    bus = EventBus()
    channel = bus.subscribe()
    bus.publish({"type": "test"})
    assert channel.get_nowait() == {"type": "test"}
    bus.unsubscribe(channel)
    bus.publish({"type": "ignored"})
    assert channel.empty()


class StaleStream:
    """A capture source whose last frame is minutes old (dead scrcpy tunnel)."""

    name = "scrcpy:stale"

    def __init__(self, age_s: float = 60.0) -> None:
        self._frame = Frame(np.zeros((100, 100, 3), dtype=np.uint8), ts=0.0)
        self._frame.ts = time.time() - age_s

    def stats(self) -> dict:
        return {
            "name": self.name,
            "frames": 4,
            "fps": 0.01,
            "running": True,
            "age_ms": self._frame.age_ms,
            "effective_age_ms": self._frame.age_ms,
            "restarts": 2,
            "error": None,
        }

    def grab(self):
        return self._frame

    def close(self) -> None:
        return None


def _serve(phone, **kwargs):
    from phone_console.service import create_server

    server = create_server(phone, host="127.0.0.1", port=0, **kwargs)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
    )
    thread.start()
    return server, thread


def test_stale_capture_returns_503() -> None:
    from phone_console.device import Device
    from phone_console.phone import DevicePhone
    from phone_console.transport import MockTransport

    phone = DevicePhone(Device(MockTransport()), StaleStream())
    server, thread = _serve(phone, max_frame_age_ms=1000)
    try:
        status, payload = request(server, "POST", "/screenshot", {})
        assert status == 503
        assert payload["error"]["code"] == "STALE_FRAME"
        status, payload = request(server, "POST", "/find", {"text": "设置"})
        assert status == 503
        assert payload["error"]["code"] == "STALE_FRAME"
        status, health = request(server, "GET", "/health", token="")
        assert status == 200
        assert health["ok"] is True
        assert health["capture"]["degraded"] is True
        assert "old" in health["capture"]["reason"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        phone.close()


def test_shutdown_endpoint_stops_the_server() -> None:
    phone = VirtualPhone(
        [Frame(np.zeros((10, 10, 3), dtype=np.uint8))], timeout=0.1
    )
    server, thread = _serve(phone)
    try:
        status, payload = request(server, "POST", "/shutdown", {})
        assert status == 200
        assert payload["shutting_down"] is True
        thread.join(5)
        assert not thread.is_alive()
    finally:
        server.server_close()
        phone.close()


class _FreshStream:
    """A live capture source that always has a current frame."""

    name = "test:fresh"

    def __init__(self) -> None:
        self.grabs = 0

    def grab(self):
        self.grabs += 1
        return Frame(np.zeros((100, 100, 3), dtype=np.uint8), ts=time.time())

    def stats(self) -> dict:
        return {
            "name": self.name,
            "frames": self.grabs,
            "fps": 0.0,
            "running": True,
            "process_alive": True,
            "age_ms": 0.0,
            "effective_age_ms": 0.0,
            "restarts": 0,
            "error": None,
        }

    def close(self) -> None:
        return None


class _StaticStream:
    """A live capture source whose screen has not changed for minutes."""

    name = "scrcpy:static"

    def __init__(self, age_s: float = 120.0) -> None:
        self._frame = Frame(
            np.zeros((100, 100, 3), dtype=np.uint8), ts=time.time() - age_s
        )

    def grab(self):
        return self._frame

    def stats(self) -> dict:
        age_ms = self._frame.age_ms
        return {
            "name": self.name,
            "frames": 40,
            "fps": 0.0,
            "running": True,
            "process_alive": True,
            "age_ms": age_ms,
            "effective_age_ms": age_ms,
            "restarts": 0,
            "error": None,
        }

    def close(self) -> None:
        return None


class _PendingStream:
    """A capture source that has not produced its first frame yet."""

    name = "scrcpy:pending"

    def grab(self):
        raise AssertionError("no grab expected while checking health")

    def stats(self) -> dict:
        return {
            "name": self.name,
            "frames": 0,
            "fps": 0.0,
            "running": True,
            "process_alive": False,
            "age_ms": None,
            "effective_age_ms": None,
            "restarts": 0,
            "error": None,
        }

    def close(self) -> None:
        return None


def _device_phone(stream, device_factory=None):
    from phone_console.device import Device
    from phone_console.phone import DevicePhone
    from phone_console.transport import MockTransport

    device = device_factory() if device_factory else Device(MockTransport())
    return DevicePhone(device, stream)


def test_static_screen_is_healthy_even_when_the_frame_is_old(tmp_path) -> None:
    phone = _device_phone(_StaticStream())
    engine = FakeEngine(
        [OcrItem("设置", 0.99, ((10, 10), (40, 10), (40, 30), (10, 30)))]
    )
    server, thread = _serve(
        phone, screenshot_dir=tmp_path / "shots", engine=engine, max_frame_age_ms=1000
    )
    try:
        status, health = request(server, "GET", "/health", token="")
        assert status == 200
        capture = health["capture"]
        # A static screen is "static", not degraded: restarting cannot help and
        # would only throw away a perfectly current frame.
        assert capture["degraded"] is False
        assert capture["state"] == "static"
        assert capture["age_ms"] > 100_000

        status, payload = request(server, "POST", "/screenshot", {})
        assert status == 200
        assert payload["ok"] is True

        status, payload = request(server, "POST", "/find", {"text": "设置"})
        assert status == 200
        assert payload["found"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        phone.close()


def test_first_frame_pending_is_not_ready_not_degraded(tmp_path) -> None:
    phone = _device_phone(_PendingStream())
    server, thread = _serve(phone, screenshot_dir=tmp_path / "shots", max_frame_age_ms=1000)
    try:
        status, health = request(server, "GET", "/health", token="")
        assert status == 200
        capture = health["capture"]
        assert capture["degraded"] is False
        assert capture["state"] == "not_ready"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        phone.close()


def test_screenshot_does_not_wait_for_the_hand(tmp_path) -> None:
    from phone_console.device import DeviceError

    hello_calls = []

    def broken_hello():
        hello_calls.append(1)
        raise DeviceError("DISCONNECT", "hand is down")

    from phone_console.device import Device
    from phone_console.transport import MockTransport

    device = Device(MockTransport())
    device.hello = broken_hello
    phone = _device_phone(_FreshStream(), device_factory=lambda: device)
    server, thread = _serve(phone, screenshot_dir=tmp_path / "shots")
    try:
        status, payload = request(server, "POST", "/screenshot", {})
        assert status == 200
        assert payload["ok"] is True
        # The eye (adb/scrcpy) is independent of the hand (input).
        assert hello_calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        phone.close()


def test_find_probes_a_missing_hand_only_once(tmp_path) -> None:
    from phone_console.device import DeviceError

    hello_calls = []

    def broken_hello():
        hello_calls.append(1)
        raise DeviceError("DISCONNECT", "hand is down")

    from phone_console.device import Device
    from phone_console.transport import MockTransport

    device = Device(MockTransport())
    device.hello = broken_hello
    engine = FakeEngine(
        [OcrItem("设置", 0.99, ((10, 10), (40, 10), (40, 30), (10, 30)))]
    )
    phone = _device_phone(_FreshStream(), device_factory=lambda: device)
    server, thread = _serve(phone, screenshot_dir=tmp_path / "shots", engine=engine)
    try:
        for _ in range(2):
            status, payload = request(server, "POST", "/find", {"text": "设置"})
            assert status == 200
            assert payload["found"] is True
        # The first find probes the hand; the second reuses "hand is missing"
        # instead of paying another DNS/connect round trip.
        assert len(hello_calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        phone.close()


def test_ocr_lists_texts_with_device_centers(service) -> None:
    server, _ = service
    status, payload = request(server, "GET", "/ocr?min_score=0.1")
    assert status == 200
    assert payload["ok"] is True
    assert payload["screen"] == [1080, 2400]
    assert payload["frame"] == [100, 100]
    texts = {item["text"]: item for item in payload["items"]}
    assert "设置" in texts
    # Frame 100x100 is scaled up to the 1080x2400 device.
    assert texts["设置"]["device_center"] == [270, 480]
    assert payload["screenshot_path"]


def test_ocr_respects_min_score(service) -> None:
    server, _ = service
    status, payload = request(server, "GET", "/ocr?min_score=0.999")
    assert status == 200
    assert payload["items"] == []
