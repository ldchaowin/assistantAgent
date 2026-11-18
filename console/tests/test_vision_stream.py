import time
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")
av = pytest.importorskip("av")

import numpy as np
from PIL import Image

from phone_console.vision.stream import (
    H264StreamSource,
    ScrcpySource,
    ScreenrecordSource,
    StreamError,
    scrcpy_command,
    scrcpy_version,
    screenrecord_command,
)

ASSET = Path(__file__).resolve().parents[2] / "docs" / "assets" / "eye-demo.png"


@pytest.fixture(scope="module")
def stream_file(tmp_path_factory) -> Path:
    """A tiny H.264/Matroska clip of the synthetic settings screen."""
    path = tmp_path_factory.mktemp("stream") / "fixture.mkv"
    image = np.asarray(Image.open(ASSET).convert("RGB"))
    height, width = image.shape[:2]
    container = av.open(str(path), mode="w", format="matroska")
    stream = container.add_stream("libx264", rate=30)
    stream.width, stream.height = width, height
    stream.pix_fmt = "yuv420p"
    stream.options = {"preset": "ultrafast", "crf": "22", "g": "30"}
    for index in range(3):
        frame = av.VideoFrame.from_ndarray(image, format="rgb24")
        frame.pts = index
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


@pytest.fixture(scope="module")
def annexb_file(tmp_path_factory) -> Path:
    """Tiny raw Annex-B H.264 clip (what `adb screenrecord` emits)."""
    path = tmp_path_factory.mktemp("annexb") / "fixture.h264"
    image = np.asarray(Image.open(ASSET).convert("RGB"))
    height, width = image.shape[:2]
    container = av.open(str(path), mode="w", format="h264")
    stream = container.add_stream("libx264", rate=30)
    stream.width, stream.height = width, height
    stream.pix_fmt = "yuv420p"
    stream.options = {"preset": "ultrafast", "crf": "22", "g": "10"}
    for index in range(6):
        frame = av.VideoFrame.from_ndarray(image, format="rgb24")
        frame.pts = index
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


def test_scrcpy_command_streams_mkv_to_stdout() -> None:
    command = scrcpy_command(
        serial="ABC", version="2.4", max_size=720, max_fps=15, record="/tmp/rec.mkv"
    )
    assert command[0] == "scrcpy"
    assert "-s" in command and "ABC" in command
    assert "--no-playback" in command
    assert "--no-audio" in command
    assert "--no-control" in command
    assert "--max-size=720" in command
    assert "--max-fps=15" in command
    assert "--record=/tmp/rec.mkv" in command
    assert "--record-format=mkv" in command


def test_scrcpy_command_supports_legacy_1x() -> None:
    command = scrcpy_command(version="1.25.1")
    assert "--no-display" in command
    assert "--no-playback" not in command


def test_screenrecord_command_streams_h264() -> None:
    command = screenrecord_command(adb="/opt/adb", serial="X", size="720x1600")
    assert command[0] == "/opt/adb"
    assert command[1:3] == ["-s", "X"]
    assert "screenrecord" in command
    assert "--output-format=h264" in command
    assert "--size=720x1600" in command
    assert command[-1] == "-"


def test_scrcpy_source_requires_binary(tmp_path) -> None:
    with pytest.raises(StreamError, match="scrcpy not found"):
        ScrcpySource(scrcpy=str(tmp_path / "missing-scrcpy"))


def test_scrcpy_version_parsing(tmp_path) -> None:
    script = tmp_path / "fake-scrcpy"
    script.write_text("#!/bin/sh\necho 'scrcpy 2.4 <https://github.com/Genymobile/scrcpy>'\n")
    script.chmod(0o755)
    assert scrcpy_version(str(script)) == "2.4"


def test_decodes_frames_from_a_pipe(stream_file) -> None:
    source = H264StreamSource(
        ["cat", str(stream_file)], container="matroska", restart=False
    )
    try:
        frame = source.grab(timeout=10)
        assert frame.size == (1080, 2400)
        assert frame.source == source.name
        stats = source.stats()
        assert stats["frames"] >= 1
        assert stats["error"] is None
        assert stats["command"].startswith("cat ")
    finally:
        source.close()


