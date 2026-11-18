"""Task templates: a markdown file that both humans and the runner can use.

A template is one ``.md`` file:

* YAML frontmatter -> identity, calibration screen, coordinate freshness
* prose            -> intent, criteria, strategy (for humans and the model)
* a ```yaml steps`` block under ``## 步骤`` -> deterministic steps (no LLM)

Compiling turns the steps block into a scenario the existing runner executes.
Coordinates are normalized 0-1 against ``screen``, so another resolution only
changes the frontmatter. A template declares whether its coordinates are known
and when they were last verified; past ``coords_ttl_days`` they are not used.

Traces are one JSON object per line and are aggregated into a review that
proposes template edits (promote a text step to a coordinate step, retune a
timeout, demote a coordinate that keeps failing).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
STEPS_BLOCK = re.compile(r"^##\s*步骤\s*$(.*?)^```yaml\s*\n(.*?)^```", re.S | re.M)

ACTIONS = {"tap", "tap_text", "expect", "key", "swipe", "sleep", "text"}
REQUIRED = {
    "tap": ("at",),
    "tap_text": ("text",),
    "expect": ("text",),
    "key": ("code",),
    "swipe": ("from", "to"),
    "sleep": ("ms",),
    "text": ("value",),
}
ACTION_TO_DO = {
    "tap": "tap",
    "tap_text": "tap_text",
    "assert_text": "expect",
    "wait_for_text": "expect",
    "key": "key",
    "swipe": "swipe",
    "sleep": "sleep",
    "text": "text",
}
ENTRY_ACTIONS = {"tap_text", "expect", "key", "sleep", "text"}
ENTRY_DEFAULT_TIMEOUT = 15.0
DEFAULT_TTL_DAYS = 30


class TemplateError(ValueError):
    """Template file is malformed or cannot be compiled."""


@dataclass
class Template:
    path: Path
    meta: dict[str, Any]
    steps: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.meta.get("id") or self.path.stem)

    @property
    def version(self) -> int:
        try:
            return int(self.meta.get("version", 1))
        except (TypeError, ValueError):
            return 1

    @property
    def screen(self) -> tuple[int, int]:
        value = self.meta.get("screen") or (1080, 2400)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise TemplateError("screen must be [width, height]")
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError) as exc:
            raise TemplateError("screen must be [width, height]") from exc

    def step(self, step_id: str) -> dict[str, Any] | None:
        for step in self.steps:
            if str(step.get("id")) == step_id:
                return step
        return None


# ---------------------------------------------------------------- parsing
def parse_template(path: Path) -> Template:
    text = path.read_text(encoding="utf-8")

    match = FRONTMATTER.match(text)
    if not match:
        raise TemplateError("missing YAML frontmatter (--- ... ---)")
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        raise TemplateError("frontmatter must be a mapping")

    block = STEPS_BLOCK.search(text)
    if not block:
        raise TemplateError('missing "## 步骤" section with a ```yaml steps block')
    parsed = yaml.safe_load(block.group(2)) or {}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("steps"), list):
        raise TemplateError("steps block must be a mapping with a 'steps' list")

    steps = parsed["steps"]
    if not steps:
        raise TemplateError("steps must not be empty")
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise TemplateError(f"step {index} must be a mapping")
        step.setdefault("id", f"step-{index}")

    template = Template(path=path, meta=meta, steps=steps)
    validate_steps(template)
    return template


def _frac(value: Any, what: str, where: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TemplateError(f"{where}: {what} must be a number")
    if not 0.0 <= float(value) <= 1.0:
        raise TemplateError(f"{where}: {what} must be within 0..1 (normalized)")
    return float(value)


def _pair(value: Any, what: str, where: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise TemplateError(f"{where}: {what} must be [x, y]")
    return [_frac(value[0], f"{what}[0]", where), _frac(value[1], f"{what}[1]", where)]


def _quad(value: Any, what: str, where: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise TemplateError(f"{where}: {what} must be [x, y, w, h]")
    return [_frac(item, f"{what}[{i}]", where) for i, item in enumerate(value)]


def parse_entry(meta: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the optional `entry` block: confirm the starting page first.

    A phone's state before a task is arbitrary, so coordinates only make sense
    after the flow has been proven to be on the expected page. `approach` steps
    may only use text/keys (never coordinates).
    """
    raw = meta.get("entry")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TemplateError("entry must be a mapping (expect / timeout / approach)")
    expect = str(raw.get("expect") or "").strip()
    if not expect:
        raise TemplateError("entry.expect is required (the anchor text of the start page)")
    timeout = raw.get("timeout", ENTRY_DEFAULT_TIMEOUT)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < float(timeout) <= 120:
        raise TemplateError("entry.timeout must be 0..120 seconds")
    approach = raw.get("approach") or []
    if not isinstance(approach, list):
        raise TemplateError("entry.approach must be a list of steps")
    compiled = []
    for index, step in enumerate(approach, 1):
        where = f"entry.approach[{index}]"
        if not isinstance(step, dict):
            raise TemplateError(f"{where} must be a mapping")
        action = step.get("do")
        if action not in ENTRY_ACTIONS:
            raise TemplateError(
                f"{where}: do={action!r} is not allowed before the page anchor; "
                f"expected one of {sorted(ENTRY_ACTIONS)}"
            )
        for name in REQUIRED[action]:
            if name not in step:
                raise TemplateError(f"{where}: {action} needs '{name}'")
        compiled.append(dict(step))
    return {"expect": expect, "timeout": float(timeout), "approach": compiled}


