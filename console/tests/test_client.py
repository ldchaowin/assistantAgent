import json
import threading

import pytest

np = pytest.importorskip("numpy")

from phone_console.client import PhoneClient, PhoneClientError
from phone_console.service import create_server
from phone_console.virtual import VirtualPhone
from phone_console.vision.frame import Frame
from phone_console.vision.ocr import OcrItem

TOKEN = "client-token"


class FakeEngine:
    name = "fake"
    last_elapsed_ms = 1.0

    def __init__(self, items):
        self.items = list(items)

    def recognize(self, image):
        return list(self.items)


@pytest.fixture
def client(tmp_path):
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
    host, port = server.server_address[:2]
    try:
        yield PhoneClient(f"http://{host}:{port}", token=TOKEN), phone
    finally:
        server.shutdown()
        server.server_close()
        phone.close()


def test_health_and_status(client) -> None:
    api, _ = client
    assert api.health()["ok"] is True
    status = api.status()
    assert status["screen"] == [1080, 2400]


def test_tap_and_find(client) -> None:
    api, phone = client
    assert api.tap(12, 34)["ok"] is True
    assert phone.mock.taps == [(12, 34)]
    found = api.find("设置")
    assert found["found"] is True
    assert found["match"]["device_center"] == [270, 480]


def test_screenshot_saves_png(client, tmp_path) -> None:
    pytest.importorskip("PIL")
    api, _ = client
    target = tmp_path / "shot.png"
    payload = api.screenshot(target, max_width=50)
    assert payload["width"] == 50
    assert target.read_bytes().startswith(b"\x89PNG")


def test_run_scenario(client, tmp_path) -> None:
    api, _ = client
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps({"name": "client", "steps": [{"action": "sleep", "ms": 1}]}),
        encoding="utf-8",
    )
    result = api.run(str(scenario), str(tmp_path / "run"))
    assert result["ok"] is True
    assert (tmp_path / "run" / "report.jsonl").is_file()


def test_bad_token_raises(client) -> None:
    api, _ = client
    bad = PhoneClient(api.base_url, token="wrong")
    with pytest.raises(PhoneClientError) as exc:
        bad.status()
    assert exc.value.status == 401
