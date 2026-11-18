"""Apply review proposals back into a task template, with versioning.

The reviewer proposes; a human (or CI) decides. This module is the "apply"
half of that loop:

* only a whitelist of mechanical edits is supported (coordinates, timeout, ROI);
* every write backs the old file up to ``templates/.versions/<id>/vN.md``;
* the template version is bumped and the changelog gets one row;
* ``dry_run`` returns the diff without touching anything;
* ``rollback`` restores an older version and keeps walking backwards.

Semantic changes (step order, deletes, irreversible steps) are never applied
here — the reviewer only prints them.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import date
from difflib import unified_diff
from pathlib import Path
from typing import Any

from .template import (
    STEPS_BLOCK,
    Template,
    TemplateError,
    parse_template,
    render_step_line,
)

VERSIONS_DIRNAME = ".versions"
MAX_VERSIONS = 30
SUPPORTED_KINDS = {
    "promote_coords",
    "demote_coords",
    "timeout_raise",
    "timeout_lower",
    "widen_roi",
}

# Corrections that only make the template more tolerant. They are safe to apply
# from a single run because a failure is already conclusive evidence, and they
# never change the template's semantics.
SAFE_KINDS = {
    "demote_coords",   # a coordinate that failed falls back to text
    "timeout_raise",   # a timeout that nearly fired gets headroom
    "widen_roi",       # a check that needed retries gets a wider search box
}


def select_safe(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the conservative corrections (usable from one failing run)."""
    return [proposal for proposal in proposals if proposal.get("kind") in SAFE_KINDS]


def select_confident(proposals: list[dict[str, Any]], min_runs: int = 3) -> list[dict[str, Any]]:
    """Mechanical edits that need repeated evidence before they are safe."""
    confident = []
    for proposal in proposals:
        if proposal.get("kind") not in SUPPORTED_KINDS:
            continue
        if proposal.get("kind") in SAFE_KINDS:
            continue
        evidence = proposal.get("evidence") or {}
        if int(evidence.get("runs") or 0) >= min_runs and proposal.get("confidence") != "low":
            confident.append(proposal)
    return confident


def versions_dir(template_path: Path) -> Path:
    return template_path.parent / VERSIONS_DIRNAME / template_path.stem


def _backup_versions(template_path: Path) -> list[tuple[int, Path]]:
    directory = versions_dir(template_path)
    if not directory.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for path in directory.glob("v*.md"):
        match = re.fullmatch(r"v(\d+)", path.stem)
        if match:
            found.append((int(match.group(1)), path))
    return sorted(found)


def backup_template(template_path: Path, version: int) -> Path:
    directory = versions_dir(template_path)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"v{version}.md"
    if not target.exists():
        shutil.copyfile(template_path, target)
    backups = _backup_versions(template_path)
    for _, old in backups[:-MAX_VERSIONS]:
        old.unlink(missing_ok=True)
    return target


