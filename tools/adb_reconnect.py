#!/usr/bin/env python3
"""Reconnect the phone's wireless ADB so scrcpy capture works again.

The plugin's Eye (screenshot / scrcpy) dies when wireless debugging drops.
This tool tries, in order:

1. the endpoint that worked last time (``~/.dsh/phone-service/adb-endpoint.json``)
2. endpoints advertised over mDNS (``adb mdns services``)
3. an explicit ``--endpoint``

It reports JSON so the DSH plugin can show a real reason instead of a generic
"读不到手机画面".

    tools/adb_reconnect.py --json
    tools/adb_reconnect.py --endpoint 192.168.1.20:41235
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
from pathlib import Path

from phone_console.vision.adb import AdbError, adb_connect, adb_status, resolve_adb

STATE_FILE = Path.home() / ".dsh" / "phone-service" / "adb-endpoint.json"


def load_saved_endpoint() -> str | None:
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    endpoint = payload.get("endpoint") if isinstance(payload, dict) else None
    return str(endpoint) if endpoint else None


def save_endpoint(endpoint: str) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps({"endpoint": endpoint}, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError:
        # Reconnecting is what matters; remembering it is best effort.
        pass


def mdns_endpoints(adb: str, timeout: float = 8.0) -> list[str]:
    """Parse `adb mdns services` for wireless-debugging connect endpoints."""
    try:
        proc = subprocess.run(
            [adb, "mdns", "services"], capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    found: list[str] = []
    for line in text.splitlines():
        if "_adb-tls-connect._tcp" not in line:
            continue
        # adb mdns prints: <name>  _adb-tls-connect._tcp  <ip>:<port>
        for token in line.split():
            match = re.fullmatch(r"(\S+):(\d+)", token)
            if match:
                found.append(f"{match.group(1)}:{match.group(2)}")
                break
    return found


def port_open(endpoint: str, timeout: float = 1.5) -> bool:
    """TCP probe: a reachable port with a failed adb connect means auth/pairing."""
    host, _, port = endpoint.rpartition(":")
    if not host or not port.isdigit():
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconnect the phone's wireless ADB")
    parser.add_argument("--endpoint", help="HOST:PORT of the wireless debugging port")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)

    result: dict = {"ok": False, "attempts": [], "hint": ""}
    try:
        adb = resolve_adb()
    except AdbError as exc:
        result["hint"] = str(exc)
        print(json.dumps(result, ensure_ascii=False) if args.json else result["hint"])
        return 2

    current = adb_status(adb=adb)
    if current.get("ok"):
        endpoint = current.get("serial")
        if endpoint and ":" in str(endpoint):
            save_endpoint(str(endpoint))
        result.update({"ok": True, "endpoint": endpoint, "state": current.get("state"), "status": current})
        result["hint"] = "adb 已连接，无需重连"
        print(json.dumps(result, ensure_ascii=False) if args.json else result["hint"])
        return 0

    candidates = dedupe(
        [args.endpoint or ""]
        + [load_saved_endpoint() or ""]
        + mdns_endpoints(adb)
    )
    for endpoint in candidates:
        attempt = {"endpoint": endpoint}
        try:
            connected = adb_connect(endpoint, adb=adb)
            attempt["connect"] = connected.get("message") or connected.get("output") or ""
            attempt["ok"] = bool(connected.get("ok"))
        except (AdbError, ValueError) as exc:
            attempt["ok"] = False
            attempt["error"] = str(exc)
        status = adb_status(adb=adb)
        attempt["state"] = status.get("state")
        result["attempts"].append(attempt)
        if status.get("ok"):
            save_endpoint(endpoint)
            result.update({"ok": True, "endpoint": endpoint, "state": status.get("state"), "status": status})
            result["hint"] = f"已连接 {endpoint}"
            print(json.dumps(result, ensure_ascii=False) if args.json else result["hint"])
            return 0

    status = adb_status(adb=adb)
    result["state"] = status.get("state")
    result["status"] = status
    reachable = [endpoint for endpoint in candidates if port_open(endpoint)]
    result["reachable"] = reachable
    if reachable:
        result["hint"] = (
            f"能找到无线调试端口 {reachable[-1]}，但 adb 连不上：手机可能取消了本机的调试授权/配对。"
            "在手机「无线调试 → 使用配对码配对设备」重新配对，然后执行 "
            f"phone adb pair HOST:PORT CODE && phone adb connect {reachable[-1]}"
        )
    elif candidates:
        result["hint"] = (
            "无线调试端口不可达：确认手机和电脑在同一 Wi-Fi，手机上重新打开「开发者选项 → 无线调试」；"
            "或插 USB 线"
        )
    else:
        result["hint"] = (
            "没有可用的无线调试端点：手机上打开无线调试（记下 IP:地址和端口），"
            "或插 USB 线；也可 --endpoint HOST:PORT 指定"
        )
    print(json.dumps(result, ensure_ascii=False) if args.json else result["hint"])
    return 1


if __name__ == "__main__":
    sys.exit(main())
