"""adb discovery and USB-debugging authorization helpers.

The Eye's live path (scrcpy / screenrecord) needs ``adb`` and an *authorized*
device: after a reboot or a fresh RSA key the phone shows the "Allow USB
debugging?" dialog and ``adb devices`` reports ``unauthorized`` until someone
taps 允许. These helpers turn that state into a clear message instead of a
cryptic scrcpy failure.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# Environment variables checked, in order, before falling back to PATH.
ADB_ENV_VARS = ("PHONE_ADB", "ADB")

# The repository ships platform-tools (and a wrapper that points HOME at
# .tools/android-home so adb keys stay inside the checkout).
_TOOLS = Path(__file__).resolve().parents[3] / ".tools"
_REPO_ADB_WRAPPER = _TOOLS / "bin" / "adb"
_REPO_ADB = _TOOLS / "platform-tools" / "adb"

# States reported by ``adb devices``.
ONLINE = "device"
UNAUTHORIZED = "unauthorized"
OFFLINE = "offline"
NO_PERMISSIONS = "no permissions"


class AdbError(RuntimeError):
    """adb is missing or the device is not usable."""


def _candidate(path: str | None) -> str | None:
    if not path:
        return None
    found = shutil.which(path)
    if found:
        return found
    expanded = Path(path).expanduser()
    if expanded.is_file():
        return str(expanded)
    return None


def resolve_adb(adb: str | None = None) -> str:
    """Return a usable adb executable path or raise :class:`AdbError`."""
    if adb:
        found = _candidate(adb)
        if found is None:
            raise AdbError(f"adb not found: {adb}")
        return found
    for variable in ADB_ENV_VARS:
        found = _candidate(os.environ.get(variable))
        if found:
            return found
    found = _candidate("adb")
    if found:
        return found
    for candidate in (_REPO_ADB_WRAPPER, _REPO_ADB):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise AdbError(
        "adb not found in PATH; install Android platform-tools, set PHONE_ADB, "
        "or drop platform-tools into .tools/platform-tools"
    )


def adb_devices(adb: str | None = None, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Parse ``adb devices -l`` into a list of device dicts."""
    executable = resolve_adb(adb)
    try:
        proc = subprocess.run(
            [executable, "devices", "-l"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"adb devices timed out after {timeout:.0f}s") from exc
    output = (proc.stdout or b"").decode("utf-8", errors="replace")
    devices: list[dict[str, Any]] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        entry: dict[str, Any] = {"serial": parts[0], "state": parts[1]}
        for token in parts[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                entry[key] = value
        devices.append(entry)
    return devices


def pick_device(
    devices: list[dict[str, Any]], serial: str | None = None
) -> dict[str, Any] | None:
    if serial:
        for device in devices:
            if device["serial"] == serial:
                return device
        return None
    for device in devices:
        if device.get("state") == ONLINE:
            return device
    return devices[0] if devices else None


# Wireless-debugging serials carry a host:port or an mDNS service suffix. Every
# byte of an adb screencap crosses WiFi there, which is several times slower
# than USB, so the eye prefers a USB-attached device when both are visible.
_NETWORK_SERIAL_SUFFIXES = (
    "._adb-tls-connect._tcp",
    "._adb-tls-pairing._tcp",
)


def is_usb_serial(serial: str | None) -> bool:
    text = str(serial or "")
    if not text or ":" in text:  # adb connect host:port
        return False
    return not text.endswith(_NETWORK_SERIAL_SUFFIXES)


def pick_usb_serial(
    devices: list[dict[str, Any]], serial: str | None = None
) -> str | None:
    """Return the online USB serial, honouring an explicitly requested one."""
    if serial:
        return serial
    for device in devices:
        candidate = device.get("serial")
        if device.get("state") != ONLINE or not is_usb_serial(candidate):
            continue
        return candidate
    return None


def preferred_adb_serial(
    adb: str | None = None, serial: str | None = None, timeout: float = 10.0
) -> str | None:
    """The serial the eye should use: the requested one, else a USB device.

    Returns ``None`` when only wireless devices (or nothing) are attached, which
    keeps the previous "let adb pick its default" behaviour.
    """
    if serial:
        return serial
    try:
        devices = adb_devices(adb, timeout=timeout)
    except AdbError:
        return None
    return pick_usb_serial(devices)


def authorization_hint(state: str | None) -> str:
    if state in (None, ""):
        return (
            "没有检测到设备：检查 USB 数据线，手机上确认「USB 调试」已开启，"
            "然后重新插拔或执行 adb kill-server"
        )
    if state == UNAUTHORIZED:
        return (
            "手机未授权此电脑：在手机上点「允许」/「始终允许」USB 调试"
            "（Authorization dialog: tap Allow / Always allow）"
        )
    if state == OFFLINE:
        return "设备处于 offline：重新插拔 USB，或执行 adb kill-server 后重试"
    if state == NO_PERMISSIONS:
        return "USB 权限不足：macOS 上换用原装线/直连口，并检查系统隐私授权"
    return "设备已授权，可继续"


def adb_status(
    adb: str | None = None, serial: str | None = None, timeout: float = 10.0
) -> dict[str, Any]:
    executable = resolve_adb(adb)
    devices = adb_devices(executable, timeout=timeout)
    device = pick_device(devices, serial)
    state = device.get("state") if device else None
    hint = authorization_hint(state)
    if state is None and serial and ":" in serial:
        hint = (
            "无线设备未连接：确认手机与电脑在同一 Wi-Fi，再执行 "
            f".venv/bin/phone adb connect {serial}"
        )
    return {
        "ok": state == ONLINE,
        "adb": executable,
        "devices": devices,
        "serial": device.get("serial") if device else serial,
        "state": state,
        "hint": hint,
    }


def adb_pair(
    host_port: str,
    code: str,
    adb: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Pair with Android wireless debugging (``adb pair HOST:PORT CODE``)."""
    executable = resolve_adb(adb)
    if not host_port:
        raise ValueError("pairing address must be HOST:PORT (shown on the phone)")
    try:
        proc = subprocess.run(
            [executable, "pair", host_port, str(code)],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"adb pair timed out after {timeout:.0f}s") from exc
    output = (
        (proc.stdout or b"").decode("utf-8", errors="replace")
        + (proc.stderr or b"").decode("utf-8", errors="replace")
    ).strip()
    ok = proc.returncode == 0 and "successfully paired" in output.lower()
    return {
        "ok": ok,
        "adb": executable,
        "address": host_port,
        "output": output,
        "hint": (
            "配对成功。点插件的「重连 ADB」，或执行 "
            ".venv/bin/phone adb connect <无线调试页面的 IP:端口>（是调试端口，不是配对端口）"
            if ok
            else "配对失败：确认配对弹窗里的端口和 6 位码、手机与电脑在同一局域网"
        ),
    }


def adb_connect(
    host_port: str,
    adb: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Connect to an already-paired wireless debugging endpoint."""
    executable = resolve_adb(adb)
    if not host_port:
        raise ValueError("connect address must be HOST:PORT")
    try:
        proc = subprocess.run(
            [executable, "connect", host_port],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"adb connect timed out after {timeout:.0f}s") from exc
    output = (
        (proc.stdout or b"").decode("utf-8", errors="replace")
        + (proc.stderr or b"").decode("utf-8", errors="replace")
    ).strip()
    lowered = output.lower()
    ok = proc.returncode == 0 and (
        "connected to" in lowered or "already connected" in lowered
    ) and "failed" not in lowered and "cannot" not in lowered
    return {
        "ok": ok,
        "adb": executable,
        "address": host_port,
        "output": output,
        "hint": (
            "已连接，运行 phone adb status 确认 state=device"
            if ok
            else "连接失败：确认手机已开启无线调试、使用“IP 地址和端口”（不是配对端口）"
        ),
    }


def adb_screen_state(
    adb: str | None = None,
    serial: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Whether the phone screen is awake and unlocked.

    Mirrored video from a dozing or locked phone is empty or stalls, so the
    live-stream commands warn early instead of timing out with "no frame".
    """
    executable = resolve_adb(adb)
    base = [executable]
    if serial:
        base += ["-s", serial]

    def shell(*args: str) -> str:
        try:
            proc = subprocess.run(
                base + list(args), capture_output=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return (proc.stdout or b"").decode("utf-8", errors="replace")

    power = shell("shell", "dumpsys", "power")
    window = shell("shell", "dumpsys", "window")
    awake: bool | None = None
    if power:
        awake = "mWakefulness=Awake" in power
    locked: bool | None = None
    if window:
        locked = "isKeyguardShowing=true" in window
    if locked:
        hint = (
            "手机处于锁屏：投屏/截图拿不到有效画面。请解锁并保持亮屏"
            "（建议把锁屏设为「无」、屏幕超时调长）"
        )
    elif awake is False:
        hint = "手机处于息屏/休眠：请点亮屏幕并保持亮屏"
    else:
        hint = ""
    return {"awake": awake, "locked": locked, "hint": hint}


def wait_for_device(
    adb: str | None = None,
    serial: str | None = None,
    timeout: float = 60.0,
    interval: float = 1.0,
    on_wait: Any = None,
) -> dict[str, Any]:
    """Block until one device is authorized, reporting the reason it is not."""
    executable = resolve_adb(adb)
    deadline = time.monotonic() + max(0.0, timeout)
    last: dict[str, Any] = {
        "ok": False,
        "adb": executable,
        "serial": serial,
        "state": None,
        "devices": [],
        "hint": authorization_hint(None),
    }
    while True:
        status = adb_status(executable, serial=serial)
        last = status
        if status["state"] == ONLINE:
            return status
        if on_wait is not None:
            on_wait(status)
        if time.monotonic() >= deadline:
            last["timed_out"] = True
            return last
        time.sleep(max(0.05, interval))
