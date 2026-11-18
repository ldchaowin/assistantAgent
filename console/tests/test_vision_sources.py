
import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

from PIL import Image

from phone_console.vision.sources import AdbSource, ImageSource


def make_png(path, size=(20, 10)) -> None:
    Image.new("RGB", size, (10, 20, 30)).save(path)


def test_image_source_caches_frame(tmp_path) -> None:
    path = tmp_path / "shot.png"
    make_png(path)
    source = ImageSource(path)
    first = source.grab()
    second = source.grab()
    assert first is second
    assert first.size == (20, 10)
    assert source.name == f"image:{path}"


def test_image_source_missing_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        ImageSource(tmp_path / "nope.png").grab()


def test_adb_source_trims_crlf_and_decodes_png(tmp_path, monkeypatch) -> None:
    png = tmp_path / "screen.png"
    make_png(png, (30, 40))
    script = tmp_path / "fake-adb"
    script.write_text('#!/bin/sh\nprintf "\\r\\n"\ncat "$FAKE_PNG"\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_PNG", str(png))

    source = AdbSource(adb=str(script), serial="abc")
    frame = source.grab()
    assert frame.size == (30, 40)
    assert source.name == "adb:abc"


def test_adb_source_rejects_non_png(tmp_path) -> None:
    script = tmp_path / "fake-adb"
    script.write_text('#!/bin/sh\nprintf "not a png"\n', encoding="utf-8")
    script.chmod(0o755)
    source = AdbSource(adb=str(script))
    with pytest.raises(RuntimeError, match="no PNG"):
        source.grab()


def test_adb_source_requires_adb() -> None:
    with pytest.raises(RuntimeError, match="adb not found"):
        AdbSource(adb="/nonexistent/adb")