_LABEL_BAD = set("，。、；;：:,.!?！？\n\r\t")
_EXPECT_AFTER_ACTIONS = {"tap", "tap_text", "swipe", "key"}


def short_expect_after(value: Any) -> str | None:
    """`expect_after` is compiled to an assert only when it is a short on-screen label."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 16 or any(char in _LABEL_BAD for char in text):
        return None
    return text


def validate_steps(template: Template) -> None:
    parse_entry(template.meta)
    seen: set[str] = set()
    for step in template.steps:
        sid = str(step["id"])
        where = f"step '{sid}'"
        if sid in seen:
            raise TemplateError(f"duplicate step id '{sid}'")
        seen.add(sid)

        action = step.get("do")
        if action not in ACTIONS:
            raise TemplateError(
                f"{where}: unknown do={action!r}; expected one of {sorted(ACTIONS)}"
            )
        for name in REQUIRED[action]:
            if name not in step:
                raise TemplateError(f"{where}: {action} needs '{name}'")

        if action == "tap":
            _pair(step["at"], "at", where)
        elif action == "swipe":
            _pair(step["from"], "from", where)
            _pair(step["to"], "to", where)
        elif action == "sleep":
            ms = step["ms"]
            if not isinstance(ms, int) or isinstance(ms, bool) or not 0 <= ms <= 60000:
                raise TemplateError(f"{where}: ms must be an integer 0..60000")
        elif action == "expect":
            timeout = step.get("timeout", 3)
            if not isinstance(timeout, (int, float)) or not 0 < float(timeout) <= 120:
                raise TemplateError(f"{where}: timeout must be 0..120 seconds")

        if "roi" in step:
            if action not in {"expect", "tap_text", "tap"}:
                raise TemplateError(f"{where}: roi is only supported on expect/tap/tap_text")
            _quad(step["roi"], "roi", where)


# ------------------------------------------------------ coordinate freshness
def coord_state(meta: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Whether the template's coordinates may be used, and why not."""
    today = today or date.today()
    known = bool(meta.get("coords_known", False))
    raw_updated = meta.get("coords_updated")
    try:
        ttl = int(meta.get("coords_ttl_days", DEFAULT_TTL_DAYS))
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_DAYS

    updated: date | None = None
    if raw_updated in (None, "", "null"):
        updated = None
    elif isinstance(raw_updated, date) and not isinstance(raw_updated, datetime):
        updated = raw_updated
    elif isinstance(raw_updated, datetime):
        updated = raw_updated.date()
    else:
        try:
            updated = date.fromisoformat(str(raw_updated))
        except ValueError as exc:
            raise TemplateError(f"coords_updated must be YYYY-MM-DD, got {raw_updated!r}") from exc

    age = (today - updated).days if updated else None
    stale = known and (updated is None or age is None or age > ttl)

    if not known:
        reason = "coords_known=false（未在真机验证坐标）"
    elif updated is None:
        reason = "coords_updated 缺失"
    elif stale:
        reason = f"坐标已 {age} 天未更新（阈值 {ttl} 天）"
    else:
        reason = f"坐标 {age} 天前更新（阈值 {ttl} 天）"

    return {
        "known": known,
        "updated": updated.isoformat() if updated else None,
        "age_days": age,
        "ttl_days": ttl,
        "stale": stale,
        "usable": known and not stale,
        "reason": reason,
    }


