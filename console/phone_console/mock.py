"""A small firmware simulator used by --mock and the test suite."""
from __future__ import annotations

import time
from typing import Any

KEY_CODES = {
    "HOME",
    "BACK",
    "ENTER",
    "RETURN",
    "ESC",
    "ESCAPE",
    "TAB",
    "SPACE",
    "BACKSPACE",
    "DELETE",
    "DEL",
    "UP",
    "DOWN",
    "LEFT",
    "RIGHT",
    "MENU",
}


class MockDevice:
    def __init__(self) -> None:
        self.state = "READY"
        self.screen = (1080, 2400)
        self.cal = (0, 32767, 0, 32767)
        self.last_err = ""
        self.started = time.monotonic()
        self.calls: list[dict[str, Any]] = []
        self.taps: list[tuple[int, int]] = []
        self.moves: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int, int]] = []
        self.keys: list[str] = []
        self.texts: list[str] = []
        self.injected: list[str] = []
        self.prov = {"state": "idle", "name": "PROV-0001", "err": ""}
        self.wifi = {
            "state": "off",
            "ssid": "",
            "ip": "",
            "rssi": 0,
            "port": 9000,
            "err": "",
        }

    def _err(self, rid: Any, code: str, message: str = "") -> list[dict]:
        self.last_err = code
        out: dict[str, Any] = {"id": rid, "ok": False, "err": code}
        if message:
            out["msg"] = message
        return [out]

    def _ok(self, rid: int, elapsed: int = 0) -> list[dict]:
        self.last_err = ""
        return [{"id": rid, "ok": True, "t": elapsed}]

    def _wifi_payload(self) -> dict[str, Any]:
        payload = dict(self.wifi)
        if not payload.get("err"):
            payload.pop("err", None)
        return payload

    def _wifi_event(self, state: str) -> dict[str, Any]:
        self.wifi["state"] = state
        return {"evt": "wifi", "wifi": self._wifi_payload()}

    def inject(self, code: str, count: int = 1) -> None:
        """Return ``code`` for the next action command(s)."""
        self.injected.extend([code.upper()] * max(1, int(count)))

    def inject_busy(self, count: int = 1) -> None:
        self.inject("BUSY", count)

    def inject_fault(self) -> None:
        self.state = "FAULT"

    def clear_faults(self) -> None:
        self.injected.clear()
        self.state = "READY"

    def handle(self, msg: dict[str, Any]) -> list[dict]:
        rid = msg.get("id")
        cmd = msg.get("cmd")
        if not isinstance(rid, int) or not isinstance(cmd, str):
            return self._err(0, "BAD_JSON", "id/cmd")
        self.calls.append({"id": rid, "cmd": cmd, "msg": dict(msg)})

        if self.injected and cmd in {"tap", "swipe", "key", "text"}:
            code = self.injected.pop(0)
            if code == "FAULT":
                self.state = "FAULT"
                return self._err(rid, "NOT_READY", "injected fault")
            return self._err(rid, code, "injected")

        if cmd == "hello":
            self.state = "READY"
            self.last_err = ""
            return [
                {
                    "evt": "ready",
                    "fw": "0.3.1-mock",
                    "dev": "mock-0001",
                    "screen": list(self.screen),
                    "cal": list(self.cal),
                    "hid": "mock",
                    "wifi": self._wifi_payload(),
                    "prov": dict(self.prov),
                }
            ]
        if cmd == "ping":
            return [{"evt": "pong", "ts": msg.get("ts", 0)}]
        if cmd == "status":
            return [
                {
                    "id": rid,
                    "ok": True,
                    "state": self.state,
                    "uptime": int((time.monotonic() - self.started) * 1000),
                    "queue": 0,
                    "last_err": self.last_err,
                    "usb": True,
                    "wifi": self._wifi_payload(),
                    "prov": dict(self.prov),
                }
            ]
        if cmd == "cfg":
            screen = msg.get("screen", {})
            cal = msg.get("cal", {})
            if not isinstance(screen, dict) or not isinstance(cal, dict):
                return self._err(rid, "BAD_JSON", "cfg")
            w = int(screen.get("w", self.screen[0]))
            h = int(screen.get("h", self.screen[1]))
            x0 = int(cal.get("x0", self.cal[0]))
            x1 = int(cal.get("x1", self.cal[1]))
            y0 = int(cal.get("y0", self.cal[2]))
            y1 = int(cal.get("y1", self.cal[3]))
            if not (100 <= w <= 5000 and 100 <= h <= 5000):
                return self._err(rid, "OUT_OF_RANGE", "screen")
            if not all(0 <= v <= 32767 for v in (x0, x1, y0, y1)):
                return self._err(rid, "OUT_OF_RANGE", "cal")
            if x0 == x1 or y0 == y1:
                return self._err(rid, "OUT_OF_RANGE", "cal")
            self.screen = (w, h)
            self.cal = (x0, x1, y0, y1)
            return self._ok(rid)
        if cmd == "save":
            return self._ok(rid)
        if cmd == "wifi":
            return self._handle_wifi(rid, msg)
        if cmd == "stop":
            self.state = "READY"
            return self._ok(rid)
        if self.state == "FAULT" and cmd in {"tap", "swipe", "key", "text"}:
            return self._err(rid, "NOT_READY", "hello first")
        if cmd == "move":
            x = msg.get("x")
            y = msg.get("y")
            if not isinstance(x, int) or not isinstance(y, int):
                return self._err(rid, "BAD_JSON", "x/y")
            if not (0 <= x < self.screen[0] and 0 <= y < self.screen[1]):
                return self._err(rid, "OUT_OF_RANGE", "x/y")
            self.moves.append((x, y))
            return self._ok(rid, 2)
        if cmd == "tap":
            x = msg.get("x")
            y = msg.get("y")
            if not isinstance(x, int) or not isinstance(y, int):
                return self._err(rid, "BAD_JSON", "x/y")
            if not (0 <= x < self.screen[0] and 0 <= y < self.screen[1]):
                return self._err(rid, "OUT_OF_RANGE", "x/y")
            self.taps.append((x, y))
            return self._ok(rid, 12)
        if cmd == "swipe":
            values = [msg.get(k) for k in ("x1", "y1", "x2", "y2")]
            if not all(isinstance(v, int) for v in values):
                return self._err(rid, "BAD_JSON", "x1..y2")
            x1, y1, x2, y2 = values
            if not (
                0 <= x1 < self.screen[0]
                and 0 <= y1 < self.screen[1]
                and 0 <= x2 < self.screen[0]
                and 0 <= y2 < self.screen[1]
            ):
                return self._err(rid, "OUT_OF_RANGE", "x1..y2")
            ms = int(msg.get("ms", 400))
            self.swipes.append((x1, y1, x2, y2, ms))
            return self._ok(rid, ms + 20)
        if cmd == "key":
            code = str(msg.get("code", "")).upper()
            if code not in KEY_CODES and len(code) != 1:
                return self._err(rid, "BAD_VALUE", "code")
            self.keys.append(code)
            return self._ok(rid, 25)
        if cmd == "text":
            text = msg.get("s")
            if not isinstance(text, str) or not (1 <= len(text) <= 40):
                return self._err(rid, "OUT_OF_RANGE", "s")
            if any(not (0x20 <= ord(c) <= 0x7E) for c in text):
                return self._err(rid, "BAD_VALUE", "s")
            self.texts.append(text)
            return self._ok(rid, len(text) * 18)

        return self._err(rid, "UNKNOWN_CMD", cmd)

    def _handle_wifi(self, rid: Any, msg: dict[str, Any]) -> list[dict]:
        op = str(msg.get("op", "status"))
        if op == "prov":
            self.prov = {"state": "started", "name": "PROV-0001", "err": ""}
            return [{"id": rid, "ok": True, "t": 1}]
        if op == "status":
            return [{"id": rid, "ok": True, "wifi": self._wifi_payload()}]
        if op == "set":
            ssid = msg.get("ssid")
            password = msg.get("pass", "")
            if not isinstance(ssid, str) or not (1 <= len(ssid) <= 32):
                return self._err(rid, "BAD_VALUE", "ssid")
            if not isinstance(password, str) or len(password) > 63:
                return self._err(rid, "BAD_VALUE", "pass")
            if password and len(password) < 8:
                return self._err(rid, "BAD_VALUE", "pass")
            port = msg.get("port", self.wifi.get("port", 9000))
            if not isinstance(port, int) or not (1 <= port <= 65535):
                return self._err(rid, "BAD_VALUE", "port")
            self.wifi.update(
                {
                    "ssid": ssid,
                    "port": port,
                    "ip": "192.168.1.50",
                    "rssi": -40,
                    "err": "",
                }
            )
            return [
                self._wifi_event("connecting"),
                {"id": rid, "ok": True, "t": 1},
                self._wifi_event("connected"),
            ]
        if op == "clear":
            self.wifi.update(
                {"ssid": "", "ip": "", "rssi": 0, "err": ""}
            )
            return [self._wifi_event("off"), {"id": rid, "ok": True, "t": 0}]
        return self._err(rid, "BAD_VALUE", "op")
