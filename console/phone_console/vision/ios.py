"""The iOS eye: find an iPhone and capture its screen on macOS.

The Android path is ``adb`` + ``scrcpy``. iPhones expose no adb; instead the
iPhone screen can be captured **the way QuickTime Player does it**: a trusted,
unlocked iPhone appears as an AVFoundation video device on the Mac and ffmpeg
can read it as an MJPEG stream. This needs no Xcode, no developer mode and no
app installation -- only a data cable, "Trust This Computer", and macOS.

Discovery is layered so diagnostics work without any optional dependency:

1. ``usbmuxd`` (part of macOS) over its unix socket -- a tiny in-process plist
   protocol. Tells us whether an iOS *device* is physically connected.
2. ``ffmpeg -f avfoundation -list_devices`` -- tells us whether the screen
   *capture* device is available (trusted + unlocked + not busy).
3. One-shot fallbacks for headless/remote setups:
   ``pymobiledevice3`` (Developer Mode + tunnel) and ``idevicescreenshot``
   (libimobiledevice + mounted developer disk image).

Public entry points: :func:`ios_status`, :func:`wait_for_ios`,
:func:`open_ios_source`, :func:`capture_once`, :class:`IosMjpegSource`,
:class:`IosStillSource`.
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import signal
import sys
import uuid
import plistlib
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Sequence

from .frame import Frame

PLATFORM = "ios"

# Environment variables checked, in order, before falling back to PATH.
FFMPEG_ENV_VARS = ("PHONE_IOS_FFMPEG", "PHONE_FFMPEG", "FFMPEG")
USBMUXD_ENV_VAR = "PHONE_USBMUXD"

FFMPEG_CANDIDATES = (
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
)

# usbmuxd (libusbmuxd) protocol constants.
_USBMUXD_MESSAGE_PLIST = 8
_USBMUXD_VERSION = 1
_USBMUXD_TAG = 1
_USBMUXD_PROG = "phone-console"
_DEFAULT_USBMUXD = "/var/run/usbmuxd"

_CAPTURE_VIDEO_HEADER = "AVFoundation video devices:"
_CAPTURE_AUDIO_HEADER = "AVFoundation audio devices:"
_DEVICE_LINE = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")

# Localized names still contain the product name; macOS system screen capture
# devices are named "Capture screen N" (localized on some systems).
_IOS_NAME_HINTS = ("iphone", "ipad", "ipod", "ios", "apple tv")
_SCREEN_NAME_HINTS = ("capture screen", "屏幕")


class IosError(RuntimeError):
    """The iPhone (or its capture device) is missing or unusable."""


# ------------------------------------------------------------------ helpers
def _first_existing(candidates: Sequence[str | None]) -> str | None:
    for candidate in candidates:
        if not candidate:
            continue
        found = shutil.which(candidate)
        if found:
            return found
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def usbmuxd_socket_path() -> str:
    return os.environ.get(USBMUXD_ENV_VAR) or _DEFAULT_USBMUXD


def resolve_ffmpeg(ffmpeg: str | None = None) -> str:
    """Return a usable ffmpeg executable path or raise :class:`IosError`."""
    found = _first_existing(
        (ffmpeg,)
        + tuple(os.environ.get(name) for name in FFMPEG_ENV_VARS)
        + ("ffmpeg",)
        + FFMPEG_CANDIDATES
    )
    if not found:
        raise IosError(
            "ffmpeg 未安装：iOS 屏幕采集需要 ffmpeg（brew install ffmpeg），"
            "或用 PHONE_IOS_FFMPEG 指定路径"
        )
    return found


def tool_availability(ffmpeg: str | None = None) -> dict[str, Any]:
    """Best-effort inventory of the tools the iOS path can use."""
    try:
        ffmpeg_path = resolve_ffmpeg(ffmpeg)
    except IosError:
        ffmpeg_path = None
    native = {
        "helper": "IOSEye",
        "app": str(ios_eye_app()),
        "built": ios_eye_built(),
        "clang": resolve_clang(),
    }
    return {
        "platform": "darwin" if os.uname().sysname == "Darwin" else os.uname().sysname.lower(),
        "native_eye": native,
        "ffmpeg": ffmpeg_path,
        "pymobiledevice3": shutil.which("pymobiledevice3"),
        "idevicescreenshot": shutil.which("idevicescreenshot"),
        "usbmuxd": os.path.exists(usbmuxd_socket_path()),
    }


# --------------------------------------------------------------- usbmuxd
def _usbmuxd_send(sock: socket.socket, message: dict[str, Any]) -> None:
    payload = plistlib.dumps(message)
    header = struct.pack(
        "<IIII", 16 + len(payload), _USBMUXD_VERSION, _USBMUXD_MESSAGE_PLIST, _USBMUXD_TAG
    )
    sock.sendall(header + payload)


def _usbmuxd_recv(sock: socket.socket) -> dict[str, Any]:
    header = b""
    while len(header) < 16:
        chunk = sock.recv(16 - len(header))
        if not chunk:
            raise IosError("usbmuxd closed the connection")
        header += chunk
    length, _version, _message, _tag = struct.unpack("<IIII", header)
    body = b""
    while len(body) < length - 16:
        chunk = sock.recv(length - 16 - len(body))
        if not chunk:
            raise IosError("usbmuxd closed the connection while reading the reply")
        body += chunk
    try:
        return plistlib.loads(body)
    except Exception as exc:  # noqa: BLE001 - corrupt reply is a hard error
        raise IosError(f"usbmuxd reply is not a plist: {exc}") from exc


def usbmux_list(
    socket_path: str | None = None, timeout: float = 3.0
) -> list[dict[str, Any]]:
    """List iOS devices connected to this Mac via usbmuxd.

    Returns one dict per device with ``device_id``, ``serial``,
    ``connection_type`` (``USB`` / ``Network``), ``product_id`` and
    ``location_id``. Unpaired devices are listed too: usbmuxd sees them before
    the "Trust This Computer" prompt is answered.
    """
    path = socket_path or usbmuxd_socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(max(0.2, float(timeout)))
    try:
        try:
            sock.connect(path)
        except FileNotFoundError as exc:
            raise IosError(
                f"usbmuxd socket not found: {path}（iOS 采集需要 macOS；"
                "其它系统可安装 libimobiledevice 并用 pymobiledevice3/idevicescreenshot 回退）"
            ) from exc
        except OSError as exc:
            raise IosError(f"无法连接 usbmuxd ({path}): {exc}") from exc
        _usbmuxd_send(
            sock,
            {
                "MessageType": "ListDevices",
                "ClientVersionString": _USBMUXD_PROG,
                "ProgName": _USBMUXD_PROG,
                "kLibUSBMuxVersion": 3,
            },
        )
        reply = _usbmuxd_recv(sock)
    except socket.timeout as exc:
        raise IosError(f"usbmuxd 在 {timeout:.1f}s 内没有响应") from exc
    finally:
        try:
            sock.close()
        except OSError:
            pass

    devices: list[dict[str, Any]] = []
    for entry in reply.get("DeviceList", []) or []:
        props = entry.get("Properties") or {}
        serial = props.get("SerialNumber") or props.get("UDID") or ""
        devices.append(
            {
                "device_id": entry.get("DeviceID", props.get("DeviceID")),
                "serial": serial,
                "connection_type": props.get("ConnectionType", "USB"),
                "product_id": props.get("ProductID"),
                "location_id": props.get("LocationID"),
            }
        )
    return devices


# ----------------------------------------------------- AVFoundation (ffmpeg)
def parse_ffmpeg_devices(output: str) -> list[dict[str, Any]]:
    """Parse ``ffmpeg -f avfoundation -list_devices`` output into video devices.

    Pure string parsing (no ffmpeg needed) so it is easy to unit-test. Audio
    devices and unrelated ffmpeg chatter are skipped.
    """
    devices: list[dict[str, Any]] = []
    in_video = False
    for raw in output.splitlines():
        if _CAPTURE_VIDEO_HEADER in raw:
            in_video = True
            continue
        if _CAPTURE_AUDIO_HEADER in raw:
            in_video = False
            continue
        if not in_video:
            continue
        match = _DEVICE_LINE.search(raw)
        if match:
            devices.append({"index": int(match.group(1)), "name": match.group(2).strip()})
    return devices


def is_screen_device(device: dict[str, Any]) -> bool:
    name = str(device.get("name", "")).lower()
    return any(hint in name for hint in _SCREEN_NAME_HINTS)


def _looks_like_ios(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _IOS_NAME_HINTS)


def pick_capture_device(
    devices: list[dict[str, Any]], wanted: str | int | None = None
) -> dict[str, Any] | None:
    """Pick the iPhone screen capture device out of ffmpeg's device list.

    ``wanted`` may be an index ("1"/1) or a case-insensitive name fragment.
    Without a hint, iPhone/iPad-named devices win; otherwise a single
    non-Mac-screen device is accepted.
    """
    if not devices:
        return None
    if wanted is not None and str(wanted).strip():
        text = str(wanted).strip()
        if text.isdigit():
            for device in devices:
                if int(device.get("index", -1)) == int(text):
                    return device
            return None
        needle = text.lower()
        for device in devices:
            if needle in str(device.get("name", "")).lower():
                return device
        return None
    for device in devices:
        if _looks_like_ios(str(device.get("name", ""))):
            return device
    candidates = [device for device in devices if not is_screen_device(device)]
    if len(candidates) == 1:
        return candidates[0]
    # Some macOS builds expose the iPhone as an *extra* "Capture screen N"
    # device (the Mac itself is usually "Capture screen 0"). Only accept that
    # when there is exactly one additional screen device, otherwise the user
    # must say which one (--ios "Capture screen 1" / --ios 1).
    screen_devices = [device for device in devices if is_screen_device(device)]
    if not candidates and len(screen_devices) == 2:
        return screen_devices[-1]
    return None


def list_capture_devices(
    ffmpeg: str | None = None, timeout: float = 12.0
) -> list[dict[str, Any]]:
    """List AVFoundation video devices (spawns ffmpeg, ~0.5-1.5s)."""
    executable = resolve_ffmpeg(ffmpeg)
    command = [
        executable,
        "-hide_banner",
        "-f",
        "avfoundation",
        "-list_devices",
        "true",
        "-i",
        "",
    ]
    try:
        proc = subprocess.run(
            command, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise IosError(f"ffmpeg 设备枚举超时（{timeout:.0f}s）") from exc
    except OSError as exc:
        raise IosError(f"无法执行 ffmpeg: {exc}") from exc
    output = (proc.stderr or b"").decode("utf-8", errors="replace")
    output += (proc.stdout or b"").decode("utf-8", errors="replace")
    return parse_ffmpeg_devices(output)


# ------------------------------------------------------------------ status
def _capture_hint(state: str, usbmux_error: str | None, native: dict[str, Any] | None = None) -> str:
    native = native or {}
    if state == "ready":
        return "iPhone 屏幕采集可用（IOSEye 原生通道）；建议先运行 phone ios shot 验证一帧"
    native_error = str(native.get("error") or "")
    if state == "connected":
        generic = (
            "iPhone 已连接但屏幕采集没有就绪：解锁手机并确认「信任此电脑」；"
            "首次使用请允许「IOSEye」访问相机；仍不行时打开 QuickTime → 新建影片录制 → "
            "选一次 iPhone 唤醒系统采集服务，然后重试"
        )
        if native_error:
            detail = str(native.get("hint") or _status_hint(native))
            if detail and detail != native_error:
                return f"{generic}\n原因：{detail}"
        return generic
    if state == "screen-only":
        return (
            "只发现 Mac 的屏幕采集设备，没有 iPhone：用数据线直连 Mac（勿用仅充电线），"
            "解锁并信任此电脑后重试"
        )
    if usbmux_error:
        return (
            f"{usbmux_error}。如果这是 macOS 而 usbmuxd 正常，可安装可选回退："
            "pymobiledevice3 或 idevicescreenshot（见 docs/ios.md）"
        )
    return (
        "没有检测到 iPhone：确认数据线支持数据传输、直连 Mac 而非充电器，"
        "解锁手机并在弹窗点「信任此电脑」，然后运行 phone ios wait"
    )


def ios_status(
    serial: str | None = None,
    *,
    ffmpeg: str | None = None,
    probe_capture: bool = False,
    probe_native: bool = True,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """Diagnose the iOS eye: native capture helper + device presence."""
    tools = tool_availability(ffmpeg)
    usbmux_error: str | None = None
    devices: list[dict[str, Any]] = []
    try:
        devices = usbmux_list(timeout=min(3.0, timeout))
    except IosError as exc:
        usbmux_error = str(exc)
    attached = list(devices)
    if serial:
        selected = next((item for item in devices if item["serial"] == serial), None)
        devices = [selected] if selected is not None else []
    if devices:
        # Optional enrichment: device name, product type and iOS version.
        try:
            enriched = {
                item["serial"]: item for item in pymobiledevice3_devices() if item.get("serial")
            }
        except Exception:  # noqa: BLE001 - optional tool, never fail status
            enriched = {}
        for device in devices:
            extra = enriched.get(device.get("serial"))
            if extra:
                device.update({key: value for key, value in extra.items() if value})

    native: dict[str, Any] | None = None
    if probe_native:
        native = ios_eye_probe(timeout=max(10.0, min(float(timeout) * 2, 30.0)))

    capture_devices: list[dict[str, Any]] = []
    capture_error: str | None = None
    capture_device: dict[str, Any] | None = None
    if probe_capture and tools["ffmpeg"]:
        try:
            capture_devices = list_capture_devices(tools["ffmpeg"], timeout=timeout)
            capture_device = pick_capture_device(capture_devices)
        except IosError as exc:
            capture_error = str(exc)

    if native is not None and native.get("ok"):
        state = "ready"
    elif devices:
        state = "connected"
    elif capture_devices:
        state = "screen-only"
    else:
        state = "missing"
    status: dict[str, Any] = {
        "ok": state == "ready",
        "platform": PLATFORM,
        "state": state,
        "serial": serial or (devices[0]["serial"] if devices else None),
        "devices": devices,
        "native": native,
        "capture_devices": capture_devices,
        "capture_device": capture_device,
        "capture_backend": "native" if state == "ready" else None,
        "tools": tools,
        "usbmux_error": usbmux_error,
        "capture_error": capture_error,
        "hint": _capture_hint(state, usbmux_error, native),
    }
    if serial and not devices and attached:
        status["hint"] = (
            f"没有找到 UDID={serial} 的 iPhone；当前连接: "
            + ", ".join(str(item.get("serial")) for item in attached)
        )
    return status


def wait_for_ios(
    *,
    timeout: float = 60.0,
    interval: float = 1.0,
    serial: str | None = None,
    ffmpeg: str | None = None,
    probe_capture: bool = False,
    probe_native: bool = True,
    on_wait: Any = None,
) -> dict[str, Any]:
    """Block until the iPhone capture device is usable (or timeout)."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    last: dict[str, Any] = {}
    while True:
        # Cheap poll first (usbmuxd only); only launch the capture helper once
        # a phone is physically present.
        status = ios_status(
            serial=serial,
            ffmpeg=ffmpeg,
            probe_capture=False,
            probe_native=False,
            timeout=6.0,
        )
        if status.get("devices"):
            status = ios_status(
                serial=serial,
                ffmpeg=ffmpeg,
                probe_capture=probe_capture,
                probe_native=probe_native,
                timeout=12.0,
            )
        last = status
        if status["ok"]:
            return status
        if on_wait is not None:
            on_wait(status)
        if time.monotonic() >= deadline:
            last["timed_out"] = True
            return last
        time.sleep(max(0.05, interval))


