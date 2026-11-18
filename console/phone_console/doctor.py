"""Pre-flight diagnostics: is this machine ready for the hand/eye stack?"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .phone import Phone
from .transport import list_serial_ports

DEFAULT_SERVICE_TOKEN_FILE = Path.home() / ".dsh" / "phone-service" / "token"


@dataclass
class CheckResult:
    name: str
    status: str  # pass / warn / fail / skip
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "data": self.data,
        }


@dataclass
class DoctorReport:
    mode: str
    checks: list[CheckResult]
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    versions: dict[str, Any] = field(default_factory=dict)
    platform: str | None = None

    @property
    def ok(self) -> bool:
        return not any(check.status == "fail" for check in self.checks)

    def counts(self) -> dict[str, int]:
        counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "platform": self.platform,
            "started_at": self.started_at,
            "versions": self.versions,
            "summary": self.counts(),
            "checks": [check.to_dict() for check in self.checks],
        }

    def to_text(self) -> str:
        label = f"{self.mode} / {self.platform}" if self.platform else self.mode
        lines = [f"phone doctor ({label})"]
        for check in self.checks:
            lines.append(f"[{check.status.upper():4s}] {check.name}: {check.detail}")
        counts = self.counts()
        lines.append(
            "summary: "
            f"{counts['pass']} pass, {counts['warn']} warn, "
            f"{counts['fail']} fail, {counts['skip']} skip"
        )
        return "\n".join(lines)


def _vision_check() -> tuple[str, str, dict[str, Any]]:
    modules = ["numpy", "PIL", "rapidocr_onnxruntime", "cv2"]
    found = {name: importlib.util.find_spec(name) is not None for name in modules}
    missing = [name for name, present in found.items() if not present]
    if missing:
        return (
            "warn",
            f"missing vision modules: {', '.join(missing)} (hand-only still works)",
            {"modules": found},
        )
    detail = "numpy / Pillow / OpenCV / RapidOCR available"
    try:
        from importlib.metadata import version as package_version

        detail += f" (rapidocr {package_version('rapidocr-onnxruntime')})"
    except Exception:  # noqa: BLE001 - diagnostics
        pass
    return "pass", detail, {"modules": found}


def _stream_check() -> tuple[str, str, dict[str, Any]]:
    """Live capture readiness: PyAV decoding + scrcpy binary."""
    from .vision.stream import av_version, scrcpy_version

    av = av_version()
    scrcpy = shutil.which("scrcpy")
    version = scrcpy_version(scrcpy) if scrcpy else None
    data: dict[str, Any] = {"pyav": av, "scrcpy": scrcpy, "scrcpy_version": version}
    if av is None:
        return (
            "warn",
            "PyAV missing: live scrcpy/screenrecord decoding unavailable "
            "(.venv/bin/pip install -e 'console[stream]')",
            data,
        )
    if scrcpy is None:
        return (
            "warn",
            "PyAV ready; scrcpy not installed, use --screenrecord fallback "
            "(brew install scrcpy)",
            data,
        )
    return "pass", f"PyAV {av}; scrcpy {version or '?'}", data


def _adb_check(timeout: float = 3.0) -> tuple[str, str, dict[str, Any]]:
    try:
        from .vision.adb import AdbError, adb_status

        status = adb_status(timeout=timeout)
    except AdbError as exc:
        return "skip", str(exc), {}
    except Exception as exc:  # noqa: BLE001 - diagnostics
        return "warn", f"adb devices failed: {exc}", {}
    data = {
        "adb": status["adb"],
        "devices": status["devices"],
        "state": status["state"],
        "serial": status["serial"],
    }
    if status["state"] == "device":
        return "pass", f"authorized: {status['serial']}", data
    return "warn", f"adb present, device {status['state'] or 'missing'}: {status['hint']}", data


def _ios_checks(ffmpeg: str | None = None) -> list[tuple[str, str, str, dict[str, Any]]]:
    """iOS readiness: ffmpeg, usbmuxd device, AVFoundation capture device."""
    from .vision.ios import IosError, ios_status

    try:
        status = ios_status(ffmpeg=ffmpeg, probe_capture=True)
    except IosError as exc:  # pragma: no cover - defensive
        return [("ios", "fail", str(exc), {})]
    tools = status.get("tools") or {}
    checks: list[tuple[str, str, str, dict[str, Any]]] = []

    native_tools = tools.get("native_eye") or {}
    if native_tools.get("built") or native_tools.get("clang"):
        detail = "IOSEye helper"
        if native_tools.get("built"):
            detail += " (built, signed)"
        elif native_tools.get("clang"):
            detail += " (will build on first use)"
        optional = [
            name
            for name in ("ffmpeg", "pymobiledevice3", "idevicescreenshot")
            if tools.get(name)
        ]
        if optional:
            detail += "; fallback: " + ", ".join(optional)
        checks.append(("ios-tools", "pass", detail, tools))
    else:
        checks.append(
            (
                "ios-tools",
                "fail",
                "IOSEye helper not built and clang missing: xcode-select --install, "
                "then `phone ios build`",
                tools,
            )
        )

    devices = status.get("devices") or []
    if devices:
        names = []
        for device in devices:
            name = device.get("name") or device.get("serial") or "?"
            version = device.get("ios_version")
            names.append(f"{name} (iOS {version})" if version else str(name))
        checks.append(
            (
                "ios-device",
                "pass",
                "usbmuxd: " + ", ".join(names),
                {"devices": devices},
            )
        )
    else:
        checks.append(
            (
                "ios-device",
                "fail",
                "no iPhone on USB: " + str(status.get("usbmux_error") or status.get("hint")),
                {"devices": []},
            )
        )

    native = status.get("native") or {}
    if native.get("ok"):
        detail = f"IOSEye native capture: {native.get('device') or 'iPhone'}"
        width, height = native.get("width"), native.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            detail += f" {width}x{height}"
        checks.append(("ios-native", "pass", detail, {"native": native}))
    else:
        detail = str(native.get("hint") or native.get("error") or "IOSEye capture not ready")
        checks.append(("ios-native", "warn" if native else "skip", detail, {"native": native}))

    capture_device = status.get("capture_device")
    if native.get("ok"):
        checks.append(
            (
                "ios-capture",
                "pass",
                "native helper (AVFoundation/CoreMediaIO muxed iPhone device)",
                {"backend": "native"},
            )
        )
    elif capture_device:
        checks.append(
            (
                "ios-capture",
                "pass",
                f"AVFoundation [{capture_device.get('index')}] {capture_device.get('name')}",
                {"capture_device": capture_device,
                 "capture_devices": status.get("capture_devices") or []},
            )
        )
    else:
        available = status.get("capture_devices") or []
        detail = status.get("hint") or "no AVFoundation capture device"
        if available:
            detail += " (visible: " + ", ".join(
                f"[{item['index']}] {item['name']}" for item in available
            ) + ")"
        checks.append(
            (
                "ios-capture",
                "warn",
                str(detail),
                {"capture_devices": available},
            )
        )
    # Plan B for iOS: the hand owns the iPhone's USB port, so the eye is a
    # mirrored window (iPhone Mirroring / UxPlay). Informational, never fails
    # an otherwise-ready native setup.
    try:
        from .vision.mirror import find_mirror_window, mirror_available, mirror_windows

        if not mirror_available():
            checks.append(
                (
                    "ios-mirror",
                    "skip",
                    "pyobjc Quartz 未安装（方案 B 投屏取帧需要：.venv/bin/pip install -e 'console[mirror]'）",
                    {},
                )
            )
        else:
            window = find_mirror_window()
            if window:
                checks.append(
                    (
                        "ios-mirror",
                        "pass",
                        f"投屏窗口: {window.get('owner')} "
                        f"{window.get('width')}x{window.get('height')}",
                        {"window": window, "candidates": mirror_windows()},
                    )
                )
            else:
                checks.append(
                    (
                        "ios-mirror",
                        "warn",
                        "没有投屏窗口：方案 B（ESP32 接 iPhone）需要打开 iPhone Mirroring 或 UxPlay",
                        {"candidates": []},
                    )
                )
    except Exception as exc:  # noqa: BLE001 - optional diagnostics
        checks.append(("ios-mirror", "skip", str(exc), {}))
    return checks


def run_doctor(
    phone: Phone | None,
    mode: str,
    *,
    platform: str | None = None,
    ffmpeg: str | None = None,
    service_url: str | None = None,
    service_token: str = "",
    check_adb: bool = True,
    check_vision: bool = True,
    check_stream: bool = True,
    output: Callable[[str], None] | None = print,
) -> DoctorReport:
    checks: list[CheckResult] = []
    versions: dict[str, Any] = {
        "phone_console": __version__,
        "python": sys.version.split()[0],
    }

    python_ok = sys.version_info >= (3, 10)
    checks.append(
        CheckResult(
            "python",
            "pass" if python_ok else "fail",
            sys.version.split()[0],
            {"major": sys.version_info.major, "minor": sys.version_info.minor},
        )
    )
    checks.append(CheckResult("package", "pass", f"phone-console {__version__}"))

    if mode == "mock":
        checks.append(CheckResult("serial", "skip", "mock phone"))
    else:
        try:
            ports = list_serial_ports()
            if ports:
                detail = ", ".join(
                    f"{port['device']} ({port['description']})" for port in ports
                )
                checks.append(CheckResult("serial", "pass", detail, {"ports": ports}))
            else:
                checks.append(
                    CheckResult("serial", "warn", "no serial ports found", {"ports": []})
                )
        except Exception as exc:  # noqa: BLE001 - diagnostics
            checks.append(CheckResult("serial", "warn", f"port scan failed: {exc}"))

    hello: dict[str, Any] = {}
    if phone is None:
        if platform == "ios":
            checks.append(
                CheckResult(
                    "hand",
                    "skip",
                    "no hand configured (eye-only iOS debugging; "
                    "pass --port/--tcp to also check the hand)",
                )
            )
        else:
            checks.append(CheckResult("device", "fail", "phone was not built"))
    else:
        try:
            hello = phone.hello()
            checks.append(
                CheckResult(
                    "hello",
                    "pass",
                    f"fw={hello.get('fw', '?')} dev={hello.get('dev', '?')} "
                    f"hid={hello.get('hid', '?')} screen={hello.get('screen')}",
                    hello,
                )
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics
            checks.append(CheckResult("hello", "fail", str(exc)))

        if hello:
            try:
                status = phone.status()
                state = status.get("state", "?")
                usb = status.get("usb")
                level = "pass" if state == "READY" else "warn"
                detail = f"state={state} usb={usb} last_err={status.get('last_err', '')}"
                checks.append(CheckResult("status", level, detail, status))
            except Exception as exc:  # noqa: BLE001 - diagnostics
                checks.append(CheckResult("status", "fail", str(exc)))

            try:
                pong = phone.ping()
                rtt = float(pong.get("rtt_ms", 0.0))
                level = "pass" if rtt <= 100 else "warn"
                checks.append(
                    CheckResult("ping", level, f"rtt={rtt:.2f} ms", pong)
                )
            except Exception as exc:  # noqa: BLE001 - diagnostics
                checks.append(CheckResult("ping", "fail", str(exc)))

            screen = tuple(phone.screen)
            screen_ok = 100 <= screen[0] <= 5000 and 100 <= screen[1] <= 5000
            checks.append(
                CheckResult(
                    "screen",
                    "pass" if screen_ok else "fail",
                    f"{screen[0]}x{screen[1]}",
                    {"screen": list(screen)},
                )
            )

            cal = tuple(phone.cal)
            cal_ok = (
                len(cal) == 4
                and all(0 <= value <= 32767 for value in cal)
                and cal[0] != cal[1]
                and cal[2] != cal[3]
            )
            default_cal = cal == (0, 32767, 0, 32767)
            if not cal_ok:
                level, detail = "fail", f"invalid cal={cal}"
            elif default_cal:
                level, detail = "warn", "default cal 0,32767,0,32767 (not calibrated)"
            else:
                level, detail = "pass", f"cal={cal}"
            checks.append(CheckResult("calibration", level, detail, {"cal": list(cal)}))

    if check_vision:
        status, detail, data = _vision_check()
        checks.append(CheckResult("vision", status, detail, data))

    if check_adb and platform != "ios":
        status, detail, data = _adb_check()
        checks.append(CheckResult("adb", status, detail, data))

    if platform == "ios":
        for name, status, detail, data in _ios_checks(ffmpeg):
            checks.append(CheckResult(name, status, detail, data))
        if hello:
            from .ios import hand_checks

            for check in hand_checks(hello):
                checks.append(
                    CheckResult(
                        check["name"],
                        check["status"],
                        check["detail"],
                        check.get("data") or {},
                    )
                )

    if check_stream:
        status, detail, data = _stream_check()
        checks.append(CheckResult("stream", status, detail, data))

    if service_url:
        try:
            from .client import PhoneClient

            health = PhoneClient(service_url, token=service_token, timeout=3.0).health()
            checks.append(
                CheckResult(
                    "service",
                    "pass",
                    f"{service_url} version={health.get('version', '?')} "
                    f"auth={health.get('auth')}",
                    health,
                )
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics
            checks.append(CheckResult("service", "warn", str(exc), {"url": service_url}))
        if not service_token and DEFAULT_SERVICE_TOKEN_FILE.is_file():
            checks.append(
                CheckResult(
                    "service-token",
                    "pass",
                    f"token file present: {DEFAULT_SERVICE_TOKEN_FILE}",
                )
            )

    report = DoctorReport(
        mode=mode, checks=checks, versions=versions, platform=platform
    )
    if output is not None:
        output(report.to_text())
    return report


def write_report(report: DoctorReport, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target