def test_wait_newer_returns_a_fresh_frame(stream_file) -> None:
    source = H264StreamSource(
        ["cat", str(stream_file)], container="matroska", restart=False
    )
    try:
        first = source.grab(timeout=10)
        second = source.wait_newer(first.ts, timeout=5)
        assert second.ts > first.ts
        deadline = time.time() + 5
        while source.stats()["running"] and time.time() < deadline:
            time.sleep(0.02)
        last = source.latest()
        assert last is not None
        with pytest.raises(StreamError):
            # The clip is over; no frame newer than the last one can arrive.
            source.wait_newer(last.ts, timeout=0.4)
    finally:
        source.close()


def test_close_is_idempotent_and_stops_the_reader(stream_file) -> None:
    source = H264StreamSource(
        ["cat", str(stream_file)], container="matroska", restart=False
    )
    source.grab(timeout=10)
    source.close()
    source.close()
    assert source.stats()["running"] is False


def test_missing_command_surfaces_stderr() -> None:
    source = H264StreamSource(
        ["sh", "-c", "echo 'boom: no device' >&2; sleep 0.05; exit 3"],
        restart=False,
        startup_timeout=1.5,
    )
    try:
        with pytest.raises(StreamError) as excinfo:
            source.grab(timeout=1.5)
        assert source.stats()["frames"] == 0
        assert "boom" in source.stats()["stderr"]
        assert "no frame" in str(excinfo.value)
    finally:
        source.close()


def test_restart_counts_and_backs_off() -> None:
    source = H264StreamSource(
        ["sh", "-c", "exit 1"],
        restart=True,
        restart_delay=0.05,
        max_restarts=50,
        startup_timeout=0.5,
    )
    try:
        with pytest.raises(StreamError):
            source.grab(timeout=0.5)
        time.sleep(0.3)
        assert source.stats()["restarts"] >= 1
    finally:
        source.close()


def test_fatal_startup_error_fails_fast() -> None:
    source = H264StreamSource(
        [
            "sh",
            "-c",
            "echo 'ERROR: Could not find any ADB device' >&2; sleep 0.05; exit 1",
        ],
        restart=True,
        restart_delay=0.05,
        startup_timeout=5,
    )
    try:
        started = time.monotonic()
        with pytest.raises(StreamError, match="no ADB device"):
            source.grab(timeout=5)
        assert time.monotonic() - started < 2.0
    finally:
        source.close()


def test_stderr_noise_is_filtered() -> None:
    source = H264StreamSource(
        [
            "sh",
            "-c",
            "echo 'Keyboard Layouts: duplicate keyboard layout identifier -1' >&2; "
            "sleep 0.05; exit 1",
        ],
        restart=False,
        startup_timeout=1,
    )
    try:
        with pytest.raises(StreamError):
            source.grab(timeout=1)
        time.sleep(0.2)
        assert "Keyboard Layouts" not in source.stats()["stderr"]
    finally:
        source.close()


def test_restart_backoff_grows_and_caps() -> None:
    from phone_console.vision.stream import restart_backoff

    assert restart_backoff(0.5, 0) == 0.5
    assert restart_backoff(0.5, 1) == 0.5
    assert restart_backoff(0.5, 2) == 1.0
    assert restart_backoff(0.5, 3) == 2.0
    assert restart_backoff(0.5, 10) == 5.0


def test_empty_restarts_back_off() -> None:
    source = H264StreamSource(
        ["sh", "-c", "exit 1"],
        restart=True,
        restart_delay=0.02,
        max_restarts=100,
        startup_timeout=0.5,
    )
    try:
        with pytest.raises(StreamError):
            source.grab(timeout=0.5)
        time.sleep(0.6)
        stats = source.stats()
        assert 1 <= stats["restarts"] <= 8
        assert stats["restart_delay_s"] > 0
    finally:
        source.close()


def test_stream_frames_feed_ocr(stream_file) -> None:
    pytest.importorskip("rapidocr_onnxruntime")
    from phone_console.vision import TextLocator, create_engine

    source = H264StreamSource(
        ["cat", str(stream_file)], container="matroska", restart=False
    )
    try:
        frame = source.grab(timeout=10)
    finally:
        source.close()
    engine = create_engine()
    items = engine.recognize(frame.image)
    match = TextLocator(screen=(1080, 2400)).find(items, "设置", frame.size)
    assert match is not None
    assert abs(match.device_center[0] - 133) <= 25
    assert abs(match.device_center[1] - 168) <= 25


