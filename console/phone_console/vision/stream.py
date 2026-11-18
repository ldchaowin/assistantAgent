"""Live H.264 frame sources: scrcpy and adb screenrecord.

The phone pushes an H.264 stream over USB; a background thread demuxes and
decodes it with PyAV and keeps **the latest frame** available. Consumers
(OCR, scenarios, the local service) call :meth:`grab` and always get the most
recent picture instead of waiting for a new screenshot round-trip.

Two transports are provided:

``ScrcpySource``
    ``scrcpy --record=- --record-format=mkv``; low latency, needs scrcpy.
``ScreenrecordSource``
    ``adb exec-out screenrecord --output-format=h264 -``; works with plain
    platform-tools (higher latency, restarts every ``--time-limit`` seconds).

Both write a byte stream to stdout, so :class:`H264StreamSource` is a generic
"command -> decoded frames" source and is easy to test without a phone.
"""
from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from typing import Any, Callable, Iterator, Sequence

import numpy as np

from .adb import AdbError, resolve_adb
from .frame import Frame

# scrcpy's default is 8 Mbps; enough for a 1024px-wide phone frame.
DEFAULT_BIT_RATE = 8_000_000
DEFAULT_MAX_SIZE = 1024
DEFAULT_MAX_FPS = 30

# How long to wait for the child to create the writer side of the record FIFO.
# Without this, a scrcpy that dies before recording leaves the reader blocked
# in open() forever and the source keeps serving the last frame.
DEFAULT_OPEN_TIMEOUT = 10.0

# A decoded frame older than this is treated as "the stream stalled": the
# ScrcpySource falls back to an adb screencap so callers still see the screen.
DEFAULT_FALLBACK_AFTER = 2.5
DEFAULT_FALLBACK_CACHE = 1.5
DEFAULT_FALLBACK_RETRY = 5.0

# A capture that gave up on a device hint ("no ADB device") retries on the next
# grab after this cooldown, so a phone that was offline at service start heals
# by itself instead of needing a service restart.
DEFAULT_FATAL_RETRY_INTERVAL = 15.0

_CONTAINER_FORMATS = {"matroska": "matroska", "mkv": "matroska", "h264": "h264"}

# After a child exits without producing a frame (wrong command, unauthorized
# device), retry with exponential backoff instead of hammering adb/scrcpy.
MAX_RESTART_DELAY = 5.0

# macOS/scrcpy stderr noise that is never useful to a phone-automation user.
_STDERR_NOISE = re.compile(
    r"(Keyboard Layouts|TISFileInterrogator|objc\[|duplicate keyboard)",
    re.IGNORECASE,
)

# Startup failures that need a human (plug the phone / tap Allow). When the
# stream has never produced a frame we stop immediately and surface the hint.
_FATAL_HINTS = (
    (
        "could not find any adb device",
        "no ADB device: connect the phone and enable USB debugging "
        "(check with `phone adb status`)",
    ),
    (
        "could not find adb device",
        "no matching ADB device: check the serial with `phone adb status`",
    ),
    (
        "device is unauthorized",
        "device unauthorized: tap Allow / 始终允许 on the phone "
        "(wait with `phone adb wait`)",
    ),
    (
        "adb: no devices",
        "no ADB device: connect the phone (check with `phone adb status`)",
    ),
)


def restart_backoff(base: float, empty_restarts: int, cap: float = MAX_RESTART_DELAY) -> float:
    if empty_restarts <= 0:
        return max(0.0, base)
    return min(max(0.0, cap), max(0.0, base) * (2 ** (empty_restarts - 1)))


