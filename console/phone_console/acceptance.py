"""Hardware acceptance checks for The Hand (phase 1) with a JSON/HTML report."""
from __future__ import annotations

import html
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .device import DeviceError
from .phone import Phone


@dataclass
class AcceptanceItem:
    id: str
    name: str
    status: str  # pass / fail / warn / skip / manual
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "data": self.data,
        }


@dataclass
class AcceptanceReport:
    mode: str
    quick: bool
    items: list[AcceptanceItem]
    environment: dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def ok(self) -> bool:
        return not any(item.status == "fail" for item in self.items)

    def counts(self) -> dict[str, int]:
        counts = {"pass": 0, "fail": 0, "warn": 0, "skip": 0, "manual": 0}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "quick": self.quick,
            "started_at": self.started_at,
            "environment": self.environment,
            "summary": self.counts(),
            "items": [item.to_dict() for item in self.items],
        }

    def to_text(self) -> str:
        lines = [
            f"phone acceptance ({self.mode}, {'quick' if self.quick else 'full'})"
        ]
        for item in self.items:
            lines.append(f"[{item.status.upper():6s}] {item.id} {item.name}: {item.detail}")
        counts = self.counts()
        lines.append(
            "summary: "
            f"{counts['pass']} pass, {counts['fail']} fail, {counts['warn']} warn, "
            f"{counts['manual']} manual, {counts['skip']} skip"
        )
        return "\n".join(lines)

    def to_html(self) -> str:
        colors = {
            "pass": "#0f7b3f",
            "fail": "#b42318",
            "warn": "#9a6700",
            "skip": "#667085",
            "manual": "#175cd3",
        }
        rows = []
        for item in self.items:
            color = colors.get(item.status, "#344054")
            rows.append(
                "<tr>"
                f"<td>{html.escape(item.id)}</td>"
                f"<td>{html.escape(item.name)}</td>"
                f'<td><span class="badge" style="background:{color}">'
                f"{html.escape(item.status.upper())}</span></td>"
                f"<td>{html.escape(item.detail)}</td>"
                "</tr>"
            )
        counts = self.counts()
        env = "<br>".join(
            f"{html.escape(str(key))}: {html.escape(str(value))}"
            for key, value in self.environment.items()
        )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Phone acceptance {html.escape(self.started_at)}</title>