def fake_scrcpy(path):
    path.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "--version" ]; then echo "scrcpy 4.1"; exit 0; fi\n'
        'out=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in --record=*) out="${arg#--record=}";; esac\n'
        'done\n'
        'exec cat "$FIXTURE" > "$out"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_scrcpy_source_exports_adb_path(tmp_path) -> None:
    adb = tmp_path / "adb"
    adb.write_text("#!/bin/sh\nexit 0\n")
    adb.chmod(0o755)
    source = ScrcpySource(
        scrcpy=str(fake_scrcpy(tmp_path / "fake-scrcpy")),
        adb=str(adb),
        version="2.4",
    )
    try:
        assert source.adb == str(adb)
        assert source.stats()["env"]["ADB"] == str(adb)
    finally:
        source.close()


def test_stream_source_passes_env_to_child(stream_file) -> None:
    source = H264StreamSource(
        [
            "sh",
            "-c",
            'test "$ADB" = "/opt/adb" && exec cat "$FIXTURE"',
        ],
        container="matroska",
        restart=False,
        env={"ADB": "/opt/adb", "FIXTURE": str(stream_file)},
    )
    try:
        frame = source.grab(timeout=10)
        assert frame.size == (1080, 2400)
    finally:
        source.close()


def test_service_find_reads_live_stream(stream_file, tmp_path, monkeypatch) -> None:
    pytest.importorskip("rapidocr_onnxruntime")
    from phone_console.device import Device
    from phone_console.phone import DevicePhone
    from phone_console.service import PhoneService
    from phone_console.transport import MockTransport

    monkeypatch.setenv("FIXTURE", str(stream_file))
    source = ScrcpySource(scrcpy=str(fake_scrcpy(tmp_path / "fake-scrcpy")))
    phone = DevicePhone(Device(MockTransport()), source)
    service = PhoneService(phone)
    try:
        payload = service.find("设置", min_score=0.6)
        assert payload["found"] is True
        assert payload["match"]["device_center"] == [134, 168]
        capture = service.status()["capture"]
        assert capture["name"] == "scrcpy:default"
        assert capture["stats"]["frames"] >= 1
    finally:
        phone.close()


def test_scrcpy_source_decodes_with_fake_binary(tmp_path, stream_file, monkeypatch) -> None:
    monkeypatch.setenv("FIXTURE", str(stream_file))
    source = ScrcpySource(
        scrcpy=str(fake_scrcpy(tmp_path / "fake-scrcpy")), serial="S1"
    )
    try:
        assert source.name == "scrcpy:S1"
        assert source.version == "4.1"
        assert "--no-playback" in source.command
        assert any(arg.startswith("--record=") for arg in source.command)
        assert source.stats()["mode"] == "fifo"
        frame = source.grab(timeout=15)
        assert frame.size == (1080, 2400)
    finally:
        source.close()


def test_decode_annexb_stream(annexb_file) -> None:
    from phone_console.vision.stream import decode_annexb_stream

    with open(annexb_file, "rb") as stream:
        frames = []
        for array in decode_annexb_stream(stream):
            frames.append(array)
    assert len(frames) >= 1
    assert frames[0].shape[2] == 3