class _PrefixStream:
    """File-like view that replays bytes read while probing a FIFO.

    Opening the record FIFO non-blocking lets us wait for scrcpy with a
    timeout, but the first read may already contain stream data. Those bytes
    are handed back first, then reads go to the real file object.
    """

    def __init__(self, prefix: bytes, stream: Any) -> None:
        self._prefix = prefix
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        if not self._prefix:
            return self._stream.read(size)
        if size is None or size < 0:
            data, self._prefix = self._prefix, b""
            return data + self._stream.read()
        if size <= len(self._prefix):
            data = self._prefix[:size]
            self._prefix = self._prefix[size:]
            return data
        data, self._prefix = self._prefix, b""
        return data + self._stream.read(size - len(data))

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def close(self) -> None:
        self._stream.close()

    @property
    def closed(self) -> bool:
        return bool(getattr(self._stream, "closed", False))


class StreamError(RuntimeError):
    """The stream could not be started or produced no frame."""


def av_version() -> str | None:
    """Return the installed PyAV version without importing the library."""
    try:
        from importlib.metadata import version as package_version

        return package_version("av")
    except Exception:  # noqa: BLE001 - fall through to the import check
        pass
    try:
        import av  # noqa: F401
    except ImportError:  # pragma: no cover - depends on env
        return None
    return getattr(av, "__version__", "unknown")


def decode_h264_stream(
    stream: Any, container: str = "matroska"
) -> Iterator[np.ndarray]:
    """Decode a byte stream to BGR numpy frames.

    ``stream`` is any file-like object (``subprocess.Popen.stdout`` in
    production, a file object in tests). ``container`` must be the stream's
    actual container: ``matroska`` for scrcpy recordings, ``h264`` for raw
    NAL units from ``screenrecord``.
    """
    try:
        import av
    except ImportError as exc:  # pragma: no cover - depends on env
        raise StreamError(
            "PyAV is not installed; install the stream extras:\n"
            "  .venv/bin/pip install -e 'console[stream]'"
        ) from exc
    fmt = _CONTAINER_FORMATS.get(container, container)
    with av.open(stream, mode="r", format=fmt) as opened:
        for packet in opened.demux():
            for frame in packet.decode():
                yield frame.to_ndarray(format="bgr24")


def decode_annexb_stream(
    stream: Any, container: str | None = None, chunk_size: int = 65536
) -> Iterator[np.ndarray]:
    """Decode a raw Annex-B H.264 byte stream (e.g. ``adb screenrecord``).

    ``av.open(..., format="h264")`` buffers until a full access unit is
    available, which can stall for seconds on a mostly static screen. Feeding
    the byte stream to :meth:`av.CodecContext.parse` gives us packets as soon
    as the parser can delimit them; we flush the parser when the stream goes
    idle so a static screen still yields its latest frame.
    """
    try:
        import av
    except ImportError as exc:  # pragma: no cover - depends on env
        raise StreamError(
            "PyAV is not installed; install the stream extras:\n"
            "  .venv/bin/pip install -e 'console[stream]'"
        ) from exc
    codec = av.CodecContext.create("h264", "r")
    pending = False
    while True:
        try:
            chunk = stream.read(chunk_size)
        except (OSError, ValueError) as exc:
            raise StreamError(f"stream read failed: {exc}") from exc
        if not chunk:
            break
        pending = True
        packets = codec.parse(chunk)
        for packet in packets:
            for frame in codec.decode(packet):
                yield frame.to_ndarray(format="bgr24")
        if not packets and pending:
            # Static screen: flush the parser so the buffered access unit is
            # decoded instead of waiting for the next one.
            for packet in codec.parse(b""):
                for frame in codec.decode(packet):
                    yield frame.to_ndarray(format="bgr24")
            pending = False
    try:
        for packet in codec.parse(b""):
            for frame in codec.decode(packet):
                yield frame.to_ndarray(format="bgr24")
        for frame in codec.decode(None):
            yield frame.to_ndarray(format="bgr24")
    except EOFError:
        pass


