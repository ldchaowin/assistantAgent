"""iOS platform glue: hand-side setup and iOS-specific gestures.

The *eye* for iOS lives in :mod:`phone_console.vision.ios` (AVFoundation
capture). This module covers the *hand* side of an iPhone setup:

* :class:`IosPhone` -- a :class:`~phone_console.phone.DevicePhone` whose
  screen size comes from the captured iPhone frame (the hand may be
  disconnected in eye-only debugging mode) and whose pointer is "warmed up"
  before the first absolute-mouse action.
* iOS absolute-pointer notes: the ESP32-S3 must run the absolute-mouse
  firmware (``make build-abs`` / ``make flash-abs``) and the iPhone needs
  AssistiveTouch enabled so a pointer appears. Older iOS builds have dead
  zones at the screen edges, hence the ``(0, 0)`` pointer reset before the
  first action (firmware ``move`` command).
* Gesture helpers for the iPhone's edge gestures: ``home`` (swipe up from the
  bottom edge), ``back`` (swipe from the left edge) and ``app_switch`` (a slow
  swipe up and hold).
"""
from __future__ import annotations

from typing import Any

from .device import Device, DeviceError
from .phone import DevicePhone
from .vision.frame import Frame

# ``hello`` reports the HID flavour baked into the firmware; iOS needs the
# absolute mouse (a single-touch digitizer is not a pointing device there).
IOS_HID_MODE = "absmouse"
ABS_FIRMWARE_HINT = (
    "iOS 需要绝对鼠标固件（当前固件 hid={hid}）："
    "先 `make build-abs` 再用 UART 口烧录 `make flash-abs PORT=/dev/cu.usbserial-XXXX`；"
    "iPhone 端打开「设置 → 辅助功能 → 触控 → 辅助触控」让指针出现。详见 docs/ios.md"
)

# Seconds to wait for the pointer-reset move reply (old firmware lacks it).
_POINTER_RESET_TIMEOUT = 1.0


class IosPhone(DevicePhone):
    """DevicePhone with iOS screen semantics and pointer warm-up."""

    platform = "ios"

    def __init__(
        self,
        device: Device,
        source: Any,
        *,
        pointer_reset: bool = True,
    ) -> None:
        super().__init__(device, source)
        self.pointer_reset = bool(pointer_reset)
        self._pointer_warmed = False

    # ------------------------------------------------------------------ screen
    @property
    def screen(self) -> tuple[int, int]:
        """Prefer the iPhone's native resolution reported by the capture source.

        ``native_size`` is the device's real pixel grid even when frames are
        downscaled for OCR; ``frame_size`` is the fallback for still sources.
        In eye-only debugging the hand is not connected, so ``device.screen``
        still holds the ESP32 default (1080x2400) until ``hello`` runs.
        """
        for attribute in ("native_size", "frame_size"):
            size = getattr(self.source, attribute, None)
            if callable(size):
                size = size()
            if size:
                return (int(size[0]), int(size[1]))
        return self.device.screen

    # ----------------------------------------------------------------- pointer
    def warm_pointer(self) -> dict[str, Any]:
        """Send the pointer to (0, 0) once so absolute moves start from a known spot.

        The firmware ``move`` command was added for iOS (dead zones at the
        screen edges); older firmware simply answers UNKNOWN_CMD, which is
        harmless and remembered so we only try once.
        """
        if self._pointer_warmed:
            return {"ok": True, "skipped": True}
        self._pointer_warmed = True
        if not self.pointer_reset:
            return {"ok": True, "disabled": True}
        try:
            response = self.device.request(
                "move", timeout=_POINTER_RESET_TIMEOUT, x=0, y=0
            )
        except DeviceError as exc:
            if exc.code in {"UNKNOWN_CMD", "TIMEOUT", "BUSY"}:
                return {"ok": False, "unsupported": True, "error": exc.code}
            return {"ok": False, "error": exc.code}
        return {"ok": True, "response": response}

    def tap(self, x: int, y: int, hold_ms: int | None = None) -> dict:
        self.warm_pointer()
        return self.device.tap(x, y, hold_ms)

    def swipe(
        self, x1: int, y1: int, x2: int, y2: int, ms: int = 400
    ) -> dict:
        self.warm_pointer()
        return self.device.swipe(x1, y1, x2, y2, ms)

    # --------------------------------------------------------------- gestures
    def _gesture(self, x1: int, y1: int, x2: int, y2: int, ms: int) -> dict:
        self.warm_pointer()
        return self.device.swipe(x1, y1, x2, y2, ms)

    def home(self) -> dict:
        """Go to the Home Screen: swipe up from the bottom edge."""
        width, height = self.screen
        return self._gesture(
            width // 2, height - 2, width // 2, int(height * 0.62), 320
        )

    def back(self) -> dict:
        """Go back: swipe in from the left edge (iOS navigation gesture)."""
        width, height = self.screen
        return self._gesture(
            2, height // 2, int(width * 0.55), height // 2, 260
        )

    def app_switch(self) -> dict:
        """Open the app switcher: slow swipe up from the bottom edge."""
        width, height = self.screen
        return self._gesture(
            width // 2, height - 2, width // 2, int(height * 0.55), 700
        )


def is_ios_phone(phone: Any) -> bool:
    return getattr(phone, "platform", "") == "ios"


def warm_pointer(phone: Any) -> dict[str, Any]:
    """Best-effort pointer reset for any phone-like object."""
    method = getattr(phone, "warm_pointer", None)
    if callable(method):
        return method()
    device = getattr(phone, "device", None)
    if device is None:
        return {"ok": False, "error": "no hand configured"}
    try:
        return {"ok": True, "response": device.request("move", timeout=1.0, x=0, y=0)}
    except DeviceError as exc:
        return {"ok": False, "error": exc.code}