<style>
body {{ font: 14px -apple-system, "PingFang SC", sans-serif; color:#101828; margin:32px; }}
h1 {{ font-size:22px; }}
table {{ border-collapse:collapse; width:100%; margin-top:16px; }}
th, td {{ border-bottom:1px solid #e4e7ec; padding:8px; text-align:left; vertical-align:top; }}
th {{ background:#f9fafb; }}
.badge {{ color:#fff; border-radius:10px; padding:2px 8px; font-size:11px; font-weight:600; }}
.summary {{ margin-top:16px; font-weight:600; }}
.env {{ color:#475467; margin-top:8px; font-size:12px; line-height:1.7; }}
</style></head><body>
<h1>Phone acceptance report</h1>
<div class="env">{env}</div>
<table><thead><tr><th>ID</th><th>Check</th><th>Status</th><th>Detail</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<div class="summary">Summary: {counts['pass']} pass, {counts['fail']} fail,
{counts['warn']} warn, {counts['manual']} manual, {counts['skip']} skip</div>
</body></html>"""


def write_report(report: AcceptanceReport, directory: str | Path) -> tuple[Path, Path]:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "report.json"
    html_path = target / "report.html"
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    html_path.write_text(report.to_html(), encoding="utf-8")
    return json_path, html_path


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95)))
    return ordered[index]


def _grid_points(cols: int, rows: int, width: int, height: int, margin: float = 0.1):
    left, right = width * margin, width * (1.0 - margin)
    top, bottom = height * margin, height * (1.0 - margin)
    xs = [int(round(left + (right - left) * (i + 0.5) / cols)) for i in range(cols)]
    ys = [int(round(top + (bottom - top) * (i + 0.5) / rows)) for i in range(rows)]
    return [(x, y) for y in ys for x in xs]


def _corner_points(width: int, height: int, margin: float = 0.05):
    xs = [int(round(width * margin)), int(round(width * (1.0 - margin)))]
    ys = [int(round(height * margin)), int(round(height * (1.0 - margin)))]
    return [(x, y) for y in ys for x in xs] + [(width // 2, height // 2)]


def run_acceptance(
    phone: Phone,
    mode: str,
    *,
    quick: bool = True,
    manual: bool = False,
    stress_n: int | None = None,
    stress_interval: float | None = None,
    long_hours: float = 0.0,
    output: Callable[[str], None] | None = print,
    prompt: Callable[[str], str] = input,
    sleep: Callable[[float], None] = time.sleep,
) -> AcceptanceReport:
    if stress_n is None:
        stress_n = 50 if quick else 1000
    if stress_interval is None:
        stress_interval = 0.0 if quick else 0.3
    gap = 0.0 if mode == "mock" else 0.1

    items: list[AcceptanceItem] = []
    environment: dict[str, Any] = {
        "phone_console": __version__,
        "quick": quick,
        "stress_n": stress_n,
        "stress_interval_s": stress_interval,
        "long_hours": long_hours,
        "manual": manual,
    }

    def emit(message: str) -> None:
        if output is not None:
            output(message)

    def add(
        item_id: str,
        name: str,
        status: str,
        detail: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        items.append(
            AcceptanceItem(item_id, name, status, detail, data or {})
        )
        emit(f"[{status.upper():6s}] {item_id} {name}: {detail}")

    def act(fn, wait: bool = True):
        """Run one HID action, respecting the firmware's 80ms rate limit."""
        for attempt in range(3):
            try:
                result = fn()
                if gap and wait:
                    sleep(gap)
                return result
            except DeviceError as exc:
                if exc.code == "RATE_LIMIT" and attempt < 2:
                    sleep(max(gap, 0.12))
                    continue
                raise
        raise RuntimeError("action retries exhausted")

    def manual_check(
        item_id: str, name: str, detail: str, question: str
    ) -> None:
        if mode == "mock":
            add(item_id, name, "skip", "mock phone")
            return
        if not manual:
            add(item_id, name, "manual", "not checked; rerun with --manual")
            return
        answer = prompt(f"{question} [y/N]: ").strip().lower()
        if answer in {"y", "yes"}:
            add(item_id, name, "pass", "operator confirmed")
        else:
            add(item_id, name, "fail", "operator reported failure")

    try:
        hello = phone.hello()
        add(
            "T3",
            "handshake",
            "pass",
            f"fw={hello.get('fw', '?')} dev={hello.get('dev', '?')}",
            hello,
        )
        environment.update(
            {
                "firmware": hello.get("fw"),
                "device": hello.get("dev"),
                "hid": hello.get("hid"),
                "screen": hello.get("screen"),
                "cal": hello.get("cal"),
            }
        )
    except Exception as exc:  # noqa: BLE001 - acceptance report
        add("T3", "handshake", "fail", str(exc))
        report = AcceptanceReport(mode, quick, items, environment)
        emit(report.to_text())
        return report

    width, height = phone.screen
    try:
        status = phone.status()
        usb = status.get("usb")
        add(
            "T2",
            "USB enumeration",
            "pass" if usb else "warn",
            f"state={status.get('state')} usb={usb}",
            status,
        )
    except Exception as exc:  # noqa: BLE001 - acceptance report
        add("T2", "USB enumeration", "warn", str(exc))
    manual_check(
        "T2m", "pointer visible", "confirm the phone shows a pointer", "Pointer visible"
    )

    # T4: corners + center, ACK only; physical hit is a manual check.
    points = _corner_points(width, height)
    failures = 0
    for x, y in points:
        try:
            act(lambda x=x, y=y: phone.tap(x, y))
        except Exception:  # noqa: BLE001 - count failures
            failures += 1
    add(
        "T4",
        "corner/center ACK",
        "pass" if failures == 0 else "fail",
        f"{len(points) - failures}/{len(points)} taps accepted",
        {"points": points, "failures": failures},
    )
    manual_check(
        "T4m", "corner/center accuracy", "confirm each tap landed within 3px", "All 5 points accurate"
    )

    # T5: 5x5 grid ACK.
    grid = _grid_points(5, 5, width, height)
    failures = 0
    for x, y in grid:
        try:
            act(lambda x=x, y=y: phone.tap(x, y))
        except Exception:  # noqa: BLE001 - count failures
            failures += 1
    add(
        "T5",
        "5x5 grid ACK",
        "pass" if failures == 0 else "fail",
        f"{len(grid) - failures}/{len(grid)} taps accepted",
        {"points": len(grid), "failures": failures},
    )
    manual_check(
        "T5m", "grid hit rate", "confirm every point landed in its target cell", "25/25 grid cells hit"
    )

    # T6: swipe durations.
    swipe_failures = 0
    swipe_data: list[dict[str, Any]] = []
    for ms in (200, 500, 1000):
        for _ in range(2):
            try:
                response = act(
                    lambda ms=ms: phone.swipe(
                        width // 2,
                        int(height * 0.75),
                        width // 2,
                        int(height * 0.25),
                        ms,
                    )
                )
                action_ms = response.get("t")
                if isinstance(action_ms, (int, float)) and abs(action_ms - ms) > max(250, ms * 0.10):
                    swipe_failures += 1
                swipe_data.append({"ms": ms, "t": action_ms})
            except Exception as exc:  # noqa: BLE001 - count failures
                swipe_failures += 1
                swipe_data.append({"ms": ms, "error": str(exc)})
    add(
        "T6",
        "swipe timing ACK",
        "pass" if swipe_failures == 0 else "fail",
        f"{swipe_failures} timing/ACK failure(s)",
        {"samples": swipe_data},
    )
    manual_check(
        "T6m", "swipe continuity", "confirm no broken touch for 200/500/1000ms", "All swipes continuous"
    )

    # T7: keys.
    key_failures = 0
    for code in ("HOME", "BACK", "ENTER", "UP"):
        try:
            act(lambda code=code: phone.key(code))
        except Exception:  # noqa: BLE001 - count failures
            key_failures += 1
    add(
        "T7",
        "key ACK",
        "pass" if key_failures == 0 else "fail",
        f"{key_failures} key failure(s)",
        {"codes": ["HOME", "BACK", "ENTER", "UP"]},
    )
    manual_check(
        "T7m", "key effect", "confirm HOME/BACK/ENTER/UP did the right thing", "All keys correct"
    )

    # T8: text.
    try:
        act(lambda: phone.text("Abc123"))
        add("T8", "ASCII text ACK", "pass", "Abc123 accepted")
    except Exception as exc:  # noqa: BLE001 - acceptance report
        add("T8", "ASCII text ACK", "fail", str(exc))
    manual_check("T8m", "text effect", "confirm Abc123 typed without loss/reorder", "Abc123 correct")

    # T9: stop.
    try:
        phone.stop()
        add("T9", "stop ACK", "pass", "stop accepted")
    except Exception as exc:  # noqa: BLE001 - acceptance report
        add("T9", "stop ACK", "fail", str(exc))
    manual_check("T9m", "no residual press", "confirm no button remains pressed", "No residual press")

    # T10: stress.
    latencies: list[float] = []
    errors: list[str] = []
    started = time.monotonic()
    for _ in range(int(stress_n)):
        begin = time.monotonic()
        try:
            act(lambda: phone.tap(width // 2, height // 2), wait=False)
            latencies.append((time.monotonic() - begin) * 1000.0)
        except Exception as exc:  # noqa: BLE001 - count failures
            errors.append(str(exc))
        if gap:
            sleep(gap)
        if stress_interval:
            sleep(float(stress_interval))
    p95 = round(_p95(latencies), 2)
    stress_ok = not errors and (quick or p95 <= 50.0)
    add(
        "T10",
        f"stress {stress_n} taps",
        "pass" if stress_ok else "fail",
        f"ok={len(latencies)} fail={len(errors)} p95={p95}ms",
        {
            "n": int(stress_n),
            "ok": len(latencies),
            "fail": len(errors),
            "p95_ms": p95,
            "elapsed_s": round(time.monotonic() - started, 2),
        },
    )

    # T11: optional long run.
    if long_hours and long_hours > 0:
        deadline = time.monotonic() + float(long_hours) * 3600.0
        long_errors = 0
        pings = 0
        while time.monotonic() < deadline:
            try:
                phone.ping()
                pings += 1
            except Exception:  # noqa: BLE001 - count failures
                long_errors += 1
            sleep(min(5.0, max(0.0, deadline - time.monotonic())))
        add(
            "T11",
            f"long run {long_hours}h",
            "pass" if long_errors == 0 else "fail",
            f"pings={pings} errors={long_errors}",
            {"pings": pings, "errors": long_errors},
        )
    else:
        add("T11", "8h long run", "skip", "use --long-hours 8 to run")

    # Physical checks that cannot be automated.
    manual_check("T1", "power/backfeed", "measure VBUS/temperature with both ports", "Power is safe")
    manual_check("T12", "replug/reboot", "50 replugs; phone reboot/lock", "At least 49/50 enumerated")
    manual_check(
        "T13", "Android 16 compatibility", "target phone model/version", "Target device works"
    )

    report = AcceptanceReport(mode, quick, items, environment)
    emit(report.to_text())
    return report