class H264StreamSource:
    """Decode a live H.264 stream from a subprocess into latest-frame grabs."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        name: str | None = None,
        container: str = "matroska",
        startup_timeout: float = 12.0,
        open_timeout: float = DEFAULT_OPEN_TIMEOUT,
        restart: bool = True,
        restart_delay: float = 1.0,
        max_restarts: int = 100,
        decoder: Callable[[Any, str], Iterator[np.ndarray]] | None = None,
        popen: Callable[..., Any] | None = None,
        stderr_lines: int = 40,
        env: dict[str, str] | None = None,
        fifo: str | None = None,
        fatal_retry_interval: float = DEFAULT_FATAL_RETRY_INTERVAL,
    ) -> None:
        self.command = list(command)
        self._fifo_dir: str | None = None
        if fifo is not None:
            fifo = self._make_fifo(fifo)
        self._fifo = fifo
        self._env = {str(key): str(value) for key, value in (env or {}).items()}
        self.container = container
        self.startup_timeout = float(startup_timeout)
        self.open_timeout = max(0.1, float(open_timeout))
        self.restart = bool(restart)
        self.restart_delay = float(restart_delay)
        self.max_restarts = int(max_restarts)
        if decoder is None and av_version() is None:
            raise StreamError(
                "PyAV is not installed; install the stream extras:\n"
                "  .venv/bin/pip install -e 'console[stream]'"
            )
        self._decoder = decoder or decode_h264_stream
        self._popen = popen or subprocess.Popen
        self._name = name or f"stream:{self.command[0]}"
        self._stderr_tail: deque[str] = deque(maxlen=max(1, stderr_lines))
        self._cond = threading.Condition()
        self._frame: Frame | None = None
        self._recent: deque[float] = deque(maxlen=240)
        self._closing = False
        self._error: str | None = None
        self._fatal = False
        self._fatal_at = 0.0
        self.fatal_retry_interval = max(0.0, float(fatal_retry_interval))
        self._proc: Any = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._frames = 0
        self._restarts = 0
        self._empty_restarts = 0
        self._started_at = 0.0

    # ------------------------------------------------------------------ fifo
    @staticmethod
    def _make_fifo(path: str) -> str:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        os.mkfifo(path, 0o600)
        return path

    # ------------------------------------------------------------------ props
    @property
    def name(self) -> str:
        return self._name

    def _process_alive(self) -> bool:
        """Whether the capture child process is currently running.

        Distinct from ``stats()["running"]``: the supervision thread stays alive
        across restarts, while ``_proc`` is only alive while frames can arrive.
        """
        proc = self._proc
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except (OSError, ValueError):  # pragma: no cover - defensive
            return False

    def stats(self) -> dict[str, Any]:
        with self._cond:
            recent = list(self._recent)
            frame = self._frame
            frames = self._frames
            restarts = self._restarts
            error = self._error
            started = self._started_at
        fps = 0.0
        if len(recent) > 1:
            span = recent[-1] - recent[0]
            if span > 0:
                fps = (len(recent) - 1) / span
        return {
            "name": self._name,
            "command": " ".join(self.command),
            "frames": frames,
            "fps": round(fps, 2),
            "env": dict(self._env),
            "mode": "fifo" if self._fifo else "stdout",
            "age_ms": round((time.time() - frame.ts) * 1000.0, 1) if frame else None,
            "size": list(frame.size) if frame else None,
            "restarts": restarts,
            "fallback_frames": int(getattr(self, "_fallback_frames", 0)),
            "restart_delay_s": round(
                restart_backoff(self.restart_delay, getattr(self, "_empty_restarts", 0)), 2
            ),
            "running": bool(self._reader and self._reader.is_alive()),
            "process_alive": self._process_alive(),
            "uptime_s": round(time.time() - started, 1) if started else 0.0,
            "error": error,
            "stderr": "\n".join(self._stderr_tail),
        }

    def _fatal_hint(self) -> str | None:
        text = "\n".join(self._stderr_tail).lower()
        for needle, hint in _FATAL_HINTS:
            if needle in text:
                return hint
        return None

    def _stderr_summary(self, lines: int = 3) -> str:
        tail = list(self._stderr_tail)[-lines:]
        return "\n".join(tail)

    # ------------------------------------------------------------------ start
    def _ensure_started(self) -> None:
        with self._cond:
            if self._closing:
                raise StreamError("stream source is closed")
            if self._reader is not None:
                return
            self._started_at = time.time()
            self._reader = threading.Thread(
                target=self._run, name=f"{self._name}-reader", daemon=True
            )
            self._reader.start()

    def _spawn(self) -> Any:
        environment = None
        if self._env:
            environment = {**os.environ, **self._env}
        stdout = subprocess.DEVNULL if self._fifo else subprocess.PIPE
        proc = self._popen(
            self.command,
            stdout=stdout,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=environment,
        )
        self._proc = proc
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr,
            args=(proc,),
            name=f"{self._name}-stderr",
            daemon=True,
        )
        self._stderr_reader.start()
        return proc

    def _open_stream(self, proc: Any) -> Any:
        if self._fifo:
            return self._open_fifo(proc)
        return proc.stdout

    def _open_fifo(self, proc: Any) -> Any:
        """Open the record FIFO, giving up when the child never writes to it.

        ``open(fifo, "rb")`` blocks until a writer appears. When scrcpy exits
        before recording (``Device disconnected``), no writer ever appears and
        the reader thread would block forever while ``grab()`` kept serving
        the last frame. Poll for a writer instead and fail fast so the restart
        loop can report the problem.
        """
        deadline = time.monotonic() + self.open_timeout
        fd = os.open(self._fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            while True:
                if self._closing:
                    raise StreamError("stream source is closed")
                ready, _, _ = select.select([fd], [], [], 0.05)
                if ready:
                    try:
                        chunk = os.read(fd, 1 << 16)
                    except BlockingIOError:
                        chunk = b""
                    except OSError as exc:
                        raise StreamError(
                            f"{self._name}: cannot read {self._fifo}: {exc}"
                        ) from exc
                    if chunk:
                        os.set_blocking(fd, True)
                        stream = os.fdopen(fd, "rb", buffering=0)
                        fd = -1
                        return _PrefixStream(chunk, stream)
                    if proc.poll() is not None:
                        raise StreamError(
                            f"{self._name}: exited before writing to the video FIFO"
                            + self._stderr_detail()
                        )
                    time.sleep(0.02)
                    continue
                if proc.poll() is not None:
                    raise StreamError(
                        f"{self._name}: exited before opening the video FIFO"
                        + self._stderr_detail()
                    )
                if time.monotonic() >= deadline:
                    raise StreamError(
                        f"{self._name}: timed out after {self.open_timeout:.1f}s "
                        "waiting for the video FIFO (the capture child never "
                        "started recording)" + self._stderr_detail()
                    )
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _stderr_detail(self) -> str:
        tail = self._stderr_summary()
        return f"\n{tail}" if tail else ""

    def _drain_stderr(self, proc: Any) -> None:
        stream = getattr(proc, "stderr", None)
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line and not _STDERR_NOISE.search(line):
                    self._stderr_tail.append(line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _run(self) -> None:
        empty_restarts = 0
        while not self._closing:
            before = self._frames
            try:
                proc = self._spawn()
            except Exception as exc:  # noqa: BLE001 - reported through stats/grab
                with self._cond:
                    self._error = f"could not start {self.command[0]}: {exc}"
                    self._cond.notify_all()
                return
            try:
                source = self._open_stream(proc)
                try:
                    for array in self._decoder(source, self.container):
                        if self._closing:
                            break
                        frame = Frame(array, ts=time.time(), source=self._name)
                        with self._cond:
                            self._frame = frame
                            self._frames += 1
                            self._recent.append(frame.ts)
                            self._error = None
                            self._cond.notify_all()
                finally:
                    try:
                        source.close()
                    except (OSError, ValueError):
                        pass
            except Exception as exc:  # noqa: BLE001 - stream must not crash callers
                with self._cond:
                    self._error = f"{type(exc).__name__}: {exc}"
                    self._cond.notify_all()
            finally:
                self._terminate(proc)
            if self._closing or not self.restart:
                break
            stderr_reader = self._stderr_reader
            if stderr_reader is not None and stderr_reader.is_alive():
                stderr_reader.join(timeout=0.5)
            hint = self._fatal_hint() if self._frames == 0 else None
            if hint:
                with self._cond:
                    self._error = hint
                    self._fatal = True
                    self._fatal_at = time.time()
                    self._cond.notify_all()
                break
            with self._cond:
                if self._restarts >= self.max_restarts:
                    self._error = "stream restart limit reached"
                    self._cond.notify_all()
                    break
                self._restarts += 1
            if self._frames > before:
                empty_restarts = 0
            else:
                empty_restarts += 1
            self._empty_restarts = empty_restarts
            delay = restart_backoff(self.restart_delay, empty_restarts)
            if delay:
                time.sleep(delay)
        with self._cond:
            self._cond.notify_all()

    @staticmethod
    def _terminate(proc: Any) -> None:
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------- grab
    def _retry_after_fatal(self) -> None:
        """Re-arm a capture that gave up on a device hint (phone was offline)."""
        if not self._fatal:
            return
        with self._cond:
            if not self._fatal:
                return
            if time.time() - self._fatal_at < self.fatal_retry_interval:
                return
            self._fatal = False
            self._error = None
            self._reader = None
        self._ensure_started()

    def grab(self, timeout: float | None = None) -> Frame:
        """Return the latest frame, waiting for the first one if necessary."""
        self._retry_after_fatal()
        self._ensure_started()
        deadline = time.monotonic() + (
            self.startup_timeout if timeout is None else float(timeout)
        )
        with self._cond:
            while self._frame is None:
                if self._fatal:
                    raise self._startup_error()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._startup_error()
                self._cond.wait(min(remaining, 0.25))
            return self._frame

    def _startup_error(self) -> StreamError:
        detail = (
            f"no frame received: {self._error}" if self._error else "no frame received"
        )
        tail = self._stderr_summary()
        return StreamError(f"{self._name}: {detail}" + (f"\n{tail}" if tail else ""))

    def latest(self) -> Frame | None:
        with self._cond:
            return self._frame

    def wait_newer(self, after: float, timeout: float = 2.0) -> Frame:
        """Wait for a frame decoded after ``after`` (a ``Frame.ts``)."""
        self._ensure_started()
        deadline = time.monotonic() + float(timeout)
        with self._cond:
            while self._frame is None or self._frame.ts <= after:
                if self._fatal:
                    raise self._startup_error()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = f" ({self._error})" if self._error else ""
                    raise StreamError(
                        f"{self._name}: no new frame within {timeout:.2f}s{detail}"
                    )
                self._cond.wait(min(remaining, 0.25))
            return self._frame

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        with self._cond:
            if self._closing:
                return
            self._closing = True
            proc = self._proc
            self._cond.notify_all()
        self._terminate(proc)
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        proc = self._proc
        if proc is not None:
            stream = getattr(proc, "stdout", None)
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        if self._fifo:
            try:
                os.unlink(self._fifo)
            except OSError:
                pass
            fifo_dir = os.path.dirname(self._fifo)
            if fifo_dir and os.path.basename(fifo_dir).startswith("phone-scrcpy-"):
                try:
                    os.rmdir(fifo_dir)
                except OSError:
                    pass

    def __enter__(self) -> "H264StreamSource":
        self._ensure_started()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# --------------------------------------------------------------------- scrcpy
def scrcpy_version(scrcpy: str = "scrcpy", timeout: float = 5.0) -> str | None:
    """Return the installed scrcpy version (e.g. ``"2.4"``), or None."""
    try:
        proc = subprocess.run(
            [scrcpy, "--version"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    text += (proc.stderr or b"").decode("utf-8", errors="replace")
    match = re.search(r"scrcpy\s+(\d+\.\d+(?:\.\d+)?)", text, re.IGNORECASE)
    return match.group(1) if match else None


def scrcpy_command(
    *,
    scrcpy: str = "scrcpy",
    serial: str | None = None,
    max_size: int = DEFAULT_MAX_SIZE,
    max_fps: int = DEFAULT_MAX_FPS,
    bit_rate: int = DEFAULT_BIT_RATE,
    version: str | None = None,
    record: str = "-",
    extra: Sequence[str] = (),
) -> list[str]:
    """Build the scrcpy command that writes an MKV stream to stdout."""
    major = 2
    if version:
        try:
            major = int(str(version).split(".", 1)[0])
        except ValueError:
            major = 2
    command = [scrcpy]
    if serial:
        command += ["-s", str(serial)]
    if major >= 2:
        command += ["--no-playback", "--no-audio"]
    else:  # scrcpy 1.x
        command += ["--no-display"]
    command += [
        # Control stays disabled: taps/swipes go through the ESP32 HID hand.
        # (--stay-awake is incompatible with --no-control, so we do not use it.)
        "--no-control",
        "--video-codec=h264",
        f"--max-size={int(max_size)}",
        f"--max-fps={int(max_fps)}",
        f"--video-bit-rate={int(bit_rate)}",
        # scrcpy 4.x no longer treats "-" as stdout: recording to a FIFO is the
        # low-latency way to get the encoded stream out of the process.
        f"--record={record}",
        "--record-format=mkv",
    ]
    command += [str(item) for item in extra]
    return command


class ScrcpySource(H264StreamSource):
    """Live frames through scrcpy's own capture pipeline (low latency)."""

    def __init__(
        self,
        serial: str | None = None,
        *,
        scrcpy: str | None = None,
        adb: str | None = None,
        max_size: int = DEFAULT_MAX_SIZE,
        max_fps: int = DEFAULT_MAX_FPS,
        bit_rate: int = DEFAULT_BIT_RATE,
        version: str | None = None,
        extra: Sequence[str] = (),
        **kwargs: Any,
    ) -> None:
        executable = scrcpy or shutil.which("scrcpy") or "scrcpy"
        if shutil.which(executable) is None:
            raise StreamError(
                "scrcpy not found; install it (macOS: brew install scrcpy) or "
                "use --screenrecord for the adb-only fallback"
            )
        # scrcpy looks for "adb" on PATH unless ADB points at an executable.
        # The repository ships adb under .tools/, so hand it over explicitly.
        env = dict(kwargs.pop("env", {}) or {})
        resolved_adb = None
        try:
            resolved_adb = resolve_adb(adb)
            env.setdefault("ADB", resolved_adb)
        except AdbError:
            pass
        self.serial = serial or None
        self._adb_binary = resolved_adb
        self._fallback_enabled = bool(kwargs.pop("fallback", True))
        self._fallback_after = float(
            kwargs.pop("fallback_after", DEFAULT_FALLBACK_AFTER)
        )
        self._fallback_cache_s = float(
            kwargs.pop("fallback_cache", DEFAULT_FALLBACK_CACHE)
        )
        self._fallback_retry_s = float(
            kwargs.pop("fallback_retry", DEFAULT_FALLBACK_RETRY)
        )
        self._fallback_source = None
        self._fallback_cache = None
        self._fallback_retry_after = 0.0
        self._fallback_frames = 0
        detected = version or scrcpy_version(executable)
        fifo_dir = tempfile.mkdtemp(prefix="phone-scrcpy-")
        fifo_path = os.path.join(fifo_dir, "video.mkv")
        command = scrcpy_command(
            scrcpy=executable,
            serial=serial,
            max_size=max_size,
            max_fps=max_fps,
            bit_rate=bit_rate,
            version=detected,
            record=fifo_path,
            extra=extra,
        )
        super().__init__(
            command,
            name=f"scrcpy:{serial or 'default'}",
            container="matroska",
            env=env,
            fifo=fifo_path,
            **kwargs,
        )
        self.version = detected

    @property
    def adb(self) -> str | None:
        return self._env.get("ADB")

    def _fallback_grab(self):
        """Still frame through adb screencap when the stream is not current.

        Android only emits encoded frames when the screen content changes, so
        an idle screen can leave the stream without any frame; a dead ADB
        tunnel looks exactly the same from here. A screencap keeps
        `find`/`tap`/`serve` honest until the next encoded frame arrives.

        Returns None when adb is not usable (the caller then keeps the old
        frame and the service rejects it as stale).
        """
        if not self._fallback_enabled:
            return None
        now = time.time()
        cached = self._fallback_cache
        if cached is not None and (now - cached.ts) < self._fallback_cache_s:
            return cached
        if now < self._fallback_retry_after:
            return None
        try:
            if self._fallback_source is None:
                from .sources import AdbSource

                self._fallback_source = AdbSource(
                    serial=self.serial, adb=self._adb_binary
                )
            raw = self._fallback_source.grab()
        except Exception:  # noqa: BLE001 - fallback is best effort
            self._fallback_retry_after = now + self._fallback_retry_s
            return None
        frame = Frame(raw.image, ts=time.time(), source=self._name)
        self._fallback_cache = frame
        self._fallback_frames += 1
        return frame

    def grab(self, timeout: float | None = None):
        # A decoded frame is by definition the current screen: Android only
        # emits frames when the content changes, so the latest packet is not
        # "stale", it means nothing moved. While the capture child is alive we
        # therefore keep serving it (a static screen); only when the process is
        # gone does an old frame mean the stream died, and only then is an adb
        # screencap (slow, 2-7s over WiFi) worth taking.
        frame = self.latest()
        if frame is not None:
            if (time.time() - frame.ts) <= self._fallback_after:
                return frame
            if self._process_alive():
                return frame
            fallback = self._fallback_grab()
            return fallback if fallback is not None else frame
        budget = 0.4 if timeout is None else min(float(timeout), 0.4)
        try:
            return super().grab(budget)
        except StreamError:
            fallback = self._fallback_grab()
            if fallback is None:
                raise
            return fallback

    def stats(self) -> dict[str, Any]:
        data = super().stats()
        fallback = self._fallback_cache
        fallback_age = (
            round((time.time() - fallback.ts) * 1000.0, 1)
            if fallback is not None
            else None
        )
        ages = [
            value for value in (data.get("age_ms"), fallback_age) if value is not None
        ]
        data["fallback_age_ms"] = fallback_age
        # What a grab() right now would hand back: the decoded frame, or the
        # adb fallback when that one is fresher.
        data["effective_age_ms"] = min(ages) if ages else None
        data["fallback_after_ms"] = round(self._fallback_after * 1000.0, 1)
        return data