# -------------------------------------------------------------- one-shot
def _png_bytes(frame: Frame) -> bytes:
    buffer = io.BytesIO()
    frame.to_pil().save(buffer, format="PNG")
    return buffer.getvalue()


def _decode_image(data: bytes, source: str) -> Frame:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return Frame.from_pil(image, source=source)


def pymobiledevice3_executable() -> str | None:
    return shutil.which("pymobiledevice3")


def _pymobiledevice3_command(udid: str | None, out_path: str) -> list[str]:
    # iOS 17+: userspace RSD tunnel is automatic and root is not required;
    # Developer Mode must be enabled on the device. ``out`` is positional.
    executable = pymobiledevice3_executable() or "pymobiledevice3"
    command = [executable, "--no-color", "developer", "dvt", "screenshot", out_path]
    if udid:
        command += ["--udid", udid]
    return command


def pymobiledevice3_devices(timeout: float = 8.0) -> list[dict[str, Any]]:
    """Device list with names/iOS versions via ``pymobiledevice3 usbmux list``.

    Optional (``pip install pymobiledevice3``): unlike the raw usbmuxd socket
    reply it opens lockdownd, so a *trusted/paired* device is required for the
    values, and JSON output is the only CLI format.
    """
    executable = pymobiledevice3_executable()
    if not executable:
        return []
    try:
        proc = subprocess.run(
            [executable, "--no-color", "usbmux", "list"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    devices: list[dict[str, Any]] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        devices.append(
            {
                "serial": item.get("UniqueDeviceID") or item.get("Identifier"),
                "name": item.get("DeviceName"),
                "product_type": item.get("ProductType"),
                "ios_version": item.get("ProductVersion"),
                "connection_type": item.get("ConnectionType"),
            }
        )
    return devices


def capture_pymobiledevice3(
    udid: str | None = None, *, timeout: float = 30.0
) -> bytes:
    """One-shot screenshot through ``pymobiledevice3`` (Developer Mode)."""
    executable = shutil.which("pymobiledevice3")
    if not executable:
        raise IosError(
            "pymobiledevice3 未安装（可选回退）：.venv/bin/pip install pymobiledevice3"
        )
    with tempfile.TemporaryDirectory(prefix="phone-ios-") as tmp:
        out_path = str(Path(tmp) / "screen.png")
        try:
            proc = subprocess.run(
                _pymobiledevice3_command(udid, out_path),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise IosError(f"pymobiledevice3 截图超时（{timeout:.0f}s）") from exc
        output = (
            (proc.stderr or b"").decode("utf-8", errors="replace")
            + (proc.stdout or b"").decode("utf-8", errors="replace")
        ).strip()
        path = Path(out_path)
        if proc.returncode != 0 or not path.is_file():
            raise IosError(
                f"pymobiledevice3 截图失败：{output[-400:] or proc.returncode}"
                "（iPhone 需开启开发者模式：设置 → 隐私与安全性 → 开发者模式；"
                "可先运行 pymobiledevice3 amfi reveal-developer-mode）"
            )
        return path.read_bytes()


def capture_idevicescreenshot(
    udid: str | None = None, *, timeout: float = 20.0
) -> bytes:
    """One-shot screenshot through libimobiledevice's ``idevicescreenshot``."""
    executable = shutil.which("idevicescreenshot")
    if not executable:
        raise IosError(
            "idevicescreenshot 未安装（可选回退）：brew install libimobiledevice"
        )
    with tempfile.TemporaryDirectory(prefix="phone-ios-") as tmp:
        out_path = str(Path(tmp) / "screen.png")
        command = [executable]
        if udid:
            command += ["-u", udid]
        command.append(out_path)
        try:
            proc = subprocess.run(
                command, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise IosError(f"idevicescreenshot 超时（{timeout:.0f}s）") from exc
        output = (
            (proc.stderr or b"").decode("utf-8", errors="replace")
            + (proc.stdout or b"").decode("utf-8", errors="replace")
        ).strip()
        path = Path(out_path)
        if proc.returncode != 0 or not path.is_file():
            raise IosError(
                f"idevicescreenshot 失败：{output[-400:] or proc.returncode}"
                "（通常需要已挂载 developer disk image）"
            )
        return path.read_bytes()


# --------------------------------------------------------- native eye (IOSEye)
# The iPhone screen is exposed through CoreMediaIO's iOSScreenCapture plug-in
# (the channel QuickTime uses). ffmpeg's avfoundation input does not enumerate
# that muxed device, so we ship a small signed helper app that talks to
# AVFoundation directly and streams JPEG frames over a unix socket.
REPO_ROOT = Path(__file__).resolve().parents[3]
IOS_EYE_APP = REPO_ROOT / ".tools" / "ios-eye" / "IOSEye.app"
IOS_EYE_SOURCE = REPO_ROOT / "tools" / "ios_eye" / "IOSEye.m"
IOS_EYE_INFO = REPO_ROOT / "tools" / "ios_eye" / "Info.plist"
IOS_EYE_SCRATCH = (
    Path("/tmp/phone-ios-eye")
    if os.uname().sysname == "Darwin"
    else Path(tempfile.gettempdir()) / "phone-ios-eye"
)
NATIVE_BUNDLE_ID = "local.phoneconsole.ioseye"

_CLANG_FRAMEWORKS = (
    "-framework", "Foundation",
    "-framework", "AVFoundation",
    "-framework", "AppKit",
    "-framework", "CoreMedia",
    "-framework", "CoreVideo",
    "-framework", "CoreMediaIO",
    "-framework", "ImageIO",
    "-framework", "CoreImage",
)


def ios_eye_app() -> Path:
    return IOS_EYE_APP


def ios_eye_built() -> bool:
    binary = IOS_EYE_APP / "Contents" / "MacOS" / "IOSEye"
    return binary.is_file() and os.access(binary, os.X_OK)


def resolve_clang() -> str | None:
    found = shutil.which("clang")
    if found:
        return found
    for candidate in ("/usr/bin/clang",):
        if Path(candidate).is_file():
            return candidate
    return None


def build_ios_eye(force: bool = False) -> dict[str, Any]:
    """Compile + ad-hoc sign the native helper app.

    Rebuilding changes the app signature and therefore resets the macOS camera
    permission (one re-approval is needed) — the Python side only builds when
    the binary is missing, unless ``force`` is set.
    """
    binary = IOS_EYE_APP / "Contents" / "MacOS" / "IOSEye"
    if not force and ios_eye_built():
        return {"ok": True, "built": True, "app": str(IOS_EYE_APP), "rebuilt": False}
    clang = resolve_clang()
    if clang is None:
        return {
            "ok": False,
            "built": False,
            "app": str(IOS_EYE_APP),
            "error": "clang not found (install Xcode Command Line Tools: xcode-select --install)",
        }
    if not IOS_EYE_SOURCE.is_file():
        return {
            "ok": False,
            "built": False,
            "app": str(IOS_EYE_APP),
            "error": f"helper source missing: {IOS_EYE_SOURCE}",
        }
    try:
        (IOS_EYE_APP / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
        app_info = IOS_EYE_APP / "Contents" / "Info.plist"
        app_info.write_bytes(IOS_EYE_INFO.read_bytes())
        proc = subprocess.run(
            [clang, "-fobjc-arc", "-O2", *_CLANG_FRAMEWORKS, str(IOS_EYE_SOURCE), "-o", str(binary)],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            return {"ok": False, "built": False, "app": str(IOS_EYE_APP), "error": detail[-800:]}
        subprocess.run(
            ["codesign", "--force", "--sign", "-", str(IOS_EYE_APP)],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "built": False, "app": str(IOS_EYE_APP), "error": str(exc)}
    return {"ok": True, "built": True, "app": str(IOS_EYE_APP), "rebuilt": True}


def _ensure_scratch() -> Path:
    IOS_EYE_SCRATCH.mkdir(parents=True, exist_ok=True)
    return IOS_EYE_SCRATCH


def _helper_binary() -> Path:
    return IOS_EYE_APP / "Contents" / "MacOS" / "IOSEye"


def helper_pids() -> list[int]:
    """PIDs of running IOSEye helper instances (exact binary path match)."""
    binary = str(_helper_binary())
    try:
        proc = subprocess.run(
            ["pgrep", "-f", f"^{re.escape(binary)}$"],
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(part) for part in (proc.stdout or b"").split() if part.isdigit()]


def kill_stale_helpers(exclude: int | None = None) -> int:
    """Terminate leftover helpers; a stale one would hold the camera.

    Returns the number of processes signalled.
    """
    killed = 0
    for pid in helper_pids():
        if exclude is not None and pid == exclude:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except OSError:
            continue
    if killed:
        time.sleep(0.2)
    return killed


class _launch_lock:
    """Serialize helper launches (the helper consumes one shared config file)."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = max(0.1, float(timeout))
        self._fd: int | None = None

    def __enter__(self) -> "_launch_lock":
        lock_path = _ensure_scratch() / "launch.lock"
        self._fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise IosError("iOS 取帧 helper 启动繁忙（另一个进程正在启动），稍后重试")
                time.sleep(0.05)

    def __exit__(self, *exc: Any) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None


def _launch_app(config: dict[str, Any], *, app: Path | None = None) -> None:
    """Write the launch config and start the helper through LaunchServices."""
    target = app or IOS_EYE_APP
    if not (target / "Contents" / "MacOS" / "IOSEye").is_file():
        raise IosError(
            "iOS 原生取帧 helper 未编译：先运行 `make ios-eye-build`（需要 Xcode Command Line Tools）"
        )
    _ensure_scratch()
    kill_stale_helpers()
    config_path = IOS_EYE_SCRATCH / "config.json"
    tmp_path = IOS_EYE_SCRATCH / f"config-{os.getpid()}-{uuid.uuid4().hex}.json"
    tmp_path.write_text(json.dumps(config), encoding="utf-8")
    os.replace(tmp_path, config_path)
    try:
        log_path = IOS_EYE_SCRATCH / "helper.log"
        proc = subprocess.run(
            ["open", "-n", str(target), "--stderr", str(log_path), "--stdout", str(log_path)],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IosError(f"无法启动 iOS 取帧 helper: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise IosError(f"无法启动 iOS 取帧 helper: {detail or proc.returncode}")


def _read_status(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _status_hint(status: dict[str, Any]) -> str:
    error = str(status.get("error") or "")
    camera_auth = status.get("camera_auth")
    if "camera permission" in error or camera_auth in (0, 2):
        return (
            "IOSEye 需要相机权限：首次运行会弹出「“IOSEye”想要访问相机」，点允许；"
            "若没弹窗/被拒绝，到 系统设置 → 隐私与安全性 → 相机 打开 IOSEye，然后重试"
        )
    if "iPhone screen not found" in error or "not found" in error:
        return (
            "没有找到 iPhone 屏幕采集设备：确认 iPhone 用数据线直连 Mac、已解锁并信任此电脑；"
            "若仍不行，打开 QuickTime → 新建影片录制 → 选一次 iPhone 以唤醒系统采集服务"
        )
    return error or "iOS 取帧 helper 启动失败"


def _terminate_status_pid(status: dict[str, Any] | None) -> None:
    if not status:
        return
    pid = status.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def _wait_for_status(
    path: Path,
    *,
    timeout: float,
    ok: bool = True,
    deadline_check: Any = None,
) -> dict[str, Any]:
    """Poll a helper status file until it reports ok (or a terminal error)."""
    started = time.monotonic()
    last: dict[str, Any] = {"ok": False, "error": "no status written by IOSEye"}
    while time.monotonic() - started < max(0.5, timeout):
        current = _read_status(path)
        if current is not None:
            last = current
            if current.get("ok") is ok:
                return current
            error = str(current.get("error") or "")
            if error and current.get("mode") != "stream":
                return current
            if error and "camera permission" in error:
                return current
        if deadline_check is not None and deadline_check():
            break
        time.sleep(0.15)
    return last


def ios_eye_probe(timeout: float = 25.0) -> dict[str, Any]:
    """Launch the helper in probe mode and report iPhone/camera readiness."""
    if not ios_eye_built():
        build = build_ios_eye()
        if not build.get("ok"):
            return {"ok": False, "built": False, "error": build.get("error"), "app": str(IOS_EYE_APP)}
    status_path = _ensure_scratch() / f"probe-{uuid.uuid4().hex}.json"
    config = {
        "mode": "probe",
        "status_file": str(status_path),
        "wait_device": 8.0,
        "wait_permission": max(5.0, float(timeout)),
    }
    pid: int | None = None
    try:
        with _launch_lock():
            status_path.unlink(missing_ok=True)
            _launch_app(config)
            status = _wait_for_status(status_path, timeout=timeout)
            pid = status.get("pid") if isinstance(status.get("pid"), int) else None
    except IosError as exc:
        return {"ok": False, "built": True, "error": str(exc), "hint": str(exc), "app": str(IOS_EYE_APP)}
    finally:
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        status_path.unlink(missing_ok=True)
    status.setdefault("built", True)
    if not status.get("ok"):
        status["hint"] = _status_hint(status)
    return status


def capture_native_shot(timeout: float = 30.0) -> tuple[Frame, dict[str, Any]]:
    """One-shot screenshot through the native helper."""
    if not ios_eye_built():
        build = build_ios_eye()
        if not build.get("ok"):
            raise IosError(str(build.get("error") or "IOSEye helper is not built"))
    token = uuid.uuid4().hex
    status_path = _ensure_scratch() / f"shot-{token}.json"
    out_path = _ensure_scratch() / f"shot-{token}.png"
    config = {
        "mode": "shot",
        "status_file": str(status_path),
        "out": str(out_path),
        "wait_device": 8.0,
        "wait_permission": min(max(10.0, timeout), 120.0),
    }
    try:
        with _launch_lock():
            status_path.unlink(missing_ok=True)
            out_path.unlink(missing_ok=True)
            _launch_app(config)
            deadline = time.monotonic() + max(5.0, float(timeout))
            status: dict[str, Any] = {"ok": False, "error": "timeout"}
            while time.monotonic() < deadline:
                current = _read_status(status_path)
                if current is not None:
                    status = current
                    if current.get("ok") and out_path.is_file() and out_path.stat().st_size > 0:
                        break
                    error = str(current.get("error") or "")
                    if error and "camera permission" in error:
                        break
                time.sleep(0.1)
            if not (status.get("ok") and out_path.is_file()):
                kill_stale_helpers()
                raise IosError(_status_hint(status))
            data = out_path.read_bytes()
    finally:
        status = _read_status(status_path)
        _terminate_status_pid(status)
        status_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
    frame = _decode_image(data, "ios:native:IOSEye")
    return frame, status


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise IosError("iOS 取帧 helper 断开连接")
        data += chunk
    return data


class IosNativeSource:
    """Live iPhone frames from the IOSEye helper (JPEG over a unix socket)."""

    def __init__(
        self,
        device: str | None = None,
        *,
        max_width: int = 0,
        max_fps: int = 30,
        quality: float = 0.7,
        startup_timeout: float = 30.0,
        synthetic: bool | None = None,
    ) -> None:
        if synthetic is None:
            synthetic = os.environ.get("PHONE_IOS_EYE_SYNTHETIC", "").lower() in {"1", "true", "yes"}
        self.synthetic = bool(synthetic)
        self._requested_device = device
        self.max_width = int(max_width or 0)
        self.max_fps = int(max_fps or 0)
        self.quality = float(quality)
        self.startup_timeout = max(5.0, float(startup_timeout))
        self._status_path: Path | None = None
        self._socket_path: Path | None = None
        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._cond = threading.Condition()
        self._frame: Frame | None = None
        self._frames = 0
        self._bytes = 0
        self._started_at = 0.0
        self._last_frame_at = 0.0
        self._error: str | None = None
        self._closing = False
        self._status: dict[str, Any] = {}
        self._native_size: tuple[int, int] | None = None

    # ------------------------------------------------------------------ setup
    def _ensure_started(self) -> None:
        with self._cond:
            if self._reader is not None and self._reader.is_alive():
                return
            if self._closing:
                raise IosError(f"{self.name}: source is closed")
            self._error = None
            self._frames = 0
            self._bytes = 0
            self._frame = None
        if not ios_eye_built():
            build = build_ios_eye()
            if not build.get("ok"):
                raise IosError(str(build.get("error") or "IOSEye helper is not built"))
        token = uuid.uuid4().hex
        self._status_path = _ensure_scratch() / f"stream-{token}.json"
        self._socket_path = _ensure_scratch() / f"stream-{token}.sock"
        config = {
            "mode": "stream",
            "status_file": str(self._status_path),
            "socket": str(self._socket_path),
            "max_width": self.max_width,
            "max_fps": self.max_fps,
            "quality": self.quality,
            "wait_device": 8.0,
            "wait_permission": self.startup_timeout,
        }
        if self.synthetic:
            config["synthetic"] = True
        try:
            with _launch_lock():
                self._status_path.unlink(missing_ok=True)
                self._socket_path.unlink(missing_ok=True)
                _launch_app(config)
                status = _wait_for_status(self._status_path, timeout=self.startup_timeout)
                self._status = status
                if not status.get("ok"):
                    raise IosError(_status_hint(status))
        except IosError:
            kill_stale_helpers()
            self._status_path.unlink(missing_ok=True)
            self._socket_path.unlink(missing_ok=True)
            raise
        size = status.get("width"), status.get("height")
        if isinstance(size[0], int) and isinstance(size[1], int) and size[0] > 0:
            self._native_size = (int(size[0]), int(size[1]))
        self._connect()
        self._started_at = time.time()
        self._reader = threading.Thread(target=self._run, name="ios-native-reader", daemon=True)
        self._reader.start()
        deadline = time.monotonic() + min(self.startup_timeout, 12.0)
        with self._cond:
            while self._frame is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise IosError(f"{self.name}: 没有收到画面")
                self._cond.wait(min(remaining, 0.2))

    def _connect(self) -> None:
        assert self._socket_path is not None
        deadline = time.monotonic() + 10.0
        last_error: str | None = None
        while time.monotonic() < deadline:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            try:
                sock.connect(str(self._socket_path))
            except OSError as exc:
                last_error = str(exc)
                sock.close()
                time.sleep(0.1)
                continue
            self._sock = sock
            return
        raise IosError(f"{self.name}: 无法连接取帧 socket ({last_error})")

    # ----------------------------------------------------------------- frames
    def _run(self) -> None:
        sock = self._sock
        assert sock is not None
        try:
            while not self._closing:
                header = _recv_exact(sock, 4)
                length = struct.unpack(">I", header)[0]
                if length <= 0 or length > 32 * 1024 * 1024:
                    raise IosError(f"{self.name}: invalid frame length {length}")
                data = _recv_exact(sock, length)
                try:
                    frame = self._decode_jpeg(data)
                except Exception:  # noqa: BLE001 - skip corrupt frames
                    continue
                with self._cond:
                    self._frame = frame
                    self._frames += 1
                    self._bytes += length
                    self._last_frame_at = time.time()
                    if self._native_size is None:
                        if self.max_width == 0:
                            self._native_size = frame.size
                        else:
                            current = _read_status(self._status_path) if self._status_path else None
                            if current:
                                width, height = current.get("width"), current.get("height")
                                if isinstance(width, int) and isinstance(height, int) and width > 0:
                                    self._native_size = (width, height)
                    self._cond.notify_all()
        except (OSError, IosError) as exc:
            with self._cond:
                self._error = str(exc)
                self._cond.notify_all()

    @staticmethod
    def _decode_jpeg(jpeg: bytes) -> Frame:
        try:
            import cv2
            import numpy as np

            image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                return Frame(image, source="ios")
        except ImportError:  # pragma: no cover - cv2 is a vision dependency
            pass
        from PIL import Image

        with Image.open(io.BytesIO(jpeg)) as image:
            return Frame.from_pil(image, source="ios")

    # ------------------------------------------------------------------- api
    @property
    def name(self) -> str:
        return "ios:native:IOSEye"

    @property
    def frame_size(self) -> tuple[int, int] | None:
        with self._cond:
            return self._frame.size if self._frame is not None else None

    @property
    def native_size(self) -> tuple[int, int] | None:
        return self._native_size

    def grab(self, timeout: float | None = None) -> Frame:
        self._ensure_started()
        deadline = time.monotonic() + (
            self.startup_timeout if timeout is None else float(timeout)
        )
        with self._cond:
            while self._frame is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise IosError(f"{self.name}: 没有收到画面")
                self._cond.wait(min(remaining, 0.2))
            return self._frame

    def latest(self) -> Frame | None:
        with self._cond:
            return self._frame

    def wait_newer(self, after: float, timeout: float = 2.0) -> Frame:
        self._ensure_started()
        deadline = time.monotonic() + float(timeout)
        with self._cond:
            while self._frame is None or self._frame.ts <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = f" ({self._error})" if self._error else ""
                    raise IosError(f"{self.name}: {timeout:.2f}s 内没有新帧{detail}")
                self._cond.wait(min(remaining, 0.2))
            return self._frame

    def stats(self) -> dict[str, Any]:
        with self._cond:
            frames = self._frames
            size = self._frame.size if self._frame is not None else None
            last = self._last_frame_at
            error = self._error or self._status.get("error")
        elapsed = max(1e-6, time.time() - self._started_at) if self._started_at else 0.0
        return {
            "name": self.name,
            "backend": "native",
            "helper": "IOSEye",
            "device": self._status.get("device"),
            "native_size": list(self._native_size) if self._native_size else None,
            "frames": frames,
            "fps": round(frames / elapsed, 2) if elapsed and frames else 0.0,
            "size": list(size) if size else None,
            "age_ms": round((time.time() - last) * 1000.0, 1) if last else None,
            "running": bool(self._reader is not None and self._reader.is_alive()),
            "error": error,
        }

    def close(self) -> None:
        with self._cond:
            if self._closing:
                return
            self._closing = True
            sock = self._sock
            self._sock = None
            reader = self._reader
            self._cond.notify_all()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        status = _read_status(self._status_path) if self._status_path else None
        _terminate_status_pid(status or self._status)
        if reader is not None and reader.is_alive():
            reader.join(timeout=2.0)
        if self._status_path is not None:
            self._status_path.unlink(missing_ok=True)
        if self._socket_path is not None:
            self._socket_path.unlink(missing_ok=True)

    def __enter__(self) -> "IosNativeSource":
        self._ensure_started()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ------------------------------------------------------------- live source
class IosMjpegSource:
    """Live iPhone screen frames through ffmpeg's AVFoundation input.

    ffmpeg runs as a child process and writes a concatenated MJPEG stream to
    stdout; a reader thread decodes every JPEG and keeps **the latest** frame,
    matching the interface of :class:`phone_console.vision.stream.H264StreamSource`
    (``grab`` / ``latest`` / ``wait_newer`` / ``stats`` / ``close``).
    """

    def __init__(
        self,
        device: str | int | dict[str, Any] | None = None,
        *,
        ffmpeg: str | None = None,
        fps: int = 0,
        startup_timeout: float = 12.0,
        jpeg_quality: int = 3,
        max_width: int | None = None,
        restart_delay: float = 1.0,
    ) -> None:
        self.ffmpeg = resolve_ffmpeg(ffmpeg)
        self._wanted = device
        self._device: dict[str, Any] | None = None
        self._fps = int(fps or 0)
        self.startup_timeout = max(1.0, float(startup_timeout))
        self.jpeg_quality = min(31, max(2, int(jpeg_quality)))
        self.max_width = int(max_width or 0) or None
        self.restart_delay = max(0.2, float(restart_delay))

        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._cond = threading.Condition()
        self._frame: Frame | None = None
        self._closing = False
        self._fatal = False
        self._error: str | None = None
        self._stderr_lines: deque[str] = deque(maxlen=40)
        self._frames = 0
        self._restarts = 0
        self._started_at = 0.0
        self._last_frame_at = 0.0
        self._command: list[str] | None = None

    # ------------------------------------------------------------------ setup
    def _resolve_device(self) -> dict[str, Any]:
        if self._device is not None:
            return self._device
        if isinstance(self._wanted, dict):
            self._device = self._wanted
            return self._device
        devices = list_capture_devices(self.ffmpeg)
        device = pick_capture_device(devices, self._wanted)
        if device is None:
            raise IosError(
                f"{_capture_hint('missing', None)}\n可见设备: "
                + ", ".join(f"[{item['index']}] {item['name']}" for item in devices)
            )
        self._device = device
        return device

    def command(self) -> list[str]:
        device = self._resolve_device()
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "avfoundation",
        ]
        if self._fps > 0:
            command += ["-framerate", str(self._fps)]
        command += ["-i", f"{device['index']}:none", "-an"]
        if self.max_width:
            command += ["-vf", f"scale={self.max_width}:-2"]
        command += [
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            str(self.jpeg_quality),
            "-",
        ]
        return command

    @property
    def name(self) -> str:
        device = self._device or self._wanted
        if isinstance(device, dict):
            return f"ios:avfoundation:{device.get('name')}"
        return f"ios:avfoundation:{device}" if device else "ios:avfoundation"

    @property
    def device(self) -> dict[str, Any] | None:
        return self._device

    @property
    def frame_size(self) -> tuple[int, int] | None:
        with self._cond:
            return self._frame.size if self._frame is not None else None

    # ------------------------------------------------------------- lifecycle
    def _ensure_started(self) -> None:
        with self._cond:
            if self._reader is not None and self._reader.is_alive():
                return
            if self._closing:
                raise IosError(f"{self.name}: source is closed")
            restart = self._reader is not None
            self._fatal = False
            self._error = None
            self._stderr_lines.clear()
            if restart:
                # ffmpeg exited (unplugged phone, QuickTime grabbed the device,
                # ...): start over and wait for a fresh frame instead of
                # silently returning the stale one.
                self._restarts += 1
                self._frame = None
        self._spawn()
        with self._cond:
            deadline = time.monotonic() + self.startup_timeout
            while self._frame is None:
                if self._fatal:
                    raise self._startup_error()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._startup_error()
                self._cond.wait(min(remaining, 0.2))

    def _spawn(self) -> None:
        command = self.command()
        try:
            proc = subprocess.Popen(  # noqa: S603 - explicit command list
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            raise IosError(f"无法启动 ffmpeg: {exc}") from exc
        with self._cond:
            self._command = command
            self._proc = proc
            self._started_at = time.time()
            self._reader = threading.Thread(
                target=self._run, name="ios-mjpeg-reader", daemon=True
            )
            self._stderr_reader = threading.Thread(
                target=self._drain_stderr, name="ios-mjpeg-stderr", daemon=True
            )
        self._reader.start()
        self._stderr_reader.start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                with self._cond:
                    self._stderr_lines.append(line)
        except (OSError, ValueError):
            pass

    def _stderr_summary(self, lines: int = 3) -> str:
        with self._cond:
            recent = list(self._stderr_lines)[-lines:]
        return " | ".join(item for item in recent if item)

    def _startup_error(self) -> IosError:
        detail = self._error or "没有收到画面"
        tail = self._stderr_summary()
        hint = ""
        lowered = tail.lower()
        if "input/output error" in lowered or "not found" in lowered:
            hint = (
                "\n提示：iPhone 可能已锁屏/未点「信任此电脑」，或 QuickTime 等 App 占用了屏幕采集；"
                "解锁后运行 phone ios wait"
            )
        elif "framerate" in lowered:
            hint = "\n提示：该设备不支持这个帧率，试试 --fps 0 或 --fps 60"
        return IosError(
            f"{self.name}: {detail}" + (f"\nffmpeg: {tail}" if tail else "") + hint
        )

    def _run(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        buffer = bytearray()
        try:
            while not self._closing:
                try:
                    chunk = proc.stdout.read(65536)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > 64 * 1024 * 1024:  # garbage guard
                    buffer.clear()
                    continue
                while True:
                    start = buffer.find(b"\xff\xd8")
                    if start < 0:
                        if len(buffer) > 2:
                            del buffer[:-1]
                        break
                    end = buffer.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start > 0:
                            del buffer[:start]
                        break
                    jpeg = bytes(buffer[start : end + 2])
                    del buffer[: end + 2]
                    try:
                        frame = self._decode_jpeg(jpeg)
                    except Exception:  # noqa: BLE001 - skip corrupt frame
                        continue
                    with self._cond:
                        self._frame = frame
                        self._frames += 1
                        self._last_frame_at = time.time()
                        self._cond.notify_all()
        finally:
            if not self._closing:
                with self._cond:
                    if self._frame is None and self._error is None:
                        self._error = "ffmpeg 退出且没有产生画面"
                    self._fatal = True
                    self._cond.notify_all()
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _decode_jpeg(jpeg: bytes) -> Frame:
        try:
            import cv2
            import numpy as np

            image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                return Frame(image, source="ios")
        except ImportError:  # pragma: no cover - cv2 is a vision dependency
            pass
        from PIL import Image

        with Image.open(io.BytesIO(jpeg)) as image:
            return Frame.from_pil(image, source="ios")

    # ------------------------------------------------------------------- api
    def grab(self, timeout: float | None = None) -> Frame:
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
                self._cond.wait(min(remaining, 0.2))
            return self._frame

    def latest(self) -> Frame | None:
        with self._cond:
            return self._frame

    def wait_newer(self, after: float, timeout: float = 2.0) -> Frame:
        self._ensure_started()
        deadline = time.monotonic() + float(timeout)
        with self._cond:
            while self._frame is None or self._frame.ts <= after:
                if self._fatal:
                    raise self._startup_error()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise IosError(
                        f"{self.name}: {timeout:.2f}s 内没有新帧"
                    )
                self._cond.wait(min(remaining, 0.2))
            return self._frame

    def stats(self) -> dict[str, Any]:
        with self._cond:
            frames = self._frames
            started = self._started_at
            last = self._last_frame_at
            frame = self._frame
            proc = self._proc
            error = self._error
        elapsed = max(1e-6, time.time() - started) if started else 0.0
        return {
            "name": self.name,
            "backend": "avfoundation",
            "device": self._device,
            "command": self._command,
            "frames": frames,
            "restarts": int(self._restarts),
            "fps": round(frames / elapsed, 2) if started and frames else 0.0,
            "age_ms": round((time.time() - last) * 1000.0, 1) if last else None,
            "size": list(frame.size) if frame is not None else None,
            "running": bool(proc is not None and proc.poll() is None),
            "error": error,
            "stderr": self._stderr_summary(),
        }

    def close(self) -> None:
        with self._cond:
            if self._closing:
                return
            self._closing = True
            proc = self._proc
            reader = self._reader
            stderr_reader = self._stderr_reader
            self._cond.notify_all()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for thread in (reader, stderr_reader):
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2.0)

    def __enter__(self) -> "IosMjpegSource":
        self._ensure_started()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class IosStillSource:
    """One-shot iPhone screenshots, trying the available backends in order.

    Order: the native IOSEye helper (no developer setup, recommended), then
    AVFoundation/ffmpeg (older macOS builds that expose the muxed device),
    then ``pymobiledevice3`` (Developer Mode), then ``idevicescreenshot``
    (libimobiledevice). Every failure is collected so the final error explains
    what to fix.
    """

    def __init__(
        self,
        device: str | int | None = None,
        *,
        ffmpeg: str | None = None,
        udid: str | None = None,
        timeout: float = 25.0,
        backends: Sequence[str] | None = None,
    ) -> None:
        self.device = device
        self.ffmpeg = ffmpeg
        self.udid = udid
        self.timeout = float(timeout)
        self.backends = list(backends) if backends else None
        self._size: tuple[int, int] | None = None
        self._last_backend: str | None = None
        self._resolved_device: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return f"ios:still:{self._last_backend or 'auto'}"

    @property
    def frame_size(self) -> tuple[int, int] | None:
        return self._size

    def _available_backends(self) -> list[str]:
        if self.backends is not None:
            return list(self.backends)
        tools = tool_availability(self.ffmpeg)
        backends: list[str] = []
        native = tools.get("native_eye") or {}
        if native.get("built") or native.get("clang"):
            backends.append("native")
        if tools["ffmpeg"]:
            backends.append("avfoundation")
        if tools["pymobiledevice3"]:
            backends.append("pymobiledevice3")
        if tools["idevicescreenshot"]:
            backends.append("idevicescreenshot")
        return backends

    def grab(self, timeout: float | None = None) -> Frame:
        errors: list[str] = []
        for backend in self._available_backends():
            try:
                frame = self._grab_backend(backend, timeout)
            except IosError as exc:
                errors.append(f"{backend}: {exc}")
                continue
            self._last_backend = backend
            self._size = frame.size
            return Frame(frame.image, ts=frame.ts, source=f"ios:still:{backend}")
        detail = "\n".join(errors) if errors else "没有可用的 iOS 截图后端"
        hint = "先运行 phone ios status / phone ios shot 查看具体原因；见 docs/ios.md"
        raise IosError(f"iOS 截图失败：\n{detail}\n提示：{hint}")

    def _grab_backend(self, backend: str, timeout: float | None) -> Frame:
        limit = self.timeout if timeout is None else float(timeout)
        if backend == "native":
            frame, _status = capture_native_shot(timeout=limit)
            return frame
        if backend == "avfoundation":
            if self._resolved_device is None:
                devices = list_capture_devices(self.ffmpeg)
                picked = pick_capture_device(devices, self.device)
                if picked is None:
                    raise IosError(
                        "没有找到 iPhone 采集设备；可见设备: "
                        + ", ".join(f"[{item['index']}] {item['name']}" for item in devices)
                    )
                self._resolved_device = picked
            source = IosMjpegSource(
                self._resolved_device,
                ffmpeg=self.ffmpeg,
                startup_timeout=min(limit, 20.0),
            )
            try:
                return source.grab(timeout=limit)
            finally:
                source.close()
        if backend == "pymobiledevice3":
            data = capture_pymobiledevice3(self.udid, timeout=limit)
            return _decode_image(data, "ios:still:pymobiledevice3")
        if backend == "idevicescreenshot":
            data = capture_idevicescreenshot(self.udid, timeout=limit)
            return _decode_image(data, "ios:still:idevicescreenshot")
        raise IosError(f"unknown iOS capture backend: {backend}")

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "backend": self._last_backend,
            "backends": self._available_backends(),
            "size": list(self._size) if self._size else None,
        }

    def close(self) -> None:
        return None


def open_ios_source(
    device: str | int | None = None,
    *,
    prefer: str = "live",
    ffmpeg: str | None = None,
    udid: str | None = None,
    fps: int = 0,
    startup_timeout: float = 30.0,
    timeout: float = 25.0,
    max_width: int = 0,
):
    """Open the best available iOS frame source.

    ``prefer="live"`` returns the native helper :class:`IosNativeSource`
    (started lazily on the first grab). ``prefer="still"`` returns an
    :class:`IosStillSource` that runs one capture per grab. ``"auto"`` picks
    live when the helper exists, otherwise a still backend.
    """
    native_ready = ios_eye_built() or resolve_clang() is not None
    if prefer in ("live", "auto") and native_ready:
        return IosNativeSource(
            str(device) if device not in (None, "") else None,
            max_width=max_width,
            max_fps=fps,
            startup_timeout=startup_timeout,
        )
    if prefer == "live":
        # Last-resort live path for other macOS builds (ffmpeg + muxed device).
        return IosMjpegSource(
            device,
            ffmpeg=ffmpeg,
            fps=fps,
            startup_timeout=max(5.0, min(float(startup_timeout), 20.0)),
        )
    return IosStillSource(device, ffmpeg=ffmpeg, udid=udid, timeout=timeout)


def capture_once(
    device: str | int | None = None,
    *,
    ffmpeg: str | None = None,
    udid: str | None = None,
    timeout: float = 25.0,
    backend: str | None = None,
) -> tuple[Frame, str]:
    """Capture a single frame; returns ``(frame, backend_name)``."""
    if backend == "native":
        source = IosStillSource(device, ffmpeg=ffmpeg, timeout=timeout, backends=["native"])
    elif backend == "avfoundation":
        source = IosStillSource(
            device, ffmpeg=ffmpeg, timeout=timeout, backends=["avfoundation"]
        )
    elif backend == "pymobiledevice3":
        source = IosStillSource(
            device, ffmpeg=ffmpeg, udid=udid, timeout=timeout, backends=["pymobiledevice3"]
        )
    elif backend == "idevicescreenshot":
        source = IosStillSource(
            device,
            ffmpeg=ffmpeg,
            udid=udid,
            timeout=timeout,
            backends=["idevicescreenshot"],
        )
    else:
        source = IosStillSource(device, ffmpeg=ffmpeg, udid=udid, timeout=timeout)
    try:
        frame = source.grab()
    finally:
        source.close()
    return frame, source._last_backend or "auto"


def frame_to_png(frame: Frame) -> bytes:
    """PNG bytes for an already captured frame (used by ``phone ios shot``)."""
    return _png_bytes(frame)
