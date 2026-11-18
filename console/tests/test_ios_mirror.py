"""Tests for the mirrored-iPhone window eye (plan B: hand owns the USB port)."""
from __future__ import annotations

import io
import sys
import types

import pytest

numpy = pytest.importorskip("numpy")
PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from phone_console.vision import mirror as mirror_mod  # noqa: E402
from phone_console.vision.frame import Frame  # noqa: E402
from phone_console.vision.mirror import (  # noqa: E402
    MirrorError,
    MirrorWindowSource,
    capture_mirror_shot,
    find_mirror_window,
    list_windows,
)


def png_bytes(width: int = 120, height: int = 260) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (20, 40, 80)).save(buffer, format="PNG")
    return buffer.getvalue()


FAKE_WINDOWS = [
    {"id": 1, "owner": "DeepSeek Harness", "title": "chat", "width": 1280, "height": 800},
    {"id": 9164, "owner": "iPhone镜像", "title": "欢迎使用iPhone镜像", "width": 640, "height": 662},
    {"id": 42, "owner": "UxPlay", "title": "UxPlay", "width": 400, "height": 800},
]


def fake_windows(include_all: bool = False):
    return [dict(item) for item in FAKE_WINDOWS]


def test_list_windows_shape(monkeypatch) -> None:
    monkeypatch.setattr(mirror_mod, "list_windows", fake_windows)
    windows = mirror_mod.list_windows()
    assert windows[0]["id"] == 1
    assert windows[1]["owner"] == "iPhone镜像"


def test_find_mirror_window_prefers_largest(monkeypatch) -> None:
    monkeypatch.setattr(mirror_mod, "list_windows", fake_windows)
    window = find_mirror_window()
    assert window is not None
    assert window["id"] == 9164  # 640x662 is larger than the 400x800 UxPlay window


def test_find_mirror_window_explicit_id_and_name(monkeypatch) -> None:
    monkeypatch.setattr(mirror_mod, "list_windows", fake_windows)
    assert find_mirror_window(42)["owner"] == "UxPlay"
    assert find_mirror_window("uxplay")["id"] == 42
    assert find_mirror_window("nope") is None


def test_find_mirror_window_none(monkeypatch) -> None:
    monkeypatch.setattr(mirror_mod, "list_windows", lambda include_all=False: [FAKE_WINDOWS[0]])
    assert find_mirror_window() is None


def test_mirror_source_captures_frames(monkeypatch) -> None:
    monkeypatch.setattr(mirror_mod, "list_windows", fake_windows)

    def fake_capture(window_id: int, path) -> bytes:
        assert window_id == 9164
        data = png_bytes()
        from pathlib import Path

        Path(path).write_bytes(data)
        return data

    monkeypatch.setattr(mirror_mod, "capture_window_png", fake_capture)
    source = MirrorWindowSource(interval=0.02, startup_timeout=5.0)
    try:
        first = source.grab()
        assert first.size == (120, 260)
        assert source.native_size == (120, 260)
        second = source.wait_newer(first.ts, timeout=2.0)
        assert second.ts >= first.ts
        stats = source.stats()
        assert stats["backend"] == "mirror"
        assert stats["frames"] >= 2
        assert stats["window"]["owner"] == "iPhone镜像"
        assert "iPhone镜像" in source.name
    finally:
        source.close()
    assert not source._tmpdir.exists() if source._tmpdir else True


def test_mirror_source_reports_missing_window(monkeypatch) -> None:
    monkeypatch.setattr(
        mirror_mod, "list_windows", lambda include_all=False: [FAKE_WINDOWS[0]]
    )
    source = MirrorWindowSource(startup_timeout=1.0)
    try:
        with pytest.raises(MirrorError) as info:
            source.grab(timeout=1.0)
    finally:
        source.close()
    assert "投屏窗口" in str(info.value)


def test_capture_mirror_shot(monkeypatch) -> None:
    monkeypatch.setattr(mirror_mod, "list_windows", fake_windows)
    monkeypatch.setattr(
        mirror_mod,
        "capture_window_png",
        lambda window_id, path: png_bytes(100, 200),
    )
    frame, window = capture_mirror_shot()
    assert frame.size == (100, 200)
    assert window["id"] == 9164


def test_factory_builds_ios_phone_with_mirror_source(monkeypatch) -> None:
    from phone_console.factory import build_phone

    monkeypatch.setattr(mirror_mod, "list_windows", fake_windows)
    monkeypatch.setattr(
        mirror_mod, "capture_window_png", lambda window_id, path: png_bytes()
    )
    phone = build_phone(
        mock=False,
        tcp="127.0.0.1:1",
        ios_mirror="",
        mirror_interval=0.02,
        timeout=0.1,
    )
    try:
        assert phone.platform == "ios"
        assert isinstance(phone.source, MirrorWindowSource)
        phone.grab()
        assert phone.screen == (120, 260)  # from the mirrored window
    finally:
        phone.close()


def test_cli_ios_mirror_list(monkeypatch, capsys) -> None:
    from phone_console.cli import main

    monkeypatch.setattr(mirror_mod, "list_windows", fake_windows)
    monkeypatch.setattr(mirror_mod, "find_mirror_window", lambda wanted=None: FAKE_WINDOWS[1])
    monkeypatch.setattr(mirror_mod, "mirror_available", lambda: True)
    assert main(["ios", "mirror", "list"]) == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["selected"]["owner"] == "iPhone镜像"


def test_cli_ios_mirror_shot(monkeypatch, capsys, tmp_path) -> None:
    from phone_console.cli import main

    monkeypatch.setattr(
        mirror_mod,
        "capture_mirror_shot",
        lambda wanted=None: (Frame(numpy.zeros((260, 120, 3), dtype=numpy.uint8)), FAKE_WINDOWS[1]),
    )
    out = tmp_path / "mirror.png"
    assert main(["ios", "mirror", "shot", "--out", str(out)]) == 0
    assert out.is_file()
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "ios:mirror"
    assert payload["frame"] == [120, 260]


def test_cli_eye_shot_ios_mirror(monkeypatch, capsys, tmp_path) -> None:
    from phone_console.cli import main

    monkeypatch.setattr(
        mirror_mod,
        "capture_mirror_shot",
        lambda wanted=None: (Frame(numpy.zeros((260, 120, 3), dtype=numpy.uint8)), FAKE_WINDOWS[1]),
    )
    out = tmp_path / "eye-mirror.png"
    assert main(["eye", "shot", "--out", str(out), "--ios-mirror"]) == 0
    assert out.is_file()
    assert "ios:mirror" in capsys.readouterr().out
