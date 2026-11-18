"""Bench self-tests and replay helpers."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from .device import Device, DeviceError

ACTION_KEYS = {
    "tap": ("x", "y", "hold_ms"),
    "swipe": ("x1", "y1", "x2", "y2", "ms"),
    "key": ("code",),
    "text": ("s",),
}


def grid_points(
    cols: int, rows: int, width: int, height: int, margin: float = 0.1
) -> list[tuple[int, int]]:
    if cols < 1 or rows < 1:
        raise ValueError("cols and rows must be >= 1")
    if not 0.0 <= margin < 0.5:
        raise ValueError("margin must be in [0, 0.5)")
    left, right = width * margin, width * (1.0 - margin)
    top, bottom = height * margin, height * (1.0 - margin)
    xs = [int(round(left + (right - left) * (i + 0.5) / cols)) for i in range(cols)]
    ys = [int(round(top + (bottom - top) * (i + 0.5) / rows)) for i in range(rows)]
    return [(x, y) for y in ys for x in xs]


def run_grid(
    device: Device,
    cols: int = 5,
    rows: int = 5,
    dwell: float = 0.6,
    screen: tuple[int, int] | None = None,
    output: Callable[[str], None] = print,
) -> list[tuple[int, int]]:
    width, height = screen or device.screen
    points = grid_points(cols, rows, width, height)
    total = len(points)
    output(f"grid {cols}x{rows}: {total} points on {width}x{height}")
    for index, (x, y) in enumerate(points, start=1):
        response = device.tap(x, y)
        output(f"{index:02d}/{total}  ({x},{y})  ok={response.get('ok')}  t={response.get('t')}ms")
        time.sleep(dwell)
    return points


def run_stress(
    device: Device,
    n: int = 1000,
    interval: float = 0.3,
    point: tuple[int, int] | None = None,
    output: Callable[[str], None] = print,
) -> dict:
    x, y = point or (device.screen[0] // 2, device.screen[1] // 2)
    latencies: list[float] = []
    errors: list[str] = []
    ok = 0
    started = time.monotonic()
    for i in range(1, n + 1):
        begin = time.monotonic()
        try:
            device.tap(x, y)
            ok += 1
            latencies.append((time.monotonic() - begin) * 1000.0)
        except DeviceError as exc:
            errors.append(exc.code)
        elapsed = time.monotonic() - begin
        if interval > elapsed:
            time.sleep(interval - elapsed)
        if i % 100 == 0:
            output(f"{i}/{n}  ok={ok}  fail={len(errors)}")
    latencies.sort()

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        idx = min(len(latencies) - 1, int(round((len(latencies) - 1) * p)))
        return round(latencies[idx], 2)

    result = {
        "n": n,
        "ok": ok,
        "fail": len(errors),
        "success_rate": round(ok / n, 4) if n else 0.0,
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "elapsed_s": round(time.monotonic() - started, 2),
        "errors": errors[:10],
    }
    output(json.dumps(result, ensure_ascii=False))
    return result


def replay(device: Device, path: str | Path, dry_run: bool = False) -> list[dict]:
    records: list[dict] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        item = json.loads(raw)
        cmd = item.get("cmd")
        if cmd not in ACTION_KEYS:
            continue
        params = {k: item[k] for k in ACTION_KEYS[cmd] if k in item}
        records.append({"cmd": cmd, "params": params})
        if not dry_run:
            if cmd == "tap":
                device.tap(**params)
            elif cmd == "swipe":
                device.swipe(**params)
            elif cmd == "key":
                device.key(params["code"])
            elif cmd == "text":
                device.text(params["s"])
    return records