def _set_frontmatter_scalar(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    replacement = f"{key}: {value}"
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    match = re.match(r"(\A---\s*\n)(.*?)(\n---\s*\n)", text, re.DOTALL)
    if not match:
        return text
    return f"{match.group(1)}{match.group(2)}\n{replacement}{match.group(3)}{text[match.end():]}"


def replace_steps_block(text: str, steps: list[dict[str, Any]]) -> str:
    match = STEPS_BLOCK.search(text)
    if not match:
        raise TemplateError('missing "## 步骤" section with a ```yaml steps block')
    block = match.group(2)
    lines = block.splitlines()
    header: list[str] = []
    for index, line in enumerate(lines):
        header.append(line)
        if line.strip().startswith("steps:"):
            break
    else:
        header = ["steps:"]
    rendered = [render_step_line(step) for step in steps]
    new_block = "\n".join(header + rendered) + "\n"
    return text[: match.start(2)] + new_block + text[match.end(2) :]


def add_changelog_row(text: str, row: str) -> str:
    section = re.search(r"^##\s*变更记录\s*$", text, re.MULTILINE)
    if not section:
        return (
            text.rstrip("\n")
            + "\n\n## 变更记录\n\n| 版本 | 日期 | 改了什么 | 依据 |\n|---|---|---|---|\n"
            + row
            + "\n"
        )
    rest = text[section.end() :]
    following = re.search(r"^##\s", rest, re.MULTILINE)
    end = section.end() + (following.start() if following else len(rest))
    body = text[section.end() : end]
    lines = body.rstrip("\n").split("\n")
    table_rows = [index for index, line in enumerate(lines) if line.strip().startswith("|")]
    if table_rows:
        insert_at = table_rows[-1] + 1
        lines.insert(insert_at, row)
    else:
        lines = ["", "| 版本 | 日期 | 改了什么 | 依据 |", "|---|---|---|---|", row]
    new_body = "\n".join(lines) + "\n"
    return text[: section.end()] + new_body + text[end:]


def _apply_one(step: dict[str, Any], proposal: dict[str, Any]) -> str | None:
    """Mutate `step` in place. Returns an error string when unsupported."""
    kind = proposal.get("kind")
    value = proposal.get("value") or {}
    if kind == "promote_coords":
        at = value.get("at")
        if not isinstance(at, (list, tuple)) or len(at) != 2:
            return "缺少坐标"
        try:
            pair = [round(float(at[0]), 4), round(float(at[1]), 4)]
        except (TypeError, ValueError):
            return "坐标不是数字"
        if not all(0.0 <= item <= 1.0 for item in pair):
            return "坐标超出 0–1"
        step["do"] = "tap"
        step["at"] = pair
        if value.get("text"):
            step["text"] = str(value["text"])
        return None
    if kind == "demote_coords":
        text = value.get("text") or step.get("text")
        if not text:
            return "没有可用的 text 兜底"
        step["do"] = "tap_text"
        step["text"] = str(text)
        return None
    if kind in {"timeout_raise", "timeout_lower"}:
        if step.get("do") != "expect":
            return "只有 expect 步骤可以调 timeout"
        try:
            timeout = float(value.get("timeout"))
        except (TypeError, ValueError):
            return "timeout 不是数字"
        if not 0 < timeout <= 120:
            return "timeout 超出 0–120 秒"
        step["timeout"] = round(timeout, 1)
        return None
    if kind == "widen_roi":
        if step.get("do") != "expect":
            return "只有 expect 步骤有 roi"
        roi = value.get("roi")
        if not isinstance(roi, (list, tuple)) or len(roi) != 4:
            return "roi 必须是四元组"
        try:
            quad = [round(float(item), 4) for item in roi]
        except (TypeError, ValueError):
            return "roi 不是数字"
        if not all(0.0 <= item <= 1.0 for item in quad):
            return "roi 超出 0–1"
        step["roi"] = quad
        return None
    return f"不支持的改动类型 {kind}"


def apply_proposals(
    template_path: Path,
    proposals: list[dict[str, Any]],
    *,
    today: date | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    template = parse_template(template_path)
    original = template_path.read_text(encoding="utf-8")
    today = today or date.today()

    steps = [dict(step) for step in template.steps]
    by_id = {str(step.get("id")): step for step in steps}
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    touched_coords = False

    for proposal in proposals:
        step_id = str(proposal.get("step_id") or proposal.get("id") or "")
        kind = proposal.get("kind")
        step = by_id.get(step_id)
        if step is None:
            skipped.append({"id": step_id, "kind": kind, "reason": "模板里没有这一步"})
            continue
        if kind not in SUPPORTED_KINDS:
            skipped.append({"id": step_id, "kind": kind, "reason": "这类改动需要人工处理"})
            continue
        problem = _apply_one(step, proposal)
        if problem:
            skipped.append({"id": step_id, "kind": kind, "reason": problem})
            continue
        if kind == "promote_coords":
            touched_coords = True
        applied.append({"id": step_id, "kind": kind, "value": step.get("at") or step.get("roi") or step.get("timeout")})

    if not applied:
        return {
            "applied": [],
            "skipped": skipped,
            "file": str(template_path),
            "backup": None,
            "version": template.version,
            "new_version": template.version,
            "diff": "",
            "written": False,
        }

    new_version = template.version + 1
    updated = replace_steps_block(original, steps)
    updated = _set_frontmatter_scalar(updated, "version", str(new_version))
    if touched_coords:
        updated = _set_frontmatter_scalar(updated, "coords_updated", today.isoformat())
    changed = "；".join(
        f"{item['id']} {item['kind']}" for item in applied
    )
    row = (
        f"| {new_version} | {today.isoformat()} | 应用复盘建议：{changed} "
        f"| 最近 {len(applied)} 条 trace 提案 |"
    )
    updated = add_changelog_row(updated, row)

    diff = "".join(
        unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"{template_path} (v{template.version})",
            tofile=f"{template_path} (v{new_version})",
        )
    )

    backup_path = None
    if not dry_run:
        backup_path = backup_template(template_path, template.version)
        template_path.write_text(updated, encoding="utf-8")

    return {
        "applied": applied,
        "skipped": skipped,
        "file": str(template_path),
        "backup": str(backup_path) if backup_path else None,
        "version": template.version,
        "new_version": new_version,
        "diff": diff,
        "written": not dry_run,
    }


def rollback_template(template_path: Path, *, to_version: int | None = None) -> dict[str, Any]:
    template = parse_template(template_path)
    backups = _backup_versions(template_path)
    if not backups:
        raise TemplateError(f"没有可回滚的版本：{versions_dir(template_path)}")
    if to_version is not None:
        candidates = [(version, path) for version, path in backups if version == to_version]
    else:
        candidates = [(version, path) for version, path in backups if version < template.version]
    if not candidates:
        raise TemplateError(
            f"找不到可回滚的版本（当前 v{template.version}，已有 {[v for v, _ in backups]}）"
        )
    version, path = candidates[-1]
    content = path.read_text(encoding="utf-8")
    template_path.write_text(content, encoding="utf-8")
    restored = parse_template(template_path)
    return {
        "file": str(template_path),
        "from_version": template.version,
        "to_version": version,
        "version_now": restored.version,
        "backup": str(path),
    }


def load_proposals(path: Path) -> list[dict[str, Any]]:
    """Read proposals from a review.json produced by tpl_run/tpl_review."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    proposals = payload.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError(f"{path} 里没有 proposals 数组")
    return proposals


# ---------------------------------------------------------- learned approach
APPROACH_ACTIONS = {"tap_text", "key", "text", "sleep"}


def _approach_step_line(step: dict[str, Any]) -> str:
    action = step.get("do")
    if action == "tap_text":
        return f"    - {{do: tap_text, text: {json.dumps(str(step['text']), ensure_ascii=False)}}}"
    if action == "key":
        return f"    - {{do: key, code: {json.dumps(str(step['code']).upper(), ensure_ascii=False)}}}"
    if action == "text":
        return f"    - {{do: text, value: {json.dumps(str(step['value']), ensure_ascii=False)}}}"
    return f"    - {{do: sleep, ms: {int(step.get('ms') or 0)}}}"


def _clean_approach_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        action = step.get("do")
        if action not in APPROACH_ACTIONS:
            continue
        if action == "tap_text" and not str(step.get("text") or "").strip():
            continue
        if action == "key" and not str(step.get("code") or "").strip():
            continue
        if action == "text" and not str(step.get("value") or "").strip():
            continue
        cleaned.append(step)
    return cleaned


def _replace_block_approach(lines: list[str], entry_start: int, steps: list[dict[str, Any]]) -> list[str]:
    end = entry_start + 1
    while end < len(lines) and (lines[end].startswith(" ") or lines[end].startswith("\t")):
        end += 1
    block = lines[entry_start + 1 : end]
    # Drop the old approach sub-block (and its list items).
    kept: list[str] = []
    index = 0
    while index < len(block):
        line = block[index]
        if re.match(r"^\s+approach:\s*$", line):
            index += 1
            while index < len(block) and re.match(r"^\s+-\s+", block[index]):
                index += 1
            continue
        kept.append(line)
        index += 1
    # Keep `expect`/`timeout` order stable, then append the learned approach.
    new_block = kept + ["  approach:"] + [_approach_step_line(step) for step in steps]
    return lines[: entry_start + 1] + new_block + lines[end:]


def set_approach(
    template_path: Path,
    steps: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Replace `entry.approach` with the path that actually worked (guided run)."""
    from .template import parse_entry

    template = parse_template(template_path)
    entry = parse_entry(template.meta)
    if not entry:
        raise TemplateError("模板没有 entry 块，无法写入到达路径")
    cleaned = _clean_approach_steps(steps)
    if not cleaned:
        raise TemplateError("学到的到达路径为空（没有任何文字/按键动作）")

    original = template_path.read_text(encoding="utf-8")
    lines = original.splitlines()
    entry_start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(r"^entry:\s*(\{.*\})?\s*$", line)
        ),
        None,
    )
    if entry_start is None:
        raise TemplateError("找不到 entry 块")
    if re.match(r"^entry:\s*\{", lines[entry_start]):
        # Flow style: rewrite as a block so the approach list has room to grow.
        flow = lines[entry_start]
        expect_match = re.search(r"expect:\s*([^,}]+)", flow)
        timeout_match = re.search(r"timeout:\s*([\d.]+)", flow)
        rebuilt = [
            "entry:",
            f"  expect: {expect_match.group(1).strip() if expect_match else '待确认'}",
            f"  timeout: {timeout_match.group(1) if timeout_match else '15'}",
            "  approach:",
            *[_approach_step_line(step) for step in cleaned],
        ]
        lines = lines[:entry_start] + rebuilt + lines[entry_start + 1 :]
    else:
        lines = _replace_block_approach(lines, entry_start, cleaned)

    updated = "\n".join(lines) + ("\n" if original.endswith("\n") else "")
    new_version = template.version + 1
    updated = _set_frontmatter_scalar(updated, "version", str(new_version))
    day = (today or date.today()).isoformat()
    row = (
        f"| {new_version} | {day} | 写入自动导航学到的到达路径（{len(cleaned)} 步） "
        f"| 一次 guided 执行成功到达起始页 |"
    )
    updated = add_changelog_row(updated, row)

    backup = backup_template(template_path, template.version)
    template_path.write_text(updated, encoding="utf-8")
    return {
        "file": str(template_path),
        "steps": cleaned,
        "version": template.version,
        "new_version": new_version,
        "backup": str(backup),
    }
