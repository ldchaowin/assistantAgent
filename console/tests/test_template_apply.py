from datetime import date
from pathlib import Path

import pytest

from phone_console.template import TemplateError, analyse_template, collect_traces, parse_template, render_review
from phone_console.template_apply import (
    apply_proposals,
    backup_template,
    load_proposals,
    rollback_template,
)

MD = """---
id: demo
name: 演示
version: 2
status: draft
screen: [1080, 2400]
coords_known: true
coords_updated: 2026-11-01
coords_ttl_days: 30
---

# 演示

## 步骤

```yaml
# 坐标归一化 0–1
steps:
  - {id: s1, do: tap_text, text: 设置, at: [0.5, 0.5]}
  - {id: s2, do: tap, text: 打开, at: [0.2, 0.2]}
  - {id: s3, do: expect, text: WLAN, roi: [0.0, 0.1, 0.4, 0.2], timeout: 3}
```

## 变更记录

| 版本 | 日期 | 改了什么 | 依据 |
|---|---|---|---|
| 2 | 2026-11-01 | 初始 | 手写 |
"""


def make_template(tmp_path: Path) -> Path:
    path = tmp_path / "demo.md"
    path.write_text(MD, encoding="utf-8")
    return path


def write_trace(root: Path, name: str, steps, done_ok=True) -> Path:
    import json

    run = root / "demo" / name
    run.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"t": "run", "id": "demo", "screen": [1080, 2400]})]
    lines += [json.dumps({"t": "step", **step}) for step in steps]
    lines.append(json.dumps({"t": "done", "ok": done_ok, "steps": len(steps), "ms": 500.0}))
    (run / "trace.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run


def promote_steps():
    return [
        {"i": 1, "id": "s1", "do": "tap_text", "ok": True, "att": 1, "ms": 40.0, "center": [540.0, 1200.0]},
        {"i": 2, "id": "s2", "do": "tap", "ok": True, "att": 1, "ms": 30.0},
        {"i": 3, "id": "s3", "do": "expect", "ok": True, "att": 4, "ms": 2800.0},
    ]


def test_analyse_proposes_promote_demote_and_timeout(tmp_path):
    root = tmp_path / "runs"
    for index in range(3):
        write_trace(root, f"2026010{index}", promote_steps() , done_ok=True)
    traces = collect_traces(root / "demo", limit=5)
    template = parse_template(make_template(tmp_path))
    analysis = analyse_template(template, traces, today=date(2026, 11, 16))
    kinds = {(proposal["step_id"], proposal["kind"]) for proposal in analysis["proposals"]}
    assert ("s1", "promote_coords") in kinds
    assert ("s3", "widen_roi") in kinds
    assert ("s3", "timeout_raise") in kinds
    assert analysis["failed_steps"] == []


def test_apply_bumps_version_and_keeps_backup(tmp_path):
    template_path = make_template(tmp_path)
    proposals = [
        {
            "uid": "s1:promote_coords",
            "kind": "promote_coords",
            "step_id": "s1",
            "value": {"at": [0.5, 0.5], "text": "设置"},
        },
        {
            "uid": "s3:timeout_raise",
            "kind": "timeout_raise",
            "step_id": "s3",
            "value": {"timeout": 4.5},
        },
    ]
    result = apply_proposals(template_path, proposals, today=date(2026, 11, 16))
    assert result["written"] is True
    assert result["new_version"] == 3
    assert Path(result["backup"]).is_file()
    text = template_path.read_text(encoding="utf-8")
    assert "version: 3" in text
    assert "coord" in result["diff"]
    assert "- {id: s1, do: tap, text: 设置, at: [0.5000, 0.5000]}" in text
    assert "- {id: s3, do: expect, text: WLAN, roi: [0.0000, 0.1000, 0.4000, 0.2000], timeout: 4.5}" in text
    assert "coords_updated: 2026-11-16" in text
    assert "应用复盘建议" in text
    # comments and the rest of the file survive
    assert "# 坐标归一化 0–1" in text


def test_apply_dry_run_writes_nothing(tmp_path):
    template_path = make_template(tmp_path)
    before = template_path.read_text(encoding="utf-8")
    result = apply_proposals(
        template_path,
        [{"kind": "promote_coords", "step_id": "s1", "value": {"at": [0.4, 0.6]}}],
        dry_run=True,
    )
    assert result["written"] is False
    assert result["diff"]
    assert template_path.read_text(encoding="utf-8") == before
    assert not (tmp_path / ".versions").exists()


def test_apply_skips_unsupported_and_unknown(tmp_path):
    template_path = make_template(tmp_path)
    result = apply_proposals(
        template_path,
        [
            {"kind": "reorder_steps", "step_id": "s1", "value": {}},
            {"kind": "promote_coords", "step_id": "missing", "value": {"at": [0.1, 0.1]}},
            {"kind": "timeout_raise", "step_id": "s1", "value": {"timeout": 9}},
        ],
        dry_run=True,
    )
    assert result["applied"] == []
    assert len(result["skipped"]) == 3
    assert template_path.read_text(encoding="utf-8") == MD


def test_rollback_walks_back(tmp_path):
    template_path = make_template(tmp_path)
    apply_proposals(
        template_path,
        [{"kind": "promote_coords", "step_id": "s1", "value": {"at": [0.5, 0.5], "text": "设置"}}],
        today=date(2026, 11, 16),
    )
    assert parse_template(template_path).version == 3
    back = rollback_template(template_path)
    assert back["to_version"] == 2
    assert parse_template(template_path).version == 2
    assert "do: tap_text" in template_path.read_text(encoding="utf-8")


def test_review_markdown_mentions_apply_command(tmp_path):
    root = tmp_path / "runs"
    for index in range(3):
        write_trace(root, f"2026010{index}", promote_steps())
    analysis = analyse_template(parse_template(make_template(tmp_path)), collect_traces(root / "demo"))
    review = render_review(analysis)
    assert "tools/tpl_apply.py" in review
    assert "s1 升级为坐标" in review


def test_load_proposals_from_review_json(tmp_path):
    import json

    path = tmp_path / "review.json"
    path.write_text(json.dumps({"proposals": [{"kind": "promote_coords", "step_id": "s1"}]}), encoding="utf-8")
    assert load_proposals(path)[0]["kind"] == "promote_coords"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        load_proposals(path)


def test_select_safe_keeps_only_conservative_corrections():
    from phone_console.template_apply import select_confident, select_safe

    proposals = [
        {"kind": "demote_coords", "step_id": "s1", "evidence": {"runs": 1}},
        {"kind": "timeout_raise", "step_id": "s2", "evidence": {"runs": 1}},
        {"kind": "widen_roi", "step_id": "s3", "evidence": {"runs": 1}},
        {"kind": "promote_coords", "step_id": "s4", "evidence": {"runs": 1}},
        {"kind": "timeout_lower", "step_id": "s5", "evidence": {"runs": 1}},
    ]
    assert [item["kind"] for item in select_safe(proposals)] == [
        "demote_coords",
        "timeout_raise",
        "widen_roi",
    ]
    # Optimistic edits need repeated evidence.
    assert select_confident(proposals, min_runs=3) == []
    proposals[3]["evidence"] = {"runs": 3}
    proposals[3]["confidence"] = "ok"
    assert [item["step_id"] for item in select_confident(proposals, min_runs=3)] == ["s4"]


def test_safe_proposals_apply_from_a_single_run(tmp_path):
    from phone_console.template_apply import select_safe

    template_path = make_template(tmp_path)
    proposals = select_safe(
        [
            {"kind": "timeout_raise", "step_id": "s3", "value": {"timeout": 4.5}},
            {"kind": "promote_coords", "step_id": "s1", "value": {"at": [0.5, 0.5]}},
        ]
    )
    result = apply_proposals(template_path, proposals, today=date(2026, 11, 16))
    assert result["written"] is True
    assert [item["kind"] for item in result["applied"]] == ["timeout_raise"]
    text = template_path.read_text(encoding="utf-8")
    assert "timeout: 4.5" in text
    # The optimistic proposal was filtered out before applying.
    assert "do: tap_text" in text


def test_set_approach_replaces_old_path_and_bumps_version(tmp_path):
    from phone_console.template_apply import set_approach

    md = """---
id: demo
name: 演示
version: 3
screen: [1080, 2400]
entry:
  expect: 我的淘宝
  timeout: 10
  approach:
    - {do: key, code: HOME}
    - {do: tap_text, text: 淘宝}
---

## 步骤

```yaml
steps:
  - {id: s1, do: key, code: BACK}
```

## 变更记录

| 版本 | 日期 | 改了什么 | 依据 |
|---|---|---|---|
| 3 | 2026-11-17 | 初始 | 手写 |
"""
    path = tmp_path / "demo.md"
    path.write_text(md, encoding="utf-8")
    result = set_approach(
        path,
        [
            {"do": "key", "code": "HOME"},
            {"do": "tap_text", "text": "买买买"},
            {"do": "tap_text", "text": "淘宝"},
            {"do": "swipe"},  # coordinates are never learned
        ],
        today=date(2026, 11, 18),
    )
    assert result["version"] == 3
    assert result["new_version"] == 4
    text = path.read_text(encoding="utf-8")
    assert "version: 4" in text
    assert "买买买" in text
    assert "写入自动导航学到的到达路径（3 步）" in text
    # The old approach line is replaced, but expect/timeout survive.
    assert "expect: 我的淘宝" in text
    assert text.count("approach:") == 1


def test_set_approach_rewrites_flow_entry_as_block(tmp_path):
    from phone_console.template_apply import set_approach

    md = """---
id: demo
name: 演示
version: 1
screen: [1080, 2400]
entry: {expect: 专属客服, timeout: 8}
---

## 步骤

```yaml
steps:
  - {id: s1, do: key, code: BACK}
```
"""
    path = tmp_path / "demo.md"
    path.write_text(md, encoding="utf-8")
    set_approach(path, [{"do": "tap_text", "text": "我的淘宝"}], today=date(2026, 11, 18))
    text = path.read_text(encoding="utf-8")
    assert "entry:\n  expect: 专属客服\n  timeout: 8\n  approach:" in text
    assert "- {do: tap_text" in text


def test_set_approach_requires_entry_and_steps(tmp_path):
    import pytest

    from phone_console.template import TemplateError
    from phone_console.template_apply import set_approach

    plain = tmp_path / "plain.md"
    plain.write_text(
        """---
id: demo
name: 演示
version: 1
screen: [1080, 2400]
---

## 步骤

```yaml
steps:
  - {id: s1, do: key, code: BACK}
```
""",
        encoding="utf-8",
    )
    with pytest.raises(TemplateError, match="没有 entry"):
        set_approach(plain, [{"do": "key", "code": "HOME"}])

    with_entry = tmp_path / "with-entry.md"
    with_entry.write_text(
        """---
id: demo
name: 演示
version: 1
screen: [1080, 2400]
entry:
  expect: 首页
  timeout: 5
---

## 步骤

```yaml
steps:
  - {id: s1, do: key, code: BACK}
```
""",
        encoding="utf-8",
    )
    with pytest.raises(TemplateError, match="为空"):
        set_approach(with_entry, [{"do": "swipe"}])