# ------------------------------------------------------------- compilation
def px(value: float, size: int) -> int:
    return max(0, min(size - 1, int(round(value * size))))


def _roi_px(roi: list[float], screen: tuple[int, int], where: str) -> list[int]:
    x, y, w, h = _quad(roi, "roi", where)
    width, height = screen
    left = px(x, width)
    top = px(y, height)
    right = min(width, int(round((x + w) * width)))
    bottom = min(height, int(round((y + h) * height)))
    return [left, top, max(1, right - left), max(1, bottom - top)]


def compile_scenario(
    template: Template,
    *,
    screen: tuple[int, int] | None = None,
    stop_before: str | None = None,
    force_coords: bool = False,
    include_entry: bool = True,
    entry_only: bool = False,
) -> dict[str, Any]:
    """Compile the steps block. Taps degrade to text taps when coords are unusable.

    ``entry_only`` compiles just the approach + anchor gate (used to check whether
    the phone can reach the start page); ``include_entry=False`` compiles the main
    path only (used after the gate has already been reached).
    """
    screen = screen or template.screen
    width, height = screen
    state = coord_state(template.meta)
    use_coords = force_coords or state["usable"]

    warnings: list[str] = []
    if not use_coords:
        warnings.append(f"{state['reason']}；tap 步骤降级为 tap_text")

    steps: list[dict[str, Any]] = []

    # Reach the confirmed page first: text/keys only, then assert the anchor.
    # Coordinate steps only run after this gate passes.
    entry = parse_entry(template.meta)
    if entry_only and not entry:
        raise TemplateError("模板没有 entry，无法只执行到达段")
    if entry_only:
        include_entry = True
    if entry and include_entry:
        for index, step in enumerate(entry["approach"], 1):
            sid = f"entry-{index}"
            action = step["do"]
            if action == "tap_text":
                item: dict[str, Any] = {"action": "tap_text", "text": str(step["text"])}
            elif action == "expect":
                item = {
                    "action": "assert_text",
                    "text": str(step["text"]),
                    "timeout": float(step.get("timeout", 3)),
                }
            elif action == "key":
                item = {"action": "key", "code": str(step["code"])}
            elif action == "sleep":
                item = {"action": "sleep", "ms": int(step["ms"])}
            else:
                item = {"action": "text", "value": str(step["value"])}
            item["id"] = sid
            steps.append(item)
        steps.append(
            {
                "action": "assert_text",
                "text": entry["expect"],
                "timeout": entry["timeout"],
                "id": "entry",
            }
        )
        warnings.append(f"entry: 先确认起始页「{entry['expect']}」，再执行坐标步骤")

    for step in ([] if entry_only else template.steps):
        sid = str(step["id"])
        if stop_before and sid == stop_before:
            break
        action = step["do"]

        if action == "tap":
            x, y = _pair(step["at"], "at", sid)
            if use_coords:
                item: dict[str, Any] = {"action": "tap", "x": px(x, width), "y": px(y, height)}
                if "hold_ms" in step:
                    item["hold_ms"] = int(step["hold_ms"])
            else:
                if not step.get("text"):
                    raise TemplateError(
                        f"step '{sid}': 坐标不可用（{state['reason']}），"
                        "请给该步加 text 兜底，或用 --force-coords"
                    )
                item = {"action": "tap_text", "text": str(step["text"]), "coord_hint": [x, y]}
                if "roi" in step:
                    item["roi"] = _roi_px(step["roi"], screen, sid)
                warnings.append(f"{sid}: tap -> tap_text({step['text']!r})")
        elif action == "tap_text":
            item = {"action": "tap_text", "text": str(step["text"])}
            if "roi" in step:
                item["roi"] = _roi_px(step["roi"], screen, sid)
        elif action == "expect":
            item = {
                "action": "assert_text",
                "text": str(step["text"]),
                "timeout": float(step.get("timeout", 3)),
            }
            if "roi" in step:
                item["roi"] = _roi_px(step["roi"], screen, sid)
        elif action == "key":
            item = {"action": "key", "code": str(step["code"])}
        elif action == "swipe":
            x1, y1 = _pair(step["from"], "from", sid)
            x2, y2 = _pair(step["to"], "to", sid)
            item = {
                "action": "swipe",
                "x1": px(x1, width),
                "y1": px(y1, height),
                "x2": px(x2, width),
                "y2": px(y2, height),
                "ms": int(step.get("ms", 400)),
            }
        elif action == "sleep":
            item = {"action": "sleep", "ms": int(step["ms"])}
        else:  # text
            item = {"action": "text", "value": str(step["value"])}
        item["id"] = sid
        steps.append(item)

        # A step's expected evidence becomes a real check: otherwise a drifted
        # page is only noticed several blind taps later.
        if action in _EXPECT_AFTER_ACTIONS:
            expectation = short_expect_after(step.get("expect_after"))
            if expectation:
                steps.append(
                    {
                        "action": "assert_text",
                        "text": expectation,
                        "timeout": float(step.get("check_timeout", 3)),
                        "id": f"{sid}-check",
                    }
                )
            elif step.get("expect_after"):
                warnings.append(
                    f"{sid}: expect_after 不是短文本（需 ≤16 字且无标点），未编译成校验"
                )

    if not steps:
        raise TemplateError(f"--stop-before {stop_before!r} left no steps")

    scenario: dict[str, Any] = {
        "name": template.id,
        "description": f"{template.meta.get('name', template.id)} (v{template.version})",
        "screen": [width, height],
        "steps": steps,
    }
    if template.meta.get("frames"):
        frames = Path(str(template.meta["frames"]))
        scenario["frames"] = str((template.path.parent / frames).resolve())

    template.warnings = warnings
    return scenario


