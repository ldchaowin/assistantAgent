"""Command-line entry point: `phone`."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .device import Device, DeviceError
from .log import JsonlLogger
from .selftest import replay, run_grid, run_stress
from .transport import (
    MockTransport,
    SerialTransport,
    TcpTransport,
    auto_port,
    list_serial_ports,
    parse_tcp_endpoint,
)


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _parse_screen(value: str) -> tuple[int, int]:
    parts = value.lower().replace("*", "x").split("x")
    if len(parts) != 2:
        raise ValueError("screen must be WxH, e.g. 1080x2400")
    return int(parts[0]), int(parts[1])


def _parse_cal(value: str) -> tuple[int, int, int, int]:
    parts = [int(v.strip()) for v in value.split(",")]
    if len(parts) != 4:
        raise ValueError("cal must be x0,x1,y0,y1")
    return parts[0], parts[1], parts[2], parts[3]


def _parse_roi(value: str) -> tuple[int, int, int, int]:
    parts = [int(v.strip()) for v in value.split(",")]
    if len(parts) != 4:
        raise ValueError("roi must be x,y,w,h")
    x, y, w, h = parts
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise ValueError("roi must have x,y >= 0 and w,h > 0")
    return x, y, w, h


def _open(args: argparse.Namespace) -> Device:
    if args.mock:
        if getattr(args, "tcp", None):
            raise ValueError("--tcp cannot be combined with --mock")
        transport = MockTransport()
    elif getattr(args, "tcp", None):
        host, port = parse_tcp_endpoint(args.tcp)
        transport = TcpTransport(host, port)
    else:
        port = auto_port(args.port)
        transport = SerialTransport(port, args.baud)
    logger = JsonlLogger(args.log) if args.log else None
    return Device(transport, logger=logger)


def _add_eye_source(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--image", metavar="PATH", help="PNG/JPG screenshot")
    group.add_argument(
        "--adb",
        nargs="?",
        const="",
        metavar="SERIAL",
        help="grab a still through adb (optional serial; default device if omitted)",
    )
    group.add_argument(
        "--scrcpy",
        nargs="?",
        const="",
        metavar="SERIAL",
        help="live frames through scrcpy (low latency; needs USB debugging enabled)",
    )
    group.add_argument(
        "--screenrecord",
        nargs="?",
        const="",
        metavar="SERIAL",
        help="live frames through adb screenrecord (no scrcpy needed)",
    )
    group.add_argument(
        "--ios",
        nargs="?",
        const="",
        metavar="DEVICE",
        help="iPhone screen through the native IOSEye helper (device name; auto if omitted)",
    )
    group.add_argument(
        "--ios-mirror",
        nargs="?",
        const="",
        metavar="WINDOW",
        help="capture a mirrored-iPhone window (iPhone Mirroring / UxPlay) instead",
    )
    _add_ios_options(parser)


def _add_stream_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--stream-size",
        type=int,
        default=1024,
        metavar="PX",
        help="scrcpy max video size, long side (default 1024)",
    )
    parser.add_argument(
        "--stream-fps",
        type=int,
        default=30,
        metavar="FPS",
        help="scrcpy max frame rate (default 30)",
    )
    parser.add_argument(
        "--stream-bit-rate",
        type=int,
        default=0,
        metavar="BPS",
        help="override video bit rate (0 = source default)",
    )
    parser.add_argument(
        "--stream-time-limit",
        type=int,
        default=180,
        metavar="S",
        help="screenrecord restart interval in seconds (default 180)",
    )


def _add_ocr_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--det-max",
        type=int,
        default=1024,
        metavar="PX",
        help="longest side for text detection; 0 = native (default 1024)",
    )
    parser.add_argument(
        "--line",
        action="store_true",
        help="treat --roi as a single text line and skip detection",
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="disable the OCR frame/crop cache"
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="deprecated alias for --det-max 768 (faster, less accurate)",
    )


def _engine_kwargs(args: argparse.Namespace, roi: tuple[int, int, int, int] | None) -> dict[str, Any]:
    det_max = getattr(args, "det_max", None)
    if getattr(args, "fast", False):
        det_max = 768 if det_max is None else min(det_max, 768)
    kwargs: dict[str, Any] = {"cache": not getattr(args, "no_cache", False)}
    if det_max is not None:
        kwargs["det_max_side"] = det_max
    if getattr(args, "line", False):
        if roi is None:
            raise ValueError("--line needs --roi (a single text-line region)")
        kwargs["single_line"] = True
    return kwargs


def _eye_source(args: argparse.Namespace):
    from .vision.sources import AdbSource, ImageSource

    if args.image:
        return ImageSource(args.image)
    if getattr(args, "ios_mirror", None) is not None:
        from .vision.mirror import MirrorWindowSource

        return MirrorWindowSource(
            args.ios_mirror or None,
            interval=getattr(args, "mirror_interval", 0.1),
            startup_timeout=getattr(args, "ios_timeout", 20.0) or 20.0,
        )
    if getattr(args, "ios", None) is not None:
        from .vision.ios import open_ios_source

        prefer = (
            "live"
            if getattr(args, "eye_command", None) in {"stream", "watch"}
            else "still"
        )
        return open_ios_source(
            args.ios or None,
            prefer=prefer,
            udid=getattr(args, "ios_udid", None),
            fps=getattr(args, "ios_fps", 0) or 0,
            startup_timeout=getattr(args, "ios_timeout", 12.0) or 12.0,
            timeout=getattr(args, "ios_timeout", 25.0) or 25.0,
        )
    if getattr(args, "scrcpy", None) is not None:
        from .vision.stream import ScrcpySource

        return ScrcpySource(
            args.scrcpy or None,
            max_size=args.stream_size,
            max_fps=args.stream_fps,
            **_bit_rate_kwarg(args),
        )
    if getattr(args, "screenrecord", None) is not None:
        from .vision.stream import ScreenrecordSource

        return ScreenrecordSource(
            args.screenrecord or None,
            time_limit=args.stream_time_limit,
            **_bit_rate_kwarg(args),
        )
    return AdbSource(args.adb or None)


def _add_ios_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ios-fps",
        type=int,
        default=0,
        metavar="FPS",
        help="AVFoundation capture frame rate (0 = device default)",
    )
    parser.add_argument(
        "--ios-udid",
        default=None,
        metavar="UDID",
        help="iPhone UDID for pymobiledevice3/idevicescreenshot fallbacks",
    )
    parser.add_argument(
        "--ios-timeout",
        type=float,
        default=25.0,
        metavar="S",
        help="iOS capture startup timeout (default 25s)",
    )


def _bit_rate_kwarg(args: argparse.Namespace) -> dict[str, Any]:
    rate = getattr(args, "stream_bit_rate", 0) or 0
    return {"bit_rate": rate} if rate else {}


def _warn_if_screen_off(args: argparse.Namespace) -> None:
    """Warn when the phone is locked/asleep: mirrored video will be empty."""
    if getattr(args, "ios", None) is not None:
        return
    serial = getattr(args, "scrcpy", None) or getattr(args, "screenrecord", None)
    try:
        from .vision.adb import adb_screen_state

        state = adb_screen_state(serial=serial or None)
    except Exception:  # noqa: BLE001 - diagnostics only
        return
    if state.get("hint"):
        print(f"warning: {state['hint']}", file=sys.stderr)


def _source_stats(source: Any) -> dict[str, Any] | None:
    stats = getattr(source, "stats", None)
    return stats() if callable(stats) else None



def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phone", description="ESP32-S3 HID console")
    link = parser.add_mutually_exclusive_group()
    link.add_argument("--port", help="serial port, e.g. /dev/cu.usbserial-0001")
    link.add_argument(
        "--tcp",
        metavar="HOST[:PORT]",
        help="control the hand over WiFi TCP (default port 9000)",
    )
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--mock", action="store_true", help="use the in-memory simulator")
    parser.add_argument("--log", help="append JSONL command log to this path")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="list serial ports")

    sub.add_parser("hello", help="handshake and print device info")

    config = sub.add_parser("config", help="set screen size / calibration")
    config.add_argument("--screen", help="WxH, e.g. 1080x2400")
    config.add_argument("--cal", help="x0,x1,y0,y1 in HID units")
    config.add_argument("--save", action="store_true", help="persist to NVS")

    tap = sub.add_parser("tap", help="absolute click")
    tap.add_argument("x", type=int)
    tap.add_argument("y", type=int)
    tap.add_argument("--hold-ms", type=int, default=None)

    swipe = sub.add_parser("swipe", help="smooth swipe")
    swipe.add_argument("x1", type=int)
    swipe.add_argument("y1", type=int)
    swipe.add_argument("x2", type=int)
    swipe.add_argument("y2", type=int)
    swipe.add_argument("--ms", type=int, default=400)

    key = sub.add_parser("key", help="press a key")
    key.add_argument("code")

    text = sub.add_parser("text", help="type ASCII text")
    text.add_argument("value")

    sub.add_parser("status", help="device state")
    sub.add_parser("ping", help="round-trip check")
    sub.add_parser("stop", help="release all inputs")

    selftest = sub.add_parser("selftest", help="tap a grid; watch the phone")
    selftest.add_argument("--cols", type=int, default=5)
    selftest.add_argument("--rows", type=int, default=5)
    selftest.add_argument("--dwell", type=float, default=0.6)

    stress = sub.add_parser("stress", help="repeat taps and report latency")
    stress.add_argument("--n", type=int, default=1000)
    stress.add_argument("--interval", type=float, default=0.3)

    replay_p = sub.add_parser("replay", help="replay tap/swipe/key/text from JSONL")
    replay_p.add_argument("file")
    replay_p.add_argument("--dry-run", action="store_true")

    calibrate_p = sub.add_parser("calibrate", help="guided HID calibration")
    calibrate_p.add_argument("--screen", help="phone screen WxH (default: device)")
    calibrate_p.add_argument("--margin", type=float, default=0.05)
    calibrate_p.add_argument("--save", action="store_true", help="write cal to NVS")
    calibrate_p.add_argument("--yes", action="store_true", help="do not ask before saving")
    calibrate_p.add_argument("--auto", action="store_true", help="assume taps land on target")
    calibrate_p.add_argument("--json", action="store_true")

    accept_p = sub.add_parser("accept", help="run hardware acceptance checks")
    accept_p.add_argument("--json", action="store_true", help="print JSON only")
    accept_p.add_argument("--full", action="store_true", help="1000 taps at 0.3s (slow)")
    accept_p.add_argument("--manual", action="store_true", help="prompt for physical checks")
    accept_p.add_argument("--stress-n", type=int, help="override stress tap count")
    accept_p.add_argument("--long-hours", type=float, default=0.0)
    accept_p.add_argument("--report-dir", metavar="DIR")

    doctor_p = sub.add_parser("doctor", help="pre-flight diagnostics")
    doctor_p.add_argument("--json", action="store_true", help="print JSON only")
    doctor_p.add_argument("--out", metavar="PATH", help="write the JSON report")
    doctor_p.add_argument("--service-url", help="also check this phone service URL")
    doctor_p.add_argument("--service-token", default="", help="service bearer token")
    doctor_p.add_argument("--skip-adb", action="store_true", help="skip the adb check")
    doctor_p.add_argument(
        "--ios",
        action="store_true",
        help="iPhone pre-flight: AVFoundation capture + absolute-mouse hand checks",
    )

    serve_p = sub.add_parser("serve", help="run the local phone control service")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--http-port", type=int, default=8765)
    serve_p.add_argument(
        "--token",
        default=None,
        help="API token (default: PHONE_SERVICE_TOKEN or generated)",
    )
    serve_p.add_argument("--no-auth", action="store_true", help="disable token auth")
    serve_group = serve_p.add_mutually_exclusive_group()
    serve_group.add_argument("--frames", metavar="DIR", help="frame sequence for --mock")
    serve_group.add_argument("--image", metavar="PATH", help="static frame")
    serve_group.add_argument(
        "--adb",
        nargs="?",
        const="",
        metavar="SERIAL",
        help="capture via adb (real device)",
    )
    serve_group.add_argument(
        "--scrcpy",
        nargs="?",
        const="",
        metavar="SERIAL",
        help="live frames through scrcpy (real device, lower latency)",
    )
    serve_group.add_argument(
        "--screenrecord",
        nargs="?",
        const="",
        metavar="SERIAL",
        help="live frames through adb screenrecord (real device, no scrcpy)",
    )
    serve_group.add_argument(
        "--ios",
        nargs="?",
        const="",
        metavar="DEVICE",
        help="live iPhone frames through the native IOSEye helper",
    )
    serve_group.add_argument(
        "--ios-mirror",
        nargs="?",
        const="",
        metavar="WINDOW",
        help="capture the mirrored-iPhone window (iPhone Mirroring / UxPlay)",
    )
    _add_stream_options(serve_p)
    _add_ios_options(serve_p)
    serve_p.add_argument(
        "--no-ios-pointer-reset",
        action="store_true",
        help="do not send the iOS pointer to (0,0) before the first action",
    )
    serve_p.add_argument(
        "--screenshot-dir", metavar="DIR", default="runs/service"
    )
    serve_p.add_argument(
        "--max-frame-age",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help=(
            "refuse /screenshot and /find only when a *dead* capture leaves the "
            "frame older than this (default 60; 0 disables the check). A static "
            "screen with a live capture is always current."
        ),
    )
    serve_p.add_argument(
        "--token-file",
        metavar="PATH",
        help="write the bearer token to this file (mode 600)",
    )

    run_p = sub.add_parser("run", help="run a scenario on the hand + eye")
    run_p.add_argument("scenario", metavar="FILE")
    source_group = run_p.add_mutually_exclusive_group()
    source_group.add_argument(
        "--frames",
        metavar="DIR",
        help="frame sequence for a virtual phone (use with --mock)",
    )
    source_group.add_argument("--image", metavar="PATH", help="static frame")
    source_group.add_argument(
        "--adb",
        nargs="?",
        const="",
        metavar="SERIAL",
        help="capture stills via adb (real device)",
    )
    source_group.add_argument(
        "--scrcpy",
        nargs="?",
        const="",
        metavar="SERIAL",
        help="live frames through scrcpy (real device)",
    )
    source_group.add_argument(
        "--screenrecord",
        nargs="?",
        const="",
        metavar="SERIAL",
        help="live frames through adb screenrecord (real device)",
    )
    source_group.add_argument(
        "--ios",
        nargs="?",
        const="",
        metavar="DEVICE",
        help="live iPhone frames through the native IOSEye helper (real device)",
    )
    source_group.add_argument(
        "--ios-mirror",
        nargs="?",
        const="",
        metavar="WINDOW",
        help="capture the mirrored-iPhone window (iPhone Mirroring / UxPlay)",
    )
    _add_stream_options(run_p)
    _add_ios_options(run_p)
    run_p.add_argument("--run-dir", metavar="DIR", help="artifacts and report directory")
    run_p.add_argument(
        "--record",
        nargs="?",
        const="",
        metavar="DIR",
        help="record frames/actions (default: run dir/record)",
    )
    run_p.add_argument("--json", action="store_true", help="print the run result as JSON")
    run_p.add_argument("--dry-run", action="store_true", help="validate and print steps only")

    eye = sub.add_parser("eye", help="The Eye: capture + OCR + text coordinates")
    eye_sub = eye.add_subparsers(dest="eye_command", required=True)

    ocr_p = eye_sub.add_parser("ocr", help="list every OCR text box")
    _add_eye_source(ocr_p)
    _add_stream_options(ocr_p)
    _add_ocr_options(ocr_p)
    ocr_p.add_argument("--min-score", type=float, default=0.0)
    ocr_p.add_argument("--roi", metavar="X,Y,W,H", help="OCR only this frame region")
    ocr_p.add_argument("--debug", metavar="PNG", help="write an annotated image")

    find_p = eye_sub.add_parser("find", help="find text and print phone coordinates")
    find_p.add_argument("text")
    _add_eye_source(find_p)
    _add_stream_options(find_p)
    _add_ocr_options(find_p)
    find_p.add_argument("--screen", help="phone screen WxH for capture->device scaling")
    find_p.add_argument("--min-score", type=float, default=0.6)
    find_p.add_argument(
        "--mode", choices=["contains", "exact", "regex"], default="contains"
    )
    find_p.add_argument("--index", type=int, default=0, help="nth match in reading order")
    find_p.add_argument("--roi", metavar="X,Y,W,H", help="OCR only this frame region")
    find_p.add_argument("--all", action="store_true", help="print every match")
    find_p.add_argument("--debug", metavar="PNG", help="write an annotated image")

    tap_p = eye_sub.add_parser("tap", help="find text on screen and tap it")
    tap_p.add_argument("text")
    _add_eye_source(tap_p)
    _add_stream_options(tap_p)
    _add_ocr_options(tap_p)
    tap_p.add_argument("--min-score", type=float, default=0.6)
    tap_p.add_argument(
        "--mode", choices=["contains", "exact", "regex"], default="contains"
    )
    tap_p.add_argument("--index", type=int, default=0, help="nth match in reading order")
    tap_p.add_argument("--roi", metavar="X,Y,W,H", help="OCR only this frame region")
    tap_p.add_argument("--hold-ms", type=int, default=None)
    tap_p.add_argument("--debug", metavar="PNG", help="write an annotated image")

    shot_p = eye_sub.add_parser("shot", help="save an adb/iOS screenshot")
    shot_p.add_argument("--out", required=True, metavar="PATH")
    shot_group = shot_p.add_mutually_exclusive_group()
    shot_group.add_argument(
        "--adb",
        nargs="?",
        const="",
        metavar="SERIAL",
        help="optional device serial (Android)",
    )
    shot_group.add_argument(
        "--ios",
        nargs="?",
        const="",
        metavar="DEVICE",
        help="capture an iPhone screen via the native IOSEye helper",
    )
    shot_group.add_argument(
        "--ios-mirror",
        nargs="?",
        const="",
        metavar="WINDOW",
        help="capture the mirrored-iPhone window instead",
    )
    shot_p.add_argument("--ios-udid", default=None, metavar="UDID")
    shot_p.add_argument(
        "--ios-backend",
        choices=["native", "avfoundation", "pymobiledevice3", "idevicescreenshot"],
        default=None,
        help="force one iOS capture backend (native = IOSEye helper, recommended)",
    )
    shot_p.add_argument("--ios-timeout", type=float, default=25.0, metavar="S")

    bench_p = eye_sub.add_parser(
        "bench", help="measure capture + OCR latency (cold, cached, per stage)"
    )
    _add_eye_source(bench_p)
    _add_stream_options(bench_p)
    _add_ocr_options(bench_p)
    bench_p.add_argument("--roi", metavar="X,Y,W,H", help="benchmark only this region")
    bench_p.add_argument("--repeat", type=int, default=5, help="frames to measure")
    bench_p.add_argument("--json", action="store_true", help="print raw JSON only")

    stream_p = eye_sub.add_parser(
        "stream", help="open a live video stream and report decode stats"
    )
    _add_eye_source(stream_p)
    _add_stream_options(stream_p)
    _add_ocr_options(stream_p)
    stream_p.add_argument("--seconds", type=float, default=5.0)
    stream_p.add_argument("--save", metavar="PNG", help="save the last frame")
    stream_p.add_argument("--find", metavar="TEXT", help="OCR the last frame for TEXT")
    stream_p.add_argument("--screen", help="phone screen WxH for coordinate mapping")
    stream_p.add_argument("--min-score", type=float, default=0.6)

    watch_p = eye_sub.add_parser(
        "watch", help="poll the latest frame until TEXT appears"
    )
    watch_p.add_argument("text")
    _add_eye_source(watch_p)
    _add_stream_options(watch_p)
    _add_ocr_options(watch_p)
    watch_p.add_argument("--screen", help="phone screen WxH for coordinate mapping")
    watch_p.add_argument("--roi", metavar="X,Y,W,H", help="OCR only this frame region")
    watch_p.add_argument("--timeout", type=float, default=30.0)
    watch_p.add_argument("--interval", type=float, default=0.2)
    watch_p.add_argument("--min-score", type=float, default=0.6)
    watch_p.add_argument(
        "--mode", choices=["contains", "exact", "regex"], default="contains"
    )
    watch_p.add_argument("--index", type=int, default=0)

    wifi_p = sub.add_parser("wifi", help="WiFi station + TCP control channel")
    wifi_sub = wifi_p.add_subparsers(dest="wifi_command", required=True)
    wifi_set_p = wifi_sub.add_parser("set", help="save SSID/password and connect")
    wifi_set_p.add_argument("ssid")
    wifi_set_p.add_argument("password")
    wifi_set_p.add_argument("--tcp-port", type=int, default=None)
    wifi_set_p.add_argument(
        "--wait", type=float, default=20.0, help="seconds to wait for connected"
    )
    wifi_sub.add_parser("status", help="show WiFi/IP state")
    wifi_sub.add_parser("clear", help="erase credentials and disable WiFi")
    wifi_sub.add_parser("prov", help="start BLE provisioning on the device")
    wifi_sub.add_parser("devices", help="scan BLE for PROV_* devices")
    wifi_prov_p = wifi_sub.add_parser(
        "provision", help="provision the device's WiFi over BLE from this Mac"
    )
    wifi_prov_p.add_argument("--ssid", required=True)
    wifi_prov_p.add_argument("--password", required=True)
    wifi_prov_p.add_argument("--device", help="BLE name, e.g. PROV_A1B2")
    wifi_prov_p.add_argument("--pop", help="proof of possession (default hand-XXXX)")
    wifi_prov_p.add_argument("--timeout", type=float, default=180.0)

    adb_p = sub.add_parser("adb", help="adb device state and USB debugging authorization")
    adb_sub = adb_p.add_subparsers(dest="adb_command", required=True)
    adb_status_p = adb_sub.add_parser("status", help="list devices and authorization state")
    adb_status_p.add_argument("--serial")
    adb_status_p.add_argument("--adb-path", metavar="PATH", help="adb executable")
    adb_wait_p = adb_sub.add_parser("wait", help="wait until a device is authorized")
    adb_wait_p.add_argument("--serial")
    adb_wait_p.add_argument("--adb-path", metavar="PATH", help="adb executable")
    adb_wait_p.add_argument("--timeout", type=float, default=60.0)
    adb_pair_p = adb_sub.add_parser(
        "pair", help="pair wireless debugging: pair HOST:PORT CODE"
    )
    adb_pair_p.add_argument("address", metavar="HOST:PORT")
    adb_pair_p.add_argument("code", help="6-digit pairing code shown on the phone")
    adb_pair_p.add_argument("--adb-path", metavar="PATH", help="adb executable")
    adb_connect_p = adb_sub.add_parser(
        "connect", help="connect wireless debugging: connect HOST:PORT"
    )
    adb_connect_p.add_argument("address", metavar="HOST:PORT")
    adb_connect_p.add_argument("--adb-path", metavar="PATH", help="adb executable")

    ios_p = sub.add_parser("ios", help="iPhone eye (AVFoundation) + absolute-mouse hand")
    ios_sub = ios_p.add_subparsers(dest="ios_command", required=True)

    def _ios_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--ffmpeg", metavar="PATH", help="ffmpeg executable")
        parser.add_argument("--json", action="store_true", help="JSON only (default)")

    ios_status_p = ios_sub.add_parser("status", help="iPhone presence + capture readiness")
    _ios_common(ios_status_p)
    ios_status_p.add_argument("--serial", help="UDID to select when several are attached")

    ios_devices_p = ios_sub.add_parser("devices", help="list usbmuxd + AVFoundation devices")
    _ios_common(ios_devices_p)

    ios_wait_p = ios_sub.add_parser("wait", help="wait until the iPhone screen is capturable")
    _ios_common(ios_wait_p)
    ios_wait_p.add_argument("--serial")
    ios_wait_p.add_argument("--timeout", type=float, default=60.0)
    ios_wait_p.add_argument("--interval", type=float, default=1.0)

    ios_build_p = ios_sub.add_parser(
        "build", help="compile + sign the IOSEye native capture helper"
    )
    ios_build_p.add_argument(
        "--force",
        action="store_true",
        help="rebuild even if the helper exists (resets the camera permission)",
    )

    ios_mirror_p = ios_sub.add_parser(
        "mirror", help="AirPlay / iPhone Mirroring window capture (plan B: hand owns USB)"
    )
    mirror_sub = ios_mirror_p.add_subparsers(dest="ios_mirror_command", required=True)
    mirror_list_p = mirror_sub.add_parser(
        "list", help="list on-screen windows and pick the mirrored iPhone"
    )
    mirror_list_p.add_argument("--all", action="store_true", help="include non-mirror windows")
    mirror_shot_p = mirror_sub.add_parser("shot", help="capture one frame of the mirror window")
    mirror_shot_p.add_argument("--out", required=True, metavar="PATH")
    mirror_shot_p.add_argument("--window", help="window id or name fragment")
    mirror_stream_p = mirror_sub.add_parser("stream", help="capture stats for N seconds")
    mirror_stream_p.add_argument("--seconds", type=float, default=5.0)
    mirror_stream_p.add_argument("--window", help="window id or name fragment")
    mirror_stream_p.add_argument("--interval", type=float, default=0.1)
    mirror_stream_p.add_argument("--save", metavar="PNG", help="save the last frame")

    ios_shot_p = ios_sub.add_parser("shot", help="save one iPhone screenshot")
    _ios_common(ios_shot_p)
    ios_shot_p.add_argument("--out", required=True, metavar="PATH")
    ios_shot_p.add_argument("--device", help="AVFoundation device name/index")
    ios_shot_p.add_argument("--udid")
    ios_shot_p.add_argument(
        "--backend",
        choices=["native", "avfoundation", "pymobiledevice3", "idevicescreenshot"],
        default=None,
        help="force one capture backend (native = IOSEye helper, recommended)",
    )
    ios_shot_p.add_argument("--timeout", type=float, default=25.0)

    ios_stream_p = ios_sub.add_parser("stream", help="live capture stats (frames/fps/size)")
    _ios_common(ios_stream_p)
    ios_stream_p.add_argument("--device", help="AVFoundation device name/index")
    ios_stream_p.add_argument("--seconds", type=float, default=5.0)
    ios_stream_p.add_argument("--fps", type=int, default=0)
    ios_stream_p.add_argument("--save", metavar="PNG", help="save the last frame")
    ios_stream_p.add_argument("--find", metavar="TEXT", help="OCR the last frame for TEXT")
    ios_stream_p.add_argument("--screen", help="phone screen WxH for coordinate mapping")
    ios_stream_p.add_argument("--min-score", type=float, default=0.6)

    ios_hand_p = ios_sub.add_parser(
        "hand", help="align the absolute-mouse hand with the iPhone screen"
    )
    _ios_common(ios_hand_p)
    ios_hand_p.add_argument("--device", help="AVFoundation device name/index")
    ios_hand_p.add_argument("--screen", help="iPhone screen WxH (default: captured frame)")
    ios_hand_p.add_argument("--cal", help="x0,x1,y0,y1 HID calibration (default: unchanged)")
    ios_hand_p.add_argument(
        "--margin",
        type=float,
        default=0.0,
        help="inset the calibration window against iOS edge dead zones (0.02 = 2%%)",
    )
    ios_hand_p.add_argument("--save", action="store_true", help="persist to NVS")
    ios_hand_p.add_argument(
        "--no-capture",
        action="store_true",
        help="do not capture a frame; use the hand's current screen size",
    )
    ios_hand_p.add_argument(
        "--mirror",
        nargs="?",
        const="",
        metavar="WINDOW",
        help="size the hand from the mirrored-iPhone window (plan B: no USB eye)",
    )
    ios_hand_p.add_argument(
        "--no-warmup",
        action="store_true",
        help="do not send the pointer reset (move 0,0)",
    )

    for name, help_text in (
        ("home", "gesture: swipe up from the bottom edge (Home)"),
        ("back", "gesture: swipe in from the left edge (Back)"),
        ("app-switch", "gesture: slow swipe up from the bottom edge"),
    ):
        gesture_p = ios_sub.add_parser(name, help=help_text)
        _ios_common(gesture_p)

    ios_tap_p = ios_sub.add_parser("tap", help="absolute tap on the hand (iOS coordinates)")
    _ios_common(ios_tap_p)
    ios_tap_p.add_argument("x", type=int)
    ios_tap_p.add_argument("y", type=int)
    ios_tap_p.add_argument("--hold-ms", type=int, default=None)

    ios_swipe_p = ios_sub.add_parser("swipe", help="smooth swipe on the hand")
    _ios_common(ios_swipe_p)
    ios_swipe_p.add_argument("x1", type=int)
    ios_swipe_p.add_argument("y1", type=int)
    ios_swipe_p.add_argument("x2", type=int)
    ios_swipe_p.add_argument("y2", type=int)
    ios_swipe_p.add_argument("--ms", type=int, default=400)

    ios_text_p = ios_sub.add_parser("text", help="type ASCII text through the HID keyboard")
    _ios_common(ios_text_p)
    ios_text_p.add_argument("value")

    ios_key_p = ios_sub.add_parser("key", help="press a keyboard key through the HID keyboard")
    _ios_common(ios_key_p)
    ios_key_p.add_argument("code")

    return parser


def _run_calibrate(args: argparse.Namespace) -> int:
    from .calibration import run_calibration
    from .factory import build_phone

    phone = None
    try:
        phone = build_phone(
            mock=args.mock,
            port=args.port,
            baud=args.baud,
            tcp=getattr(args, "tcp", None),
            log=args.log,
        )
        screen = _parse_screen(args.screen) if args.screen else None
        result = run_calibration(
            phone,
            screen=screen,
            margin=args.margin,
            save=args.save,
            auto=args.auto,
            yes=args.yes,
            output=None if args.json else print,
        )
    finally:
        if phone is not None:
            phone.close()

    if args.json:
        _print(result.to_dict())
    ok = result.valid and (not args.save or result.saved)
    return 0 if ok else 1


def _run_accept(args: argparse.Namespace) -> int:
    from .acceptance import run_acceptance, write_report
    from .factory import build_phone
    from .scenario import default_run_dir

    phone = None
    try:
        phone = build_phone(
            mock=args.mock,
            port=args.port,
            baud=args.baud,
            tcp=getattr(args, "tcp", None),
            log=args.log,
        )
        quick = not args.full
        stress_n = args.stress_n or (50 if quick else 1000)
        report = run_acceptance(
            phone,
            "mock" if args.mock else "real",
            quick=quick,
            manual=args.manual,
            stress_n=stress_n,
            stress_interval=0.0 if quick else 0.3,
            long_hours=args.long_hours,
            output=None if args.json else print,
        )
    finally:
        if phone is not None:
            phone.close()

    report_dir = Path(args.report_dir) if args.report_dir else default_run_dir(
        f"accept-{'mock' if args.mock else 'real'}"
    )
    json_path, html_path = write_report(report, report_dir)
    if args.json:
        payload = report.to_dict()
        payload["report_json"] = str(json_path)
        payload["report_html"] = str(html_path)
        _print(payload)
    else:
        print(f"report: {json_path}")
        print(f"html:   {html_path}")
    return 0 if report.ok else 1


def _run_doctor(args: argparse.Namespace) -> int:
    from .doctor import CheckResult, DoctorReport, run_doctor, write_report
    from .factory import build_phone

    platform = "ios" if getattr(args, "ios", False) else None
    mode = "mock" if args.mock else "real"
    hand_configured = bool(
        args.mock or getattr(args, "port", None) or getattr(args, "tcp", None)
    )
    phone = None
    try:
        if hand_configured:
            phone = build_phone(
                mock=args.mock,
                port=args.port,
                baud=args.baud,
                tcp=getattr(args, "tcp", None),
                log=args.log,
            )
        report = run_doctor(
            phone,
            mode,
            platform=platform,
            service_url=args.service_url,
            service_token=args.service_token,
            check_adb=not args.skip_adb,
            check_stream=platform is None,
            output=None,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics should report, not crash
        report = DoctorReport(
            mode=mode,
            checks=[CheckResult("build", "fail", str(exc))],
        )
    finally:
        if phone is not None:
            phone.close()

    if args.json:
        payload = report.to_dict()
        if args.out:
            payload["report"] = str(write_report(report, args.out))
        _print(payload)
    else:
        print(report.to_text())
        if args.out:
            print(f"report: {write_report(report, args.out)}")
    return 0 if report.ok else 1


def _run_serve(args: argparse.Namespace) -> int:
    from .factory import build_phone
    from .service import create_server, new_token, serve_forever

    phone = build_phone(
        mock=args.mock,
        port=args.port,
        baud=args.baud,
        frames=args.frames,
        image=args.image,
        adb=args.adb,
        tcp=getattr(args, "tcp", None),
        scrcpy=getattr(args, "scrcpy", None),
        screenrecord=getattr(args, "screenrecord", None),
        ios=getattr(args, "ios", None),
        ios_mirror=getattr(args, "ios_mirror", None),
        ios_udid=getattr(args, "ios_udid", None),
        ios_fps=getattr(args, "ios_fps", 0),
        ios_pointer_reset=not getattr(args, "no_ios_pointer_reset", False),
        stream_size=getattr(args, "stream_size", 1024),
        stream_fps=getattr(args, "stream_fps", 30),
        stream_bit_rate=getattr(args, "stream_bit_rate", 0),
        stream_time_limit=getattr(args, "stream_time_limit", 180),
        log=args.log,
    )
    engine = None
    try:
        from .vision.ocr import create_engine

        engine = create_engine()
        engine.warmup()
        print("ocr: ready")
    except Exception as exc:  # noqa: BLE001 - the hand still works without OCR
        print(f"ocr: unavailable ({exc})", file=sys.stderr)
    token = ""
    if not args.no_auth:
        token = args.token or os.environ.get("PHONE_SERVICE_TOKEN") or new_token()
    try:
        server = create_server(
            phone,
            host=args.host,
            port=args.http_port,
            token=token,
            screenshot_dir=args.screenshot_dir,
            engine=engine,
            max_frame_age_ms=float(getattr(args, "max_frame_age", 10.0) or 0.0) * 1000.0,
        )
    except Exception:
        phone.close()
        raise
    host, port = server.server_address[:2]
    print(f"phone service: http://{host}:{port}")
    print(f"auth: {'disabled' if not token else 'bearer token'}")
    if token:
        print(f"token: {token}")
    if token and args.token_file:
        try:
            token_path = Path(args.token_file).expanduser()
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(token + "\n", encoding="utf-8")
            token_path.chmod(0o600)
            print(f"token file: {token_path}")
        except OSError as exc:
            print(f"WARNING: could not write token file: {exc}")
    if not token and args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("WARNING: auth disabled on a non-loopback address")
    try:
        serve_forever(server)
    except KeyboardInterrupt:
        pass
    finally:
        phone.close()
    return 0


def _build_scenario_phone(args: argparse.Namespace, scenario):
    from .factory import build_phone

    frames = args.frames
    if frames is None and args.mock and not args.image and scenario.frames_path:
        frames = str(scenario.frames_path)
    return build_phone(
        mock=args.mock,
        port=args.port,
        baud=args.baud,
        frames=frames,
        image=args.image,
        adb=args.adb,
        tcp=getattr(args, "tcp", None),
        scrcpy=getattr(args, "scrcpy", None),
        screenrecord=getattr(args, "screenrecord", None),
        ios=getattr(args, "ios", None),
        ios_mirror=getattr(args, "ios_mirror", None),
        ios_udid=getattr(args, "ios_udid", None),
        ios_fps=getattr(args, "ios_fps", 0),
        stream_size=getattr(args, "stream_size", 1024),
        stream_fps=getattr(args, "stream_fps", 30),
        stream_bit_rate=getattr(args, "stream_bit_rate", 0),
        stream_time_limit=getattr(args, "stream_time_limit", 180),
        log=args.log,
    )


def _run_scenario(args: argparse.Namespace) -> int:
    from .scenario import Scenario, ScenarioRunner, default_run_dir

    scenario = Scenario.load(args.scenario)
    if args.dry_run:
        _print(scenario.to_dict())
        return 0

    phone = None
    run_dir = Path(args.run_dir) if args.run_dir else default_run_dir(scenario.name)
    try:
        phone = _build_scenario_phone(args, scenario)
        if args.record is not None:
            from .phone import RecordingPhone

            record_dir = Path(args.record) if args.record else run_dir / "record"
            phone = RecordingPhone(phone, record_dir)
        result = ScenarioRunner(
            phone,
            scenario,
            run_dir=run_dir,
            output=None if args.json else print,
        ).run()
    finally:
        if phone is not None:
            phone.close()

    if args.json:
        _print(result.to_dict())
    else:
        print(f"result: {'PASS' if result.ok else 'FAIL'}")
        if result.error:
            print(f"error: {result.error}")
        if result.run_dir:
            print(f"run dir: {result.run_dir}")
    return 0 if result.ok else 1


def _summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"min": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0, "mean": 0.0}
    ordered = sorted(values)

    def pct(p: float) -> float:
        index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
        return round(ordered[index], 2)

    return {
        "min": round(ordered[0], 2),
        "p50": pct(0.5),
        "p95": pct(0.95),
        "max": round(ordered[-1], 2),
        "mean": round(sum(ordered) / len(ordered), 2),
    }


def _run_eye_bench(args: argparse.Namespace) -> int:
    from time import perf_counter

    from .vision.ocr import create_engine

    roi = _parse_roi(args.roi) if getattr(args, "roi", None) else None
    source = _eye_source(args)
    try:
        engine = create_engine("rapidocr", **_engine_kwargs(args, roi=roi))
        engine.warmup()
        capture: list[float] = []
        ocr: list[float] = []
        total: list[float] = []
        det: list[float] = []
        rec: list[float] = []
        items = 0
        frame = None
        for _ in range(max(1, int(args.repeat))):
            started = perf_counter()
            frame = source.grab()
            grabbed = perf_counter()
            ocr_frame = frame.crop(roi) if roi else frame
            found = engine.recognize(ocr_frame.image)
            finished = perf_counter()
            capture.append((grabbed - started) * 1000.0)
            ocr.append((finished - grabbed) * 1000.0)
            total.append((finished - started) * 1000.0)
            det.append(engine.last_stage_ms.get("det", 0.0))
            rec.append(engine.last_stage_ms.get("rec", 0.0))
            items = len(found)
        payload = {
            "ok": True,
            "source": source.name,
            "frame": list(frame.size) if frame else None,
            "roi": list(roi) if roi else None,
            "repeat": len(total),
            "det_max": getattr(args, "det_max", None),
            "line": bool(getattr(args, "line", False)),
            "cache": not getattr(args, "no_cache", False),
            "capture_ms": _summary(capture),
            "ocr_ms": _summary(ocr),
            "total_ms": _summary(total),
            "det_ms": _summary(det),
            "rec_ms": _summary(rec),
            "items": items,
            "cache_hits": dict(engine.hits),
            "source_stats": _source_stats(source),
        }
        _print(payload)
        return 0
    finally:
        source.close()


def _run_eye_stream(args: argparse.Namespace) -> int:
    from time import perf_counter

    from .vision.locate import TextLocator
    from .vision.ocr import create_engine

    source = _eye_source(args)
    if not callable(getattr(source, "wait_newer", None)):
        source.close()
        raise ValueError("eye stream needs --scrcpy or --screenrecord")
    _warn_if_screen_off(args)
    try:
        started = perf_counter()
        first = source.grab()
        first_ms = (perf_counter() - started) * 1000.0
        last_ts = first.ts
        frames = 1
        intervals: list[float] = []
        deadline = started + max(0.1, float(args.seconds))
        while True:
            remaining = deadline - perf_counter()
            if remaining <= 0:
                break
            try:
                frame = source.wait_newer(last_ts, timeout=min(1.0, remaining))
            except Exception:  # noqa: BLE001 - stream ended or stalled
                break
            intervals.append((frame.ts - last_ts) * 1000.0)
            last_ts = frame.ts
            frames += 1
        latest = source.latest() or first
        elapsed = max(1e-6, perf_counter() - started)
        payload: dict[str, Any] = {
            "ok": True,
            "source": source.name,
            "size": list(latest.size),
            "first_frame_ms": round(first_ms, 1),
            "frames": frames,
            "seconds": round(elapsed, 2),
            "decode_fps": round(frames / elapsed, 2),
            "interval_ms": _summary(intervals) if intervals else None,
            "stats": source.stats(),
        }
        if getattr(args, "save", None):
            payload["saved"] = str(latest.save(args.save))
        if getattr(args, "find", None):
            engine = create_engine("rapidocr", **_engine_kwargs(args, roi=None))
            items = engine.recognize(latest.image)
            screen = (
                _parse_screen(args.screen)
                if getattr(args, "screen", None)
                else latest.size
            )
            if not getattr(args, "screen", None):
                print(
                    "warning: live frames may be scaled (--stream-size); pass "
                    "--screen WxH for phone coordinates",
                    file=sys.stderr,
                )
            match = TextLocator(
                screen=screen, min_score=args.min_score
            ).find(items, args.find, latest.size)
            payload["find"] = {
                "keyword": args.find,
                "found": match is not None,
                "match": match.as_dict() if match else None,
                "screen": list(screen),
                "ocr_ms": round(engine.last_elapsed_ms, 2),
                "stages": engine.last_stage_ms,
                "items": len(items),
            }
        _print(payload)
        return 0
    finally:
        source.close()


def _run_eye_watch(args: argparse.Namespace) -> int:
    import time
    from time import perf_counter

    from .vision.locate import TextLocator
    from .vision.ocr import create_engine, offset_items

    roi = _parse_roi(args.roi) if getattr(args, "roi", None) else None
    source = _eye_source(args)
    if callable(getattr(source, "wait_newer", None)):
        _warn_if_screen_off(args)
    try:
        engine = create_engine("rapidocr", **_engine_kwargs(args, roi=roi))
        started = perf_counter()
        deadline = started + max(0.1, float(args.timeout))
        attempts = 0
        last_ts = 0.0
        last_ocr_ms = 0.0
        items: list[Any] = []
        screen = _parse_screen(args.screen) if getattr(args, "screen", None) else None
        wait_newer = getattr(source, "wait_newer", None)
        while True:
            remaining = deadline - perf_counter()
            if remaining <= 0:
                break
            if callable(wait_newer):
                try:
                    frame = wait_newer(
                        last_ts,
                        timeout=min(max(0.1, float(args.interval) * 2.0), remaining),
                    )
                except Exception:  # noqa: BLE001 - fall back to a plain grab
                    frame = source.grab()
            else:
                frame = source.grab()
            last_ts = frame.ts
            ocr_frame = frame.crop(roi) if roi else frame
            items = engine.recognize(ocr_frame.image)
            last_ocr_ms = engine.last_elapsed_ms
            if roi:
                items = offset_items(items, roi[0], roi[1])
            attempts += 1
            locator = TextLocator(
                screen=screen or frame.size,
                min_score=args.min_score,
                mode=args.mode,
            )
            match = locator.find(items, args.text, frame.size, index=args.index)
            elapsed_ms = (perf_counter() - started) * 1000.0
            payload: dict[str, Any] = {
                "ok": match is not None,
                "found": match is not None,
                "keyword": args.text,
                "source": source.name,
                "frame": list(frame.size),
                "screen": list(screen or frame.size),
                "roi": list(roi) if roi else None,
                "attempts": attempts,
                "elapsed_ms": round(elapsed_ms, 2),
                "ocr_ms": round(last_ocr_ms, 2),
                "stages": engine.last_stage_ms,
                "items": len(items),
                "match": match.as_dict() if match else None,
                "cache_hits": dict(engine.hits),
                "source_stats": _source_stats(source),
            }
            if match is not None:
                _print(payload)
                return 0
            pause = min(float(args.interval), max(0.0, deadline - perf_counter()))
            if pause > 0:
                time.sleep(pause)
        _print(
            {
                "ok": False,
                "found": False,
                "keyword": args.text,
                "source": source.name,
                "attempts": attempts,
                "timeout": args.timeout,
                "ocr_ms": round(last_ocr_ms, 2),
                "stages": engine.last_stage_ms,
                "items": len(items),
                "cache_hits": dict(engine.hits),
                "source_stats": _source_stats(source),
            }
        )
        print(f"not found: {args.text!r}", file=sys.stderr)
        return 1
    finally:
        source.close()


def _run_provision(args: argparse.Namespace) -> int:
    from .provisioning import (
        ProvisionError,
        pop_from_device_name,
        provision,
        scan_devices,
    )

    if args.wifi_command == "devices":
        devices = scan_devices()
        _print({"ok": bool(devices), "devices": devices})
        return 0 if devices else 1

    device = args.device
    if not device:
        devices = scan_devices()
        if len(devices) == 1:
            device = devices[0]["name"]
        elif not devices:
            print("ERROR: no PROV_* device found; power the hand and retry", file=sys.stderr)
            _print({"ok": False, "devices": []})
            return 1
        else:
            _print({"ok": False, "error": "multiple devices; pass --device", "devices": devices})
            return 1
    pop = args.pop or pop_from_device_name(device)
    result = provision(
        args.ssid,
        args.password,
        device=device,
        pop=pop,
        timeout=args.timeout,
    )
    result["pop"] = pop
    _print(result)
    if not result["ok"]:
        print("ERROR: BLE provisioning failed; see output", file=sys.stderr)
        return 1
    return 0


def _run_wifi(args: argparse.Namespace) -> int:
    from .device import DeviceTimeout
    from .provisioning import ProvisionError

    if args.wifi_command in {"provision", "devices"}:
        try:
            return _run_provision(args)
        except (ProvisionError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    device: Device | None = None
    try:
        device = _open(args)
        # Opening the UART resets the ESP32; wait for its boot-ready event
        # before sending the actual command.
        try:
            device.hello()
        except Exception:  # noqa: BLE001 - the next request reports the real error
            pass
        if args.wifi_command == "status":
            response = device.wifi_status()
            _print({"ok": True, "wifi": response.get("wifi", {})})
            return 0
        if args.wifi_command == "prov":
            response = device.request("wifi", op="prov")
            _print(response)
            return 0
        if args.wifi_command == "clear":
            device.wifi_clear()
            response = device.wifi_status()
            _print({"ok": True, "wifi": response.get("wifi", {})})
            return 0

        response = device.wifi_set(args.ssid, args.password, args.tcp_port)
        events: list[dict[str, Any]] = [
            event.get("wifi") or {}
            for event in list(device.events)
            if event.get("evt") == "wifi"
        ]
        final: dict[str, Any] = (
            events[-1] if events else (response.get("wifi") or {})
        )
        deadline = time.monotonic() + max(0.5, float(args.wait))
        while (
            final.get("state") not in {"connected", "failed"}
            and time.monotonic() < deadline
        ):
            try:
                event = device.wait_event(
                    "wifi",
                    timeout=min(1.0, max(0.05, deadline - time.monotonic())),
                )
            except DeviceTimeout:
                continue
            state = event.get("wifi") or {}
            events.append(state)
            final = state
        try:
            final = device.wifi_status().get("wifi", final)
        except RuntimeError:
            pass
        ok = final.get("state") == "connected"
        _print({"ok": ok, "ssid": args.ssid, "wifi": final, "events": events})
        return 0 if ok else 1
    finally:
        if device is not None:
            device.close()


def _run_adb(args: argparse.Namespace) -> int:
    from .vision.adb import (
        adb_connect,
        adb_pair,
        adb_status,
        wait_for_device,
    )

    adb_path = getattr(args, "adb_path", None)
    serial = getattr(args, "serial", None)
    if args.adb_command == "pair":
        result = adb_pair(args.address, args.code, adb=adb_path)
        _print(result)
        if not result["ok"]:
            print(f"hint: {result['hint']}", file=sys.stderr)
            return 1
        return 0
    if args.adb_command == "connect":
        result = adb_connect(args.address, adb=adb_path)
        result["status"] = adb_status(adb_path, serial=args.address)
        _print(result)
        if not result["ok"]:
            print(f"hint: {result['hint']}", file=sys.stderr)
            return 1
        return 0
    if args.adb_command == "status":
        status = adb_status(adb_path, serial=serial)
        _print(status)
        if not status["ok"]:
            print(f"hint: {status['hint']}", file=sys.stderr)
            return 1
        return 0
    seen: set[str] = set()

    def progress(status: dict[str, Any]) -> None:
        hint = str(status.get("hint", ""))
        if hint and hint not in seen:
            seen.add(hint)
            print(f"waiting: {hint}", file=sys.stderr)

    status = wait_for_device(
        adb_path, serial=serial, timeout=args.timeout, on_wait=progress
    )
    _print(status)
    if not status["ok"]:
        print(f"hint: {status['hint']}", file=sys.stderr)
        return 1
    return 0


def _run_ios_stream(args: argparse.Namespace) -> int:
    from time import perf_counter

    from .vision.ios import open_ios_source

    source = open_ios_source(
        getattr(args, "device", None),
        prefer="live",
        ffmpeg=getattr(args, "ffmpeg", None),
        fps=getattr(args, "fps", 0) or 0,
        startup_timeout=20.0,
    )
    try:
        started = perf_counter()
        first = source.grab()
        first_ms = (perf_counter() - started) * 1000.0
        last_ts = first.ts
        frames = 1
        intervals: list[float] = []
        deadline = started + max(0.1, float(args.seconds))
        while True:
            remaining = deadline - perf_counter()
            if remaining <= 0:
                break
            try:
                frame = source.wait_newer(last_ts, timeout=min(1.0, remaining))
            except Exception:  # noqa: BLE001 - stream ended or stalled
                break
            intervals.append((frame.ts - last_ts) * 1000.0)
            last_ts = frame.ts
            frames += 1
        latest = source.latest() or first
        elapsed = max(1e-6, perf_counter() - started)
        payload: dict[str, Any] = {
            "ok": True,
            "source": source.name,
            "size": list(latest.size),
            "first_frame_ms": round(first_ms, 1),
            "frames": frames,
            "seconds": round(elapsed, 2),
            "fps": round(frames / elapsed, 2),
            "interval_ms": _summary(intervals) if intervals else None,
            "stats": source.stats(),
        }
        if getattr(args, "save", None):
            payload["saved"] = str(latest.save(args.save))
        if getattr(args, "find", None):
            from .vision.locate import TextLocator
            from .vision.ocr import create_engine

            engine = create_engine("rapidocr")
            engine.warmup()
            items = engine.recognize(latest.image)
            screen = (
                _parse_screen(args.screen)
                if getattr(args, "screen", None)
                else latest.size
            )
            match = TextLocator(
                screen=screen, min_score=args.min_score
            ).find(items, args.find, latest.size)
            payload["find"] = {
                "keyword": args.find,
                "found": match is not None,
                "match": match.as_dict() if match else None,
            }
        _print(payload)
        return 0
    finally:
        source.close()


def _run_ios_mirror(args: argparse.Namespace) -> int:
    from .vision import mirror as mirror_mod

    sub = args.ios_mirror_command
    if sub == "list":
        windows = mirror_mod.list_windows(include_all=bool(args.all))
        candidates = [window for window in windows if mirror_mod._is_mirror_window(window)]
        _print(
            {
                "ok": bool(candidates),
                "platform": "ios",
                "mirror_available": mirror_mod.mirror_available(),
                "selected": mirror_mod.find_mirror_window(),
                "candidates": candidates,
                "windows": windows if args.all else None,
                "hint": (
                    "打开 iPhone Mirroring（系统自带，需解锁 iPhone 连接）或用 UxPlay 接收投屏"
                    if not candidates
                    else "投屏窗口可用；可运行 phone ios mirror shot --out shots/mirror.png"
                ),
            }
        )
        return 0 if candidates else 1
    if sub == "shot":
        frame, window = mirror_mod.capture_mirror_shot(args.window or None)
        target = frame.save(args.out)
        _print(
            {
                "ok": True,
                "out": str(target),
                "source": "ios:mirror",
                "window": window,
                "frame": list(frame.size),
            }
        )
        return 0
    # stream
    source = mirror_mod.MirrorWindowSource(
        args.window or None, interval=args.interval, startup_timeout=20.0
    )
    try:
        started = time.perf_counter()
        first = source.grab()
        last_ts = first.ts
        frames = 1
        intervals: list[float] = []
        deadline = started + max(0.2, float(args.seconds))
        while time.perf_counter() < deadline:
            try:
                frame = source.wait_newer(last_ts, timeout=min(1.0, max(0.05, deadline - time.perf_counter())))
            except Exception:  # noqa: BLE001 - stream ended
                break
            intervals.append((frame.ts - last_ts) * 1000.0)
            last_ts = frame.ts
            frames += 1
        latest = source.latest() or first
        elapsed = max(1e-6, time.perf_counter() - started)
        payload: dict[str, Any] = {
            "ok": True,
            "source": source.name,
            "window": source.window,
            "size": list(latest.size),
            "frames": frames,
            "seconds": round(elapsed, 2),
            "fps": round(frames / elapsed, 2),
            "interval_ms": _summary(intervals) if intervals else None,
            "stats": source.stats(),
        }
        if args.save:
            payload["saved"] = str(latest.save(args.save))
        _print(payload)
        return 0
    finally:
        source.close()


def _run_ios_hand_command(args: argparse.Namespace, command: str) -> int:
    from .ios import (
        IosPhone,
        configure_hand_for_ios,
        go_back,
        go_home,
        show_app_switcher,
    )
    from .phone import NullSource
    from .vision import ios as ios_vision

    device: Device | None = None
    try:
        device = _open(args)
        hello = device.hello()
        phone = IosPhone(device, NullSource())
        if command == "hand":
            frame = None
            backend: str | None = None
            if getattr(args, "mirror", None) is not None:
                from .vision.mirror import capture_mirror_shot

                try:
                    frame, window = capture_mirror_shot(args.mirror or None)
                    backend = f"mirror:{window.get('owner')}"
                except Exception as exc:  # noqa: BLE001 - fall back to the hand's size
                    print(f"warning: 投屏窗口取帧失败（{exc}）；改用手当前屏幕尺寸", file=sys.stderr)
            elif not args.no_capture:
                try:
                    frame, backend = ios_vision.capture_once(
                        getattr(args, "device", None), ffmpeg=getattr(args, "ffmpeg", None)
                    )
                except ios_vision.IosError as exc:
                    print(
                        f"warning: iOS 画面采集不可用（{exc}）；"
                        "改用控制手当前屏幕尺寸",
                        file=sys.stderr,
                    )
            screen = _parse_screen(args.screen) if args.screen else (frame.size if frame else None)
            cal = _parse_cal(args.cal) if args.cal else None
            result = configure_hand_for_ios(
                phone,
                screen=screen,
                cal=cal,
                margin=args.margin,
                save=args.save,
                warmup=not args.no_warmup,
            )
            result["capture_backend"] = backend
            result["hello"] = hello
            _print(result)
            return 0 if result.get("ok") else 1

        if command == "home":
            response = go_home(phone)
        elif command == "back":
            response = go_back(phone)
        elif command == "app-switch":
            response = show_app_switcher(phone)
        elif command == "tap":
            response = phone.tap(args.x, args.y, args.hold_ms)
        elif command == "swipe":
            response = phone.swipe(
                args.x1, args.y1, args.x2, args.y2, args.ms
            )
        elif command == "text":
            response = phone.text(args.value)
        elif command == "key":
            response = phone.key(args.code)
        else:
            raise ValueError(f"unknown ios command: {command}")
        _print(
            {
                "ok": True,
                "command": command,
                "screen": list(phone.screen),
                "hid": hello.get("hid"),
                "response": response,
            }
        )
        return 0
    finally:
        if device is not None:
            device.close()


def _run_ios(args: argparse.Namespace) -> int:
    from .vision import ios as ios_vision

    command = args.ios_command
    ffmpeg = getattr(args, "ffmpeg", None)
    if command == "status":
        status = ios_vision.ios_status(
            serial=getattr(args, "serial", None), ffmpeg=ffmpeg
        )
        try:
            from .vision import mirror as mirror_mod

            if mirror_mod.mirror_available():
                status["mirror"] = {
                    "available": True,
                    "window": mirror_mod.find_mirror_window(),
                    "candidates": mirror_mod.mirror_windows(),
                }
            else:
                status["mirror"] = {"available": False, "error": "pyobjc Quartz 未安装"}
        except Exception as exc:  # noqa: BLE001 - optional diagnostics
            status["mirror"] = {"available": False, "error": str(exc)}
        _print(status)
        if not status["ok"]:
            print(f"hint: {status['hint']}", file=sys.stderr)
            return 1
        return 0
    if command == "devices":
        payload: dict[str, Any] = {
            "ok": False,
            "platform": "ios",
            "tools": ios_vision.tool_availability(ffmpeg),
        }
        try:
            payload["devices"] = ios_vision.usbmux_list()
        except ios_vision.IosError as exc:
            payload["devices"] = []
            payload["usbmux_error"] = str(exc)
        try:
            payload["capture_devices"] = ios_vision.list_capture_devices(ffmpeg)
        except ios_vision.IosError as exc:
            payload["capture_devices"] = []
            payload["capture_error"] = str(exc)
        payload["capture_device"] = ios_vision.pick_capture_device(
            payload.get("capture_devices", [])
        )
        payload["ok"] = bool(
            payload.get("devices") or payload.get("capture_devices")
        )
        _print(payload)
        return 0 if payload["ok"] else 1
    if command == "wait":
        seen: set[str] = set()

        def progress(status: dict[str, Any]) -> None:
            hint = str(status.get("hint", ""))
            if hint and hint not in seen:
                seen.add(hint)
                print(f"waiting: {hint}", file=sys.stderr)

        status = ios_vision.wait_for_ios(
            timeout=args.timeout,
            interval=args.interval,
            serial=getattr(args, "serial", None),
            ffmpeg=ffmpeg,
            on_wait=progress,
        )
        _print(status)
        if not status["ok"]:
            print(f"hint: {status['hint']}", file=sys.stderr)
            return 1
        return 0
    if command == "mirror":
        return _run_ios_mirror(args)
    if command == "build":
        result = ios_vision.build_ios_eye(force=bool(getattr(args, "force", False)))
        _print(result)
        if result.get("rebuilt"):
            print(
                "hint: 重新编译会重置 macOS 相机授权；下次截图时点一次「允许」即可",
                file=sys.stderr,
            )
        return 0 if result.get("ok") else 1
    if command == "shot":
        frame, backend = ios_vision.capture_once(
            getattr(args, "device", None),
            ffmpeg=ffmpeg,
            udid=getattr(args, "udid", None),
            backend=getattr(args, "backend", None),
            timeout=args.timeout,
        )
        target = frame.save(args.out)
        _print(
            {
                "ok": True,
                "out": str(target),
                "backend": backend,
                "frame": list(frame.size),
                "source": f"ios:{backend}",
            }
        )
        return 0
    if command == "stream":
        return _run_ios_stream(args)
    return _run_ios_hand_command(args, command)


def _run_eye(args: argparse.Namespace) -> int:
    try:
        from .vision.locate import TextLocator
        from .vision.ocr import create_engine, offset_items
        from .vision.overlay import annotate
    except ImportError as exc:  # pragma: no cover - depends on env
        raise RuntimeError(
            "vision dependencies are missing; install the vision extras:\n"
            "  .venv/bin/pip install -e 'console[vision]'"
        ) from exc

    if args.eye_command == "shot":
        if getattr(args, "ios_mirror", None) is not None:
            from .vision.mirror import capture_mirror_shot

            frame, window = capture_mirror_shot(args.ios_mirror or None)
            target = frame.save(args.out)
            _print(
                {
                    "ok": True,
                    "out": str(target),
                    "source": "ios:mirror",
                    "window": window,
                    "frame": list(frame.size),
                }
            )
            return 0
        if getattr(args, "ios", None) is not None:
            from .vision.ios import capture_once

            frame, backend = capture_once(
                args.ios or None,
                udid=args.ios_udid,
                backend=args.ios_backend,
                timeout=args.ios_timeout,
            )
            target = frame.save(args.out)
            _print(
                {
                    "ok": True,
                    "out": str(target),
                    "source": f"ios:{backend}",
                    "backend": backend,
                    "frame": list(frame.size),
                }
            )
            return 0
        from .vision.sources import AdbSource

        source = AdbSource(args.adb or None)
        try:
            frame = source.grab()
        finally:
            source.close()
        target = frame.save(args.out)
        _print(
            {
                "ok": True,
                "out": str(target),
                "source": source.name,
                "frame": list(frame.size),
            }
        )
        return 0

    if args.eye_command == "bench":
        return _run_eye_bench(args)
    if args.eye_command == "stream":
        return _run_eye_stream(args)
    if args.eye_command == "watch":
        return _run_eye_watch(args)

    source = _eye_source(args)
    device: Device | None = None
    try:
        screen: tuple[int, int] | None
        if args.eye_command in {"find", "ocr"} and callable(
            getattr(source, "wait_newer", None)
        ) and not getattr(args, "screen", None):
            print(
                "warning: live frames may be scaled (--stream-size); pass "
                "--screen WxH for phone coordinates",
                file=sys.stderr,
            )
        if args.eye_command == "tap":
            device = _open(args)
            device.hello()
            screen = device.screen
            if getattr(args, "ios", None) is not None:
                # iOS absolute-pointer hosts can have edge dead zones: send the
                # pointer home once before the click (firmware 0.4.0+ `move`).
                try:
                    device.request("move", timeout=1.0, x=0, y=0)
                except DeviceError:
                    pass
        else:
            screen_arg = getattr(args, "screen", None)
            screen = _parse_screen(screen_arg) if screen_arg else None

        frame = source.grab()
        if (
            device is not None
            and getattr(args, "ios", None) is not None
            and tuple(device.screen) != tuple(frame.size)
        ):
            print(
                f"warning: 控制手屏幕 {device.screen[0]}x{device.screen[1]} 与 iPhone 采集帧 "
                f"{frame.width}x{frame.height} 不一致；先运行 "
                "`phone ios hand --save` 对齐后再点击",
                file=sys.stderr,
            )
        roi = _parse_roi(args.roi) if getattr(args, "roi", None) else None
        ocr_frame = frame.crop(roi) if roi else frame
        engine = create_engine("rapidocr", **_engine_kwargs(args, roi=roi))
        items = engine.recognize(ocr_frame.image)
        if roi:
            items = offset_items(items, roi[0], roi[1])
        elapsed = round(engine.last_elapsed_ms, 2)
        screen = screen or frame.size
        stages = dict(engine.last_stage_ms)

        if args.eye_command == "ocr":
            selected = [item for item in items if item.score >= args.min_score]
            if args.debug:
                annotate(frame, selected, args.debug)
            _print(
                {
                    "source": source.name,
                    "frame": list(frame.size),
                    "roi": list(roi) if roi else None,
                    "elapsed_ms": elapsed,
                    "stages": stages,
                    "count": len(selected),
                    "items": [item.as_dict() for item in selected],
                    "source_stats": _source_stats(source),
                }
            )
            return 0

        locator = TextLocator(screen=screen, min_score=args.min_score, mode=args.mode)
        matches = locator.find_all(items, args.text, frame.size)

        if args.eye_command == "find":
            match = None
            if matches:
                index = args.index + len(matches) if args.index < 0 else args.index
                if not 0 <= index < len(matches):
                    raise IndexError(
                        f"index {args.index} out of range; {len(matches)} match(es)"
                    )
                match = matches[index]
            if args.debug:
                highlighted = matches if args.all else ([match] if match else [])
                annotate(frame, items, args.debug, highlighted)
            payload: dict[str, Any] = {
                "source": source.name,
                "keyword": args.text,
                "mode": args.mode,
                "min_score": args.min_score,
                "frame": list(frame.size),
                "screen": list(screen),
                "roi": list(roi) if roi else None,
                "elapsed_ms": elapsed,
                "stages": stages,
                "count": len(matches),
                "source_stats": _source_stats(source),
            }
            if args.all:
                payload["matches"] = [item.as_dict() for item in matches]
            else:
                payload["match"] = match.as_dict() if match else None
            _print(payload)
            if not matches:
                print(f"not found: {args.text!r}", file=sys.stderr)
                return 1
            return 0

        # eye tap
        assert device is not None
        match = locator.find(items, args.text, frame.size, index=args.index)
        if match is None:
            if args.debug:
                annotate(frame, items, args.debug)
            _print(
                {
                    "source": source.name,
                    "keyword": args.text,
                    "frame": list(frame.size),
                    "screen": list(screen),
                    "roi": list(roi) if roi else None,
                    "elapsed_ms": elapsed,
                    "stages": stages,
                    "match": None,
                }
            )
            print(f"not found: {args.text!r}", file=sys.stderr)
            return 1
        if args.debug:
            annotate(frame, items, args.debug, [match])
        response = device.tap(*match.device_center, hold_ms=args.hold_ms)
        _print(
            {
                "source": source.name,
                "keyword": args.text,
                "frame": list(frame.size),
                "screen": list(screen),
                "roi": list(roi) if roi else None,
                "elapsed_ms": elapsed,
                "stages": stages,
                "match": match.as_dict(),
                "tap": response,
            }
        )
        return 0
    finally:
        source.close()
        if device is not None:
            device.close()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "scan":
        _print(list_serial_ports())
        return 0

    if args.command == "calibrate":
        try:
            return _run_calibrate(args)
        except (DeviceError, ImportError, OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    if args.command == "accept":
        try:
            return _run_accept(args)
        except (DeviceError, ImportError, OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    if args.command == "doctor":
        try:
            return _run_doctor(args)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    if args.command == "serve":
        try:
            return _run_serve(args)
        except (
            DeviceError,
            ImportError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    if args.command == "run":
        try:
            return _run_scenario(args)
        except (
            DeviceError,
            ImportError,
            IndexError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    if args.command == "wifi":
        try:
            return _run_wifi(args)
        except (DeviceError, OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    if args.command == "adb":
        try:
            return _run_adb(args)
        except (OSError, RuntimeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    if args.command == "ios":
        try:
            return _run_ios(args)
        except (
            DeviceError,
            ImportError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    if args.command == "eye":
        try:
            return _run_eye(args)
        except (
            DeviceError,
            ImportError,
            IndexError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    device: Device | None = None
    try:
        device = _open(args)
        # The firmware drops to FAULT after 30s without a request. A fresh CLI
        # process would otherwise fail every tap/key after an idle gap.
        if args.command in {"tap", "swipe", "key", "text", "config"}:
            device.hello()

        if args.command == "hello":
            _print(device.hello())
        elif args.command == "config":
            if not args.screen and not args.cal:
                raise ValueError("pass --screen and/or --cal")
            screen = _parse_screen(args.screen) if args.screen else None
            cal = _parse_cal(args.cal) if args.cal else None
            _print(device.configure(screen=screen, cal=cal))
            if args.save:
                _print(device.save())
        elif args.command == "tap":
            _print(device.tap(args.x, args.y, args.hold_ms))
        elif args.command == "swipe":
            _print(device.swipe(args.x1, args.y1, args.x2, args.y2, args.ms))
        elif args.command == "key":
            _print(device.key(args.code))
        elif args.command == "text":
            _print(device.text(args.value))
        elif args.command == "status":
            _print(device.status())
        elif args.command == "ping":
            _print(device.ping())
        elif args.command == "stop":
            _print(device.stop())
        elif args.command == "selftest":
            device.hello()
            run_grid(device, args.cols, args.rows, args.dwell)
        elif args.command == "stress":
            device.hello()
            run_stress(device, args.n, args.interval)
        elif args.command == "replay":
            device.hello()
            records = replay(device, args.file, args.dry_run)
            print(f"replayed {len(records)} action(s)")
        else:
            raise ValueError(f"unknown command: {args.command}")
        return 0
    except (DeviceError, ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if device is not None:
            device.close()


if __name__ == "__main__":
    raise SystemExit(main())
