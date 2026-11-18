"""Build a Phone from CLI/service flags: real device or virtual."""
from __future__ import annotations

from pathlib import Path

from .device import Device
from .log import JsonlLogger
from .phone import DevicePhone, NullSource
from .transport import (
    MockTransport,
    SerialTransport,
    TcpTransport,
    auto_port,
    parse_tcp_endpoint,
)


def build_source(
    *,
    image: str | Path | None = None,
    adb: str | None = None,
    scrcpy: str | None = None,
    screenrecord: str | None = None,
    ios: str | None = None,
    ios_mirror: str | None = None,
    ios_udid: str | None = None,
    ios_fps: int = 0,
    ios_timeout: float = 25.0,
    mirror_interval: float = 0.1,
    stream_size: int = 1024,
    stream_fps: int = 30,
    stream_bit_rate: int = 0,
    stream_time_limit: int = 180,
):
    """Pick a frame source: still image, adb, iOS AVFoundation, or a live H.264 stream."""
    if image is not None:
        from .vision.sources import ImageSource

        return ImageSource(image)
    if ios_mirror is not None:
        from .vision.mirror import MirrorWindowSource

        return MirrorWindowSource(
            ios_mirror or None, interval=mirror_interval, startup_timeout=ios_timeout
        )
    if ios is not None:
        from .vision.ios import open_ios_source

        return open_ios_source(
            ios or None,
            prefer="live",
            udid=ios_udid,
            fps=ios_fps,
            timeout=ios_timeout,
        )
    if adb is not None:
        from .vision.adb import preferred_adb_serial
        from .vision.sources import AdbSource

        return AdbSource(serial=preferred_adb_serial(serial=adb or None))
    if scrcpy is not None:
        from .vision.adb import preferred_adb_serial
        from .vision.stream import ScrcpySource

        kwargs = {"max_size": stream_size, "max_fps": stream_fps}
        if stream_bit_rate:
            kwargs["bit_rate"] = stream_bit_rate
        # Why USB first: adb screencap over WiFi is 2-7s per still, while the
        # same frame over USB is a few hundred ms.
        return ScrcpySource(
            serial=preferred_adb_serial(serial=scrcpy or None), **kwargs
        )
    if screenrecord is not None:
        from .vision.adb import preferred_adb_serial
        from .vision.stream import ScreenrecordSource

        kwargs = {"time_limit": stream_time_limit}
        if stream_bit_rate:
            kwargs["bit_rate"] = stream_bit_rate
        return ScreenrecordSource(
            serial=preferred_adb_serial(serial=screenrecord or None), **kwargs
        )
    return NullSource()


def build_phone(
    *,
    mock: bool,
    port: str | None = None,
    baud: int = 460800,
    frames: str | Path | None = None,
    image: str | Path | None = None,
    adb: str | None = None,
    tcp: str | None = None,
    scrcpy: str | None = None,
    screenrecord: str | None = None,
    ios: str | None = None,
    ios_mirror: str | None = None,
    ios_udid: str | None = None,
    ios_fps: int = 0,
    ios_pointer_reset: bool = True,
    mirror_interval: float = 0.1,
    stream_size: int = 1024,
    stream_fps: int = 30,
    stream_bit_rate: int = 0,
    stream_time_limit: int = 180,
    timeout: float = 1.0,
    log: str | None = None,
):
    """Construct a Phone for either a virtual (mock) or real setup.

    ``frames`` is only valid with ``mock``; ``adb`` / ``scrcpy`` /
    ``screenrecord`` / ``ios`` are only valid on a real device. ``image``
    works for both (static frame). ``ios`` selects the iPhone eye
    (AVFoundation capture); it may be an empty string for auto-discovery or a
    capture-device name/index.
    """
    if mock:
        if (
            adb is not None
            or scrcpy is not None
            or screenrecord is not None
            or ios is not None
            or ios_mirror is not None
        ):
            raise ValueError(
                "--adb/--scrcpy/--screenrecord/--ios/--ios-mirror cannot be combined with --mock"
            )
        if tcp is not None:
            raise ValueError("--tcp cannot be combined with --mock")
        if image is not None:
            from .vision.sources import ImageSource

            return DevicePhone(
                Device(MockTransport(), timeout=timeout), ImageSource(image)
            )
        if frames is not None:
            from .virtual import VirtualPhone

            return VirtualPhone.from_dir(frames, timeout=timeout)
        return DevicePhone(Device(MockTransport(), timeout=timeout), NullSource())

    if frames is not None:
        raise ValueError(
            "--frames is only for --mock; use --adb, --scrcpy, --ios or --image "
            "on a real device"
        )
    source = build_source(
        image=image,
        adb=adb,
        scrcpy=scrcpy,
        screenrecord=screenrecord,
        ios=ios,
        ios_mirror=ios_mirror,
        ios_udid=ios_udid,
        ios_fps=ios_fps,
        mirror_interval=mirror_interval,
        stream_size=stream_size,
        stream_fps=stream_fps,
        stream_bit_rate=stream_bit_rate,
        stream_time_limit=stream_time_limit,
    )
    if tcp is not None:
        host, tcp_port = parse_tcp_endpoint(tcp)
        transport = TcpTransport(host, tcp_port)
    else:
        transport = SerialTransport(auto_port(port), baud)
    device = Device(
        transport,
        timeout=timeout,
        logger=JsonlLogger(log) if log else None,
    )
    if ios is not None or ios_mirror is not None:
        from .ios import IosPhone

        return IosPhone(device, source, pointer_reset=ios_pointer_reset)
    return DevicePhone(device, source)