# ----------------------------------------------------------------- screenrecord
def screenrecord_command(
    *,
    adb: str = "adb",
    serial: str | None = None,
    bit_rate: int = 20_000_000,
    time_limit: int = 180,
    size: str | None = None,
) -> list[str]:
    """Build the adb ``screenrecord`` command that streams H.264 to stdout."""
    command = [adb]
    if serial:
        command += ["-s", str(serial)]
    command += [
        "exec-out",
        "screenrecord",
        "--output-format=h264",
        f"--bit-rate={int(bit_rate)}",
        f"--time-limit={int(time_limit)}",
    ]
    if size:
        command.append(f"--size={size}")
    command.append("-")
    return command


class ScreenrecordSource(H264StreamSource):
    """Live frames from Android's built-in ``screenrecord`` (no scrcpy)."""

    def __init__(
        self,
        serial: str | None = None,
        *,
        adb: str | None = None,
        bit_rate: int = 20_000_000,
        time_limit: int = 180,
        size: str | None = None,
        **kwargs: Any,
    ) -> None:
        executable = resolve_adb(adb)
        command = screenrecord_command(
            adb=executable,
            serial=serial,
            bit_rate=bit_rate,
            time_limit=time_limit,
            size=size,
        )
        kwargs.setdefault("restart_delay", 0.5)
        kwargs.setdefault("decoder", decode_annexb_stream)
        super().__init__(
            command,
            name=f"screenrecord:{serial or 'default'}",
            container="h264",
            **kwargs,
        )