def _gesture_swipe(phone: Any, x1: int, y1: int, x2: int, y2: int, ms: int) -> dict:
    warm_pointer(phone)
    return phone.swipe(x1, y1, x2, y2, ms)


def go_home(phone: Any) -> dict:
    """Go to the Home Screen (swipe up from the bottom edge)."""
    method = getattr(phone, "home", None)
    if callable(method):
        return method()
    width, height = phone.screen
    return _gesture_swipe(phone, width // 2, height - 2, width // 2, int(height * 0.62), 320)


def go_back(phone: Any) -> dict:
    """Go back (swipe in from the left edge)."""
    method = getattr(phone, "back", None)
    if callable(method):
        return method()
    width, height = phone.screen
    return _gesture_swipe(phone, 2, height // 2, int(width * 0.55), height // 2, 260)


def show_app_switcher(phone: Any) -> dict:
    """Open the app switcher (slow swipe up from the bottom edge)."""
    method = getattr(phone, "app_switch", None)
    if callable(method):
        return method()
    width, height = phone.screen
    return _gesture_swipe(phone, width // 2, height - 2, width // 2, int(height * 0.55), 700)


def hand_checks(hello: dict[str, Any], screen: tuple[int, int] | None = None) -> list[dict]:
    """Static readiness checks for an iPhone hand, derived from ``hello``."""
    checks: list[dict] = []
    hid = hello.get("hid")
    if hid == IOS_HID_MODE:
        checks.append(
            {
                "name": "hand-hid",
                "status": "pass",
                "detail": "absolute mouse firmware",
                "data": {"hid": hid},
            }
        )
    elif hid:
        checks.append(
            {
                "name": "hand-hid",
                "status": "warn",
                "detail": f"firmware hid={hid}; " + ABS_FIRMWARE_HINT.format(hid=hid),
                "data": {"hid": hid},
            }
        )
    else:
        checks.append(
            {
                "name": "hand-hid",
                "status": "warn",
                "detail": "firmware did not report a HID mode (old firmware?)",
                "data": {},
            }
        )

    reported = hello.get("screen")
    if isinstance(reported, list) and len(reported) == 2:
        reported_screen = (int(reported[0]), int(reported[1]))
        if screen and tuple(screen) != reported_screen:
            checks.append(
                {
                    "name": "hand-screen",
                    "status": "warn",
                    "detail": (
                        f"hand screen {reported_screen[0]}x{reported_screen[1]} != "
                        f"iPhone frame {screen[0]}x{screen[1]}; "
                        "run `phone ios hand --save` to align absolute coordinates"
                    ),
                    "data": {"screen": list(reported_screen), "frame": list(screen)},
                }
            )
        elif min(reported_screen) < 100:
            checks.append(
                {
                    "name": "hand-screen",
                    "status": "fail",
                    "detail": f"invalid hand screen {reported_screen}",
                    "data": {"screen": list(reported_screen)},
                }
            )
        else:
            checks.append(
                {
                    "name": "hand-screen",
                    "status": "pass",
                    "detail": f"{reported_screen[0]}x{reported_screen[1]}",
                    "data": {"screen": list(reported_screen)},
                }
            )
    return checks


def configure_hand_for_ios(
    phone: Any,
    *,
    screen: tuple[int, int] | None = None,
    cal: tuple[int, int, int, int] | None = None,
    margin: float = 0.0,
    save: bool = False,
    warmup: bool = True,
) -> dict[str, Any]:
    """Align the hand's absolute-pointer mapping with the iPhone screen.

    ``screen`` defaults to the captured iPhone frame size (or the hand's
    current value when the eye is unavailable). ``margin`` shrinks the
    calibration window to stay clear of iOS edge dead zones (0.02 = 2%).
    """
    hello = phone.hello()
    frame_size: tuple[int, int] | None = None
    source = getattr(phone, "source", None)
    size_getter = getattr(source, "frame_size", None)
    if callable(size_getter):
        frame_size = size_getter()
    elif size_getter is not None:
        frame_size = size_getter

    target_screen = screen or frame_size
    if target_screen is None:
        reported = hello.get("screen")
        if isinstance(reported, list) and len(reported) == 2:
            target_screen = (int(reported[0]), int(reported[1]))
        else:
            target_screen = tuple(phone.screen)  # type: ignore[assignment]

    if cal is None and margin > 0:
        inset = round(max(0.0, min(0.2, float(margin))) * 32767)
        cal = (inset, 32767 - inset, inset, 32767 - inset)

    warnings: list[str] = []
    if hello.get("hid") != IOS_HID_MODE:
        warnings.append(ABS_FIRMWARE_HINT.format(hid=hello.get("hid", "?")))

    response: dict[str, Any] = {}
    if target_screen or cal:
        response = phone.configure(screen=target_screen, cal=cal)
    saved = False
    if save:
        save_response = phone.save()
        saved = bool(save_response.get("ok", True))

    pointer = warm_pointer(phone) if warmup else {"ok": None, "skipped": True}
    return {
        "ok": True,
        "hello": hello,
        "screen": list(target_screen) if target_screen else None,
        "frame_size": list(frame_size) if frame_size else None,
        "cal": list(cal) if cal else list(phone.cal),
        "response": response,
        "saved": saved,
        "warnings": warnings,
        "pointer": pointer,
    }


def frame_size(frame: Frame | None) -> tuple[int, int] | None:
    return frame.size if frame is not None else None