def test_screenrecord_source_decodes_raw_h264(tmp_path, annexb_file, monkeypatch) -> None:
    fake = tmp_path / "adb"
    fake.write_text('#!/bin/sh\nexec cat "$FIXTURE"\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("FIXTURE", str(annexb_file))
    source = ScreenrecordSource(adb=str(fake), restart=False, startup_timeout=10)
    try:
        frame = source.grab(timeout=10)
        assert frame.size == (1080, 2400)
        assert source.stats()["frames"] >= 1
    finally:
        source.close()


def test_screenrecord_source_wraps_adb_command(tmp_path) -> None:
    fake = tmp_path / "adb"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    source = ScreenrecordSource(adb=str(fake), serial="SERIAL")
    try:
        assert source.command[1:3] == ["-s", "SERIAL"]
        assert source.container == "h264"
        assert source.name == "screenrecord:SERIAL"
    finally:
        source.close()


# --------------------------------------------------------------------- fifo
def test_fifo_open_times_out_when_the_child_never_records(tmp_path) -> None:
    """A scrcpy that dies before recording must not block the reader forever."""
    source = H264StreamSource(
        ["/bin/sh", "-c", "sleep 5"],
        container="matroska",
        fifo=str(tmp_path / "video.mkv"),
        open_timeout=0.3,
        restart=False,
        startup_timeout=5,
    )
    started = time.monotonic()
    try:
        with pytest.raises(StreamError, match="video FIFO"):
            source.grab(timeout=1.5)
    finally:
        source.close()
    assert time.monotonic() - started < 3.0


def test_fifo_open_reports_a_child_that_never_opens_it(tmp_path) -> None:
    source = H264StreamSource(
        ["/bin/sh", "-c", "exit 3"],
        container="matroska",
        fifo=str(tmp_path / "video.mkv"),
        open_timeout=5,
        restart=False,
        startup_timeout=5,
    )
    try:
        with pytest.raises(StreamError, match="exited before"):
            source.grab(timeout=1.5)
    finally:
        source.close()


def test_fifo_waits_for_a_late_writer(tmp_path, stream_file) -> None:
    out = tmp_path / "video.mkv"
    source = H264StreamSource(
        ["/bin/sh", "-c", 'sleep 0.4; cat "$FIXTURE" > "$OUT"'],
        container="matroska",
        fifo=str(out),
        open_timeout=5,
        restart=False,
        startup_timeout=10,
        env={"FIXTURE": str(stream_file), "OUT": str(out)},
    )
    try:
        frame = source.grab(timeout=10)
        assert frame.size == (1080, 2400)
    finally:
        source.close()


# ------------------------------------------------------------------- ttl
def _stale_pair():
    from phone_console.vision.frame import Frame

    stale = Frame(
        np.zeros((20, 10, 3), dtype=np.uint8), ts=time.time() - 60.0, source="old"
    )
    fresh = Frame(
        np.ones((20, 10, 3), dtype=np.uint8), ts=time.time(), source="adb"
    )
    return stale, fresh


def test_scrcpy_grab_takes_an_adb_fallback_for_an_old_frame(tmp_path) -> None:
    stale, fresh = _stale_pair()
    source = ScrcpySource(
        scrcpy=str(fake_scrcpy(tmp_path / "fake-scrcpy")),
        fallback_after=1.0,
        restart=False,
    )
    try:
        source._frame = stale
        source._fallback_cache = fresh
        source._fallback_grab = lambda: fresh
        assert source.grab() is fresh
        stats = source.stats()
        assert stats["age_ms"] > 50_000
        assert stats["effective_age_ms"] < 1_000
        assert stats["fallback_age_ms"] is not None
    finally:
        source.close()


def test_scrcpy_grab_keeps_the_frame_when_the_fallback_fails(tmp_path) -> None:
    stale, _ = _stale_pair()
    source = ScrcpySource(
        scrcpy=str(fake_scrcpy(tmp_path / "fake-scrcpy")),
        fallback_after=1.0,
        restart=False,
    )
    try:
        source._frame = stale
        source._fallback_grab = lambda: None
        assert source.grab() is stale
        assert source.stats()["effective_age_ms"] > 50_000
    finally:
        source.close()


def test_prefix_stream_replays_probe_bytes() -> None:
    import io

    from phone_console.vision.stream import _PrefixStream

    stream = _PrefixStream(b"abc", io.BytesIO(b"defg"))
    assert stream.seekable() is False
    assert stream.read(2) == b"ab"
    assert stream.read(2) == b"cd"
    assert stream.read() == b"efg"
    stream.close()


class _AliveProcess:
    """Minimal stand-in for a still-running scrcpy child process."""

    def poll(self):
        return None

    def terminate(self) -> None:
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        return None


def test_scrcpy_grab_reuses_a_static_frame_while_the_capture_process_is_alive(
    tmp_path,
) -> None:
    stale, fresh = _stale_pair()
    source = ScrcpySource(
        scrcpy=str(fake_scrcpy(tmp_path / "fake-scrcpy")),
        fallback_after=1.0,
        restart=False,
    )
    try:
        source._frame = stale
        source._proc = _AliveProcess()
        fallback_calls = []
        source._fallback_grab = lambda: fallback_calls.append(1) or fresh
        # A static screen keeps the last decoded frame current: no slow adb
        # screencap (2-7s over WiFi) just because nothing moved.
        assert source.grab() is stale
        assert fallback_calls == []
        assert source.stats()["process_alive"] is True
    finally:
        source.close()


def test_fatal_capture_rearms_only_after_the_retry_interval(tmp_path) -> None:
    source = ScrcpySource(
        scrcpy=str(fake_scrcpy(tmp_path / "fake-scrcpy")),
        restart=False,
        fatal_retry_interval=60.0,
    )
    try:
        source._fatal = True
        source._error = "no ADB device"
        source._fatal_at = time.time()
        source._retry_after_fatal()
        assert source._fatal is True, "cooldown has not elapsed yet"

        source._fatal_at = time.time() - 120.0
        source._retry_after_fatal()
        assert source._fatal is False
        assert source._error is None
    finally:
        source.close()
