import json

import pytest

np = pytest.importorskip("numpy")

from phone_console.phone import RecordingPhone
from phone_console.virtual import VirtualPhone
from phone_console.vision.frame import Frame


def test_recording_phone_saves_frames_and_actions(tmp_path) -> None:
    inner = VirtualPhone(
        [Frame(np.zeros((10, 10, 3), dtype=np.uint8)) for _ in range(2)]
    )
    rec = RecordingPhone(inner, tmp_path / "rec")
    rec.grab()
    rec.tap(5, 6)
    rec.close()
    rec.close()  # idempotent

    assert (tmp_path / "rec" / "frames" / "0000.png").is_file()
    frames = [
        json.loads(line)
        for line in (tmp_path / "rec" / "frames.jsonl").read_text().splitlines()
    ]
    actions = [
        json.loads(line)
        for line in (tmp_path / "rec" / "actions.jsonl").read_text().splitlines()
    ]
    assert frames[0]["index"] == 0
    assert actions[0]["action"] == "tap"
    assert actions[0]["params"] == {"x": 5, "y": 6, "hold_ms": None}
    assert inner.transport.closed is True