# ------------------------------------------------------------------ traces
def report_to_trace(report_path: Path, header: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert the runner's report.jsonl into our trace.jsonl records."""
    records: list[dict[str, Any]] = [{"t": "run", **header}]
    warning_count = 0
    for raw in report_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = entry.get("type")
        if kind == "step":
            params = entry.get("params") or {}
            detail = entry.get("detail") or {}
            action = entry.get("action") or ""
            record: dict[str, Any] = {
                "t": "step",
                "i": entry.get("index"),
                "id": params.get("id") or f"step-{entry.get('index')}",
                "do": ACTION_TO_DO.get(action, action),
                "ok": entry.get("status") == "ok",
                "att": entry.get("attempts", 1),
                "ms": round(float(entry.get("elapsed_ms") or 0.0), 1),
            }
            if "text" in params:
                record["text"] = params["text"]
            if action == "tap":
                record["at"] = [params.get("x"), params.get("y")]
            center = detail.get("center")
            if isinstance(center, (list, tuple)) and len(center) == 2:
                record["center"] = [round(float(center[0]), 1), round(float(center[1]), 1)]
            if detail.get("ocr_ms") is not None:
                record["ocr_ms"] = round(float(detail["ocr_ms"]), 1)
            if entry.get("error"):
                record["err"] = entry["error"]
            records.append(record)
        elif kind == "summary":
            records.append(
                {
                    "t": "done",
                    "ok": bool(entry.get("ok")),
                    "steps": entry.get("steps"),
                    "fail": entry.get("failed"),
                    "ms": round(float(entry.get("elapsed_ms") or 0.0), 1),
                }
            )
        elif kind == "error":
            warning_count += 1
    if warning_count:
        records.append({"t": "note", "errors": warning_count})
    return records


def write_trace(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read_trace(path: Path) -> dict[str, Any]:
    header: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []
    done: dict[str, Any] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = record.get("t")
        if kind == "run":
            header = record
        elif kind == "step":
            steps.append(record)
        elif kind == "done":
            done = record
    return {"path": path, "header": header, "steps": steps, "done": done}


def collect_traces(root: Path, limit: int = 10) -> list[dict[str, Any]]:
    paths = sorted(root.glob("*/trace.jsonl"), reverse=True)[: max(1, limit)]
    return [read_trace(path) for path in reversed(paths)]


# ------------------------------------------------------------------ review
def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def summarise_runs(traces: list[dict[str, Any]]) -> dict[str, Any]:
    runs = len(traces)
    ok_runs = sum(1 for t in traces if t["done"].get("ok"))
    durations = [float(t["done"].get("ms") or 0.0) for t in traces if t["done"]]
    steps: dict[str, list[dict[str, Any]]] = {}
    for trace in traces:
        for step in trace["steps"]:
            steps.setdefault(str(step.get("id")), []).append(step)
    return {
        "runs": runs,
        "ok_runs": ok_runs,
        "durations": durations,
        "steps": steps,
    }


def yaml_step(step_id: str, do: str, **fields: Any) -> str:
    """One-line yaml step, only the fields that matter."""
    parts = [f"id: {step_id}", f"do: {do}"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            rendered = "[" + ", ".join(f"{v:.4f}" if isinstance(v, float) else str(v) for v in value) + "]"
        else:
            rendered = str(value)
        parts.append(f"{key}: {rendered}")
    return "- {" + ", ".join(parts) + "}"


def _normalised_centers(records: list[dict[str, Any]], screen: tuple[int, int]) -> list[tuple[float, float]]:
    width, height = screen
    points = []
    for record in records:
        center = record.get("center")
        if isinstance(center, (list, tuple)) and len(center) == 2 and width and height:
            points.append((float(center[0]) / width, float(center[1]) / height))
    return points


YAML_SCALAR_SAFE = re.compile(r"^[\w\u4e00-\u9fa5·./ -]+$")


def yaml_scalar(value: Any) -> str:
    """Quote a scalar only when YAML would misread it."""
    text = str(value)
    return text if YAML_SCALAR_SAFE.match(text) else json.dumps(text, ensure_ascii=False)


def render_step_line(step: dict[str, Any]) -> str:
    """One-line flow mapping for the steps block, preserving extra keys."""
    parts: list[str] = []
    for key, value in step.items():
        if key in {"id", "do"}:
            parts.append(f"{key}: {value}")
            continue
        if isinstance(value, (list, tuple)):
            items = []
            for item in value:
                if isinstance(item, bool):
                    items.append("true" if item else "false")
                elif isinstance(item, (int, float)):
                    items.append(f"{float(item):.4f}")
                else:
                    items.append(yaml_scalar(item))
            rendered = "[" + ", ".join(items) + "]"
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        else:
            rendered = yaml_scalar(value)
        parts.append(f"{key}: {rendered}")
    return "  - {" + ", ".join(parts) + "}"


def analyse_template(
    template: Template,
    traces: list[dict[str, Any]],
    today: date | None = None,
) -> dict[str, Any]:
    """Turn traces into machine-readable proposals.

    Proposal shape (also written to review.json):

        {"id": "s3", "uid": "s3:promote_coords", "kind": "promote_coords",
         "step_id": "s3", "action": "tap",
         "value": {"do": "tap", "at": [0.5, 0.5]},
         "reason": "...", "evidence": {...}, "confidence": "ok"|"low",
         "auto": false}
    """
    stats = summarise_runs(traces)
    screen = template.screen
    state = coord_state(template.meta, today)
    runs, ok_runs = stats["runs"], stats["ok_runs"]
    durations = stats["durations"]

    rows: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []

    for step_id, records in stats["steps"].items():
        ok = sum(1 for r in records if r.get("ok"))
        attempts = [float(r.get("att") or 1) for r in records]
        times = [float(r.get("ms") or 0.0) for r in records]
        do = records[-1].get("do", "?")
        points = _normalised_centers(records, screen)
        note: list[str] = []
        if points:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            spread = max(max(xs) - min(xs), max(ys) - min(ys))
            note.append(f"命中 ({_median(xs):.4f}, {_median(ys):.4f}) 离散 ±{spread * 100:.2f}%")
        if records[0].get("err"):
            note.append(f"最近错误：{records[0]['err'][:40]}")

        rows.append(
            {
                "id": step_id,
                "do": do,
                "ok": ok,
                "runs": len(records),
                "attempts": round(sum(attempts) / len(attempts), 2),
                "p95_ms": round(_p95(times), 1),
                "note": "；".join(note),
            }
        )

        source = template.step(step_id)
        if not source:
            continue
        evidence = {"runs": len(records), "ok": ok, "p95_ms": round(_p95(times), 1)}

        def propose(kind: str, action: str, value: dict[str, Any], reason: str, **extra: Any) -> None:
            proposals.append(
                {
                    "id": step_id,
                    "uid": f"{step_id}:{kind}",
                    "kind": kind,
                    "step_id": step_id,
                    "action": action,
                    "value": value,
                    "reason": reason,
                    "evidence": evidence,
                    **extra,
                }
            )

        # promote: a text tap that lands on the same spot can become a coordinate tap
        if do == "tap_text" and ok == len(records) and points:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            spread = max(max(xs) - min(xs), max(ys) - min(ys))
            if spread <= 0.01:
                propose(
                    "promote_coords",
                    "tap",
                    {
                        "do": "tap",
                        "at": [_median(xs), _median(ys)],
                        "text": source.get("text"),
                    },
                    "连续命中同一位置，可升级为坐标点击",
                    confidence="ok" if len(points) >= 3 else "low",
                )

        # demote: a coordinate that fails should fall back to text
        if do == "tap" and ok < len(records) and source.get("text"):
            propose(
                "demote_coords",
                "tap_text",
                {"do": "tap_text", "text": source["text"]},
                f"{ok}/{len(records)} 成功，坐标可能已漂移",
                confidence="ok",
            )

        # expect: repeated retries mean the ROI is too tight or the timeout too short
        if do == "expect" and len(records) >= 2:
            avg_attempts = sum(attempts) / len(attempts)
            if avg_attempts > 1.5 and source.get("roi"):
                x, y, w, h = [float(v) for v in source["roi"]]
                pad_x, pad_y = w * 0.2, h * 0.2
                roi = [
                    max(0.0, x - pad_x),
                    max(0.0, y - pad_y),
                    min(1.0 - max(0.0, x - pad_x), w + pad_x * 2),
                    min(1.0 - max(0.0, y - pad_y), h + pad_y * 2),
                ]
                propose(
                    "widen_roi",
                    "set_roi",
                    {"roi": [round(v, 4) for v in roi]},
                    f"平均要 {avg_attempts:.1f} 次才找到，校验区域可能太窄",
                    confidence="ok",
                )
            timeout = source.get("timeout")
            if isinstance(timeout, (int, float)) and timeout:
                p95 = _p95(times)
                budget_ms = float(timeout) * 1000
                if p95 > 0.7 * budget_ms:
                    propose(
                        "timeout_raise",
                        "set_timeout",
                        {"timeout": round(float(timeout) * 1.5, 1)},
                        f"p95 {p95:.0f}ms 接近 {timeout}s 预算",
                        confidence="ok",
                    )
                elif p95 < 0.05 * budget_ms and float(timeout) >= 8:
                    propose(
                        "timeout_lower",
                        "set_timeout",
                        {"timeout": max(3.0, round(float(timeout) / 3, 1))},
                        f"p95 {p95:.0f}ms 远低于 {timeout}s 预算",
                        confidence="ok",
                    )

    failed_steps = [row["id"] for row in rows if row["ok"] < row["runs"]]

    return {
        "template": {
            "id": template.id,
            "version": template.version,
            "path": str(template.path),
            "screen": list(screen),
        },
        "generated_at": (today or date.today()).isoformat(),
        "samples": {
            "runs": runs,
            "ok_runs": ok_runs,
            "p50_ms": round(_median(durations), 1),
            "p95_ms": round(_p95(durations), 1),
            "enough": runs >= 3,
        },
        "coords": state,
        "steps": rows,
        "proposals": proposals,
        "failed_steps": failed_steps,
    }


def _proposal_markdown(proposal: dict[str, Any]) -> str:
    step_id = proposal["step_id"]
    kind = proposal["kind"]
    value = proposal["value"]
    low = "（样本偏低）" if proposal.get("confidence") == "low" else ""
    if kind == "promote_coords":
        return (
            f"**{step_id} 升级为坐标**{low}\n"
            "```yaml\n"
            + yaml_step(step_id, "tap", text=value.get("text"), at=value["at"])
            + "\n```"
        )
    if kind == "demote_coords":
        return (
            f"**{step_id} 降级为文字点击**（{proposal['reason']}）\n"
            "```yaml\n"
            + yaml_step(step_id, "tap_text", text=value["text"])
            + "\n```"
        )
    if kind == "widen_roi":
        return f"**{step_id} 放宽 roi**（{proposal['reason']}）：建议 roi: {value['roi']}"
    if kind == "timeout_raise":
        return f"**{step_id} timeout 偏紧**（{proposal['reason']}）：建议 timeout: {value['timeout']}"
    return f"**{step_id} timeout 偏松**（{proposal['reason']}）：建议 timeout: {value['timeout']}"


def render_review(analysis: dict[str, Any]) -> str:
    """Markdown review generated from the same data as review.json."""
    meta = analysis["template"]
    samples = analysis["samples"]
    coords = analysis["coords"]
    runs = samples["runs"]
    lines: list[str] = [
        f"# 复盘 — {meta['id']} v{meta['version']}",
        "",
        f"- 样本：最近 {runs} 次执行，成功 {samples['ok_runs']}/{runs}"
        + ("" if samples["enough"] else "（**样本不足**，建议至少 3 次再改模板）"),
        f"- 耗时：p50 {samples['p50_ms']:.0f} ms · p95 {samples['p95_ms']:.0f} ms",
        f"- 坐标：{'可用' if coords['usable'] else '不可用'} — {coords['reason']}",
        "",
        "## 逐步",
        "",
        "| 步 | 动作 | 成功 | 尝试 | p95 ms | 备注 |",
        "|---|---|---|---|---|---|",
    ]
    for row in analysis["steps"]:
        lines.append(
            f"| {row['id']} | {row['do']} | {row['ok']}/{row['runs']} | {row['attempts']} "
            f"| {row['p95_ms']:.0f} | {row['note']} |"
        )

    proposals = analysis["proposals"]
    lines += ["", "## 建议改动", ""]
    if proposals:
        for index, proposal in enumerate(proposals, 1):
            lines.append(f"{index}. {_proposal_markdown(proposal)}")
        lines.append("")
        lines.append("用 `tools/tpl_apply.py` 查看 diff 并应用（默认预览，`--yes` 落盘）。")
    else:
        lines.append("无（数据不足以支持改动，或当前模板已是最优）")

    lines += ["", "## 下一步", ""]
    if analysis["failed_steps"]:
        lines.append(f"- 先修失败步：{', '.join(analysis['failed_steps'])}")
    if runs < 3:
        lines.append(f"- 再跑 {3 - runs} 次，样本够了再动模板")
    if proposals:
        lines.append("- 应用建议：`.venv/bin/python tools/tpl_apply.py <模板.md> --yes`")
    if coords["stale"]:
        lines.append("- 坐标已过期：重新标定后再启用 tap，或用 `--force-coords` 临时覆盖")
    lines.append("")
    return "\n".join(lines)


def build_review(template: Template, traces: list[dict[str, Any]], today: date | None = None) -> str:
    """Backward-compatible markdown review."""
    return render_review(analyse_template(template, traces, today))
