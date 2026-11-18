from datetime import date, timedelta

import pytest

from phone_console.template import (
    Template,
    TemplateError,
    build_review,
    compile_scenario,
    coord_state,
    parse_template,
    report_to_trace,
    summarise_runs,
    yaml_step,
)

MD = """---
id: demo
name: 演示
version: 3
screen: [1080, 2400]
coords_known: {known}
coords_updated: {updated}
coords_ttl_days: {ttl}
---

# 演示

## 步骤

```yaml
steps:
  - id: s1
    do: tap
    text: 设置
    at: [0.5, 0.5]
  - id: s2
    do: expect
    text: WLAN
    roi: [0.0, 0.1, 1.0, 0.2]
    timeout: 3
```
"""


def write(tmp_path, known="false", updated="null", ttl=30, text=MD):
    path = tmp_path / "demo.md"
    path.write_text(
        text.format(known=known, updated=updated, ttl=ttl), encoding="utf-8"
    )
    return path


def test_parse_keeps_steps_and_meta(tmp_path) -> None:
    template = parse_template(write(tmp_path))
    assert template.id == "demo"
    assert template.version == 3
    assert template.screen == (1080, 2400)
    assert [s["id"] for s in template.steps] == ["s1", "s2"]


def test_parse_rejects_missing_frontmatter(tmp_path) -> None:
    path = tmp_path / "bad.md"
    path.write_text("## 步骤\n```yaml\nsteps: []\n```\n", encoding="utf-8")
    with pytest.raises(TemplateError, match="frontmatter"):
        parse_template(path)


def test_parse_rejects_duplicate_ids(tmp_path) -> None:
    text = MD.replace("- id: s2", "- id: s1")
    with pytest.raises(TemplateError, match="duplicate"):
        parse_template(write(tmp_path, text=text))


def test_parse_rejects_out_of_range_coordinate(tmp_path) -> None:
    text = MD.replace("at: [0.5, 0.5]", "at: [1.5, 0.5]")
    with pytest.raises(TemplateError, match="0..1"):
        parse_template(write(tmp_path, text=text))


def test_coord_state_unknown_and_stale() -> None:
    assert coord_state({"coords_known": False})["usable"] is False

    today = date(2026, 11, 13)
    fresh = coord_state({"coords_known": True, "coords_updated": "2026-11-01"}, today)
    assert fresh["usable"] is True
    assert fresh["age_days"] == 12

    old = coord_state({"coords_known": True, "coords_updated": "2026-09-01"}, today)
    assert old["usable"] is False
    assert old["stale"] is True
    assert "73 天" in old["reason"]

    missing = coord_state({"coords_known": True}, today)
    assert missing["stale"] is True


def test_compile_degrades_tap_when_coords_unknown(tmp_path) -> None:
    template = parse_template(write(tmp_path))
    scenario = compile_scenario(template)
    assert scenario["steps"][0]["action"] == "tap_text"
    assert scenario["steps"][0]["text"] == "设置"
    assert scenario["steps"][0]["id"] == "s1"
    assert scenario["steps"][1]["roi"] == [0, 240, 1080, 480]
    assert any("降级" in w for w in template.warnings)


def test_compile_keeps_tap_when_coords_fresh(tmp_path) -> None:
    template = parse_template(
        write(tmp_path, known="true", updated=date.today().isoformat())
    )
    scenario = compile_scenario(template)
    assert scenario["steps"][0] == {"action": "tap", "x": 540, "y": 1200, "id": "s1"}


def test_compile_scales_to_other_screen(tmp_path) -> None:
    template = parse_template(
        write(tmp_path, known="true", updated=date.today().isoformat())
    )
    scenario = compile_scenario(template, screen=(1264, 2800))
    assert scenario["steps"][0]["x"] == 632
    assert scenario["steps"][0]["y"] == 1400
    assert scenario["screen"] == [1264, 2800]


def test_compile_needs_text_fallback_when_coords_unusable(tmp_path) -> None:
    text = MD.replace("    text: 设置\n", "")
    template = parse_template(write(tmp_path, text=text))
    with pytest.raises(TemplateError, match="text"):
        compile_scenario(template)
    # 显式覆盖可以跑，但这是人工决定
    assert compile_scenario(template, force_coords=True)["steps"][0]["action"] == "tap"


def test_report_to_trace_maps_steps(tmp_path) -> None:
    report = tmp_path / "report.jsonl"
    report.write_text(
        "\n".join(
            [
                '{"type":"step","index":1,"action":"tap","status":"ok","attempts":1,'
                '"elapsed_ms":12.3,"params":{"id":"s1","x":540,"y":1200},"detail":{}}',
                '{"type":"step","index":2,"action":"assert_text","status":"ok","attempts":2,'
                '"elapsed_ms":40.0,"params":{"id":"s2","text":"WLAN"},'
                '"detail":{"center":[100.5,200.0],"ocr_ms":18.0}}',
                '{"type":"summary","ok":true,"steps":2,"failed":0,"elapsed_ms":60.0}',
            ]
        ),
        encoding="utf-8",
    )
    records = report_to_trace(report, {"id": "demo", "v": 3, "screen": [1080, 2400]})
    assert records[0]["t"] == "run"
    assert records[1] == {
        "t": "step",
        "i": 1,
        "id": "s1",
        "do": "tap",
        "ok": True,
        "att": 1,
        "ms": 12.3,
        "at": [540, 1200],
    }
    assert records[2]["center"] == [100.5, 200.0]
    assert records[2]["do"] == "expect"
    assert records[3]["t"] == "done" and records[3]["ok"] is True


def make_template(tmp_path, steps=None):
    if steps:
        text = MD.split("```yaml")[0] + "```yaml\nsteps:\n" + steps + "\n```\n"
        path = write(tmp_path, text=text)
    else:
        path = write(tmp_path)
    return parse_template(path)


def trace_run(ok, steps):
    return {
        "path": "x",
        "header": {"screen": [1080, 2400]},
        "steps": steps,
        "done": {"t": "done", "ok": ok, "steps": len(steps), "ms": 100.0},
    }


def test_review_proposes_promotion_when_center_is_stable(tmp_path) -> None:
    template = make_template(tmp_path)
    runs = [
        trace_run(
            True,
            [
                {"t": "step", "i": 1, "id": "s1", "do": "tap_text", "ok": True,
                 "att": 1, "ms": 20.0, "center": [540.0, 1200.0]},
                {"t": "step", "i": 2, "id": "s2", "do": "expect", "ok": True,
                 "att": 1, "ms": 18.0, "center": [540.0, 480.0]},
            ],
        )
        for _ in range(3)
    ]
    review = build_review(template, runs, today=date(2026, 11, 13))
    assert "s1 升级为坐标" in review
    assert "do: tap, text: 设置, at: [0.5000, 0.5000]" in review
    assert "样本不足" not in review


def test_review_warns_when_sample_is_small(tmp_path) -> None:
    template = make_template(tmp_path)
    runs = [
        trace_run(
            True,
            [
                {"t": "step", "i": 1, "id": "s1", "do": "tap_text", "ok": True,
                 "att": 1, "ms": 20.0, "center": [540.0, 1200.0]},
            ],
        )
    ]
    review = build_review(template, runs)
    assert "样本不足" in review
    assert "偏低" in review


def test_review_demotes_failing_coordinate(tmp_path) -> None:
    template = make_template(
        tmp_path,
        steps=(
            "  - id: s1\n    do: tap\n    text: 设置\n    at: [0.5, 0.5]\n"
            "  - id: s2\n    do: expect\n    text: WLAN\n    timeout: 3\n"
        ),
    )
    runs = [
        trace_run(
            False,
            [
                {"t": "step", "i": 1, "id": "s1", "do": "tap", "ok": False,
                 "att": 2, "ms": 30.0, "err": "ASSERT_FAILED: 不在设置页"},
            ],
        )
    ]
    review = build_review(template, runs)
    assert "s1 降级为文字点击" in review
    assert "do: tap_text, text: 设置" in review
    assert "先修失败步" in review


def test_review_does_nothing_for_wide_spread(tmp_path) -> None:
    template = make_template(tmp_path)
    runs = [
        trace_run(
            True,
            [
                {"t": "step", "i": 1, "id": "s1", "do": "tap_text", "ok": True,
                 "att": 1, "ms": 20.0, "center": [100.0 + 200 * n, 1200.0]},
            ],
        )
        for n in range(3)
    ]
    review = build_review(template, runs)
    assert "升级为坐标" not in review


def test_summarise_runs_groups_by_step() -> None:
    runs = [
        trace_run(True, [{"t": "step", "id": "s1", "ok": True, "att": 1, "ms": 5.0}]),
        trace_run(False, [{"t": "step", "id": "s1", "ok": False, "att": 2, "ms": 7.0}]),
    ]
    stats = summarise_runs(runs)
    assert stats["runs"] == 2
    assert stats["ok_runs"] == 1
    assert len(stats["steps"]["s1"]) == 2


def test_yaml_step_skips_empty_fields() -> None:
    assert yaml_step("s1", "tap", text="设置", at=[0.5, 0.5], roi=None) == (
        "- {id: s1, do: tap, text: 设置, at: [0.5000, 0.5000]}"
    )


def test_entry_gate_prepends_anchor_and_approach(tmp_path):
    md = """---
id: demo
name: 演示
version: 1
screen: [1080, 2400]
coords_known: true
coords_updated: 2026-11-17
entry:
  expect: 我的淘宝
  timeout: 8
  approach:
    - {do: key, code: HOME}
    - {do: tap_text, text: 淘宝}
---

## 步骤

```yaml
steps:
  - {id: s1, do: tap, text: 设置, at: [0.5, 0.5]}
```
"""
    path = tmp_path / "demo.md"
    path.write_text(md, encoding="utf-8")
    template = parse_template(path)
    scenario = compile_scenario(template)
    actions = [(step["action"], step.get("id")) for step in scenario["steps"]]
    assert actions[0] == ("key", "entry-1")
    assert actions[1] == ("tap_text", "entry-2")
    assert actions[2] == ("assert_text", "entry")
    assert actions[3] == ("tap", "s1")
    assert scenario["steps"][2]["text"] == "我的淘宝"
    assert scenario["steps"][2]["timeout"] == 8.0
    assert any("起始页" in warning for warning in template.warnings)


def test_entry_approach_rejects_coordinates(tmp_path):
    md = """---
id: demo
name: 演示
version: 1
screen: [1080, 2400]
entry:
  expect: 首页
  approach:
    - {do: tap, at: [0.5, 0.5]}
---

## 步骤

```yaml
steps:
  - {id: s1, do: key, code: BACK}
```
"""
    path = tmp_path / "bad.md"
    path.write_text(md, encoding="utf-8")
    with pytest.raises(TemplateError, match="not allowed before the page anchor"):
        parse_template(path)


def test_entry_requires_expect(tmp_path):
    md = """---
id: demo
name: 演示
version: 1
screen: [1080, 2400]
entry:
  timeout: 5
---

## 步骤

```yaml
steps:
  - {id: s1, do: key, code: BACK}
```
"""
    path = tmp_path / "bad.md"
    path.write_text(md, encoding="utf-8")
    with pytest.raises(TemplateError, match="entry.expect"):
        parse_template(path)


def test_expect_after_is_compiled_into_a_check(tmp_path):
    md = """---
id: demo
name: 演示
version: 1
screen: [1080, 2400]
coords_known: true
coords_updated: 2026-11-18
entry:
  expect: 我的淘宝
  timeout: 10
---

## 步骤

```yaml
steps:
  - {id: s1, do: tap, text: 专属客服, at: [0.8, 0.1], expect_after: 官方自助服务}
  - {id: s2, do: tap, at: [0.5, 0.5], expect_after: "这一句是描述，不会编译成校验"}
```
"""
    path = tmp_path / "demo.md"
    path.write_text(md, encoding="utf-8")
    template = parse_template(path)
    scenario = compile_scenario(template)
    ids = [step["id"] for step in scenario["steps"]]
    assert ids.index("s1") < ids.index("s1-check") < ids.index("s2")
    check = scenario["steps"][ids.index("s1-check")]
    assert check["action"] == "assert_text"
    assert check["text"] == "官方自助服务"
    # A sentence is not a reliable on-screen label: skip it, warn instead.
    assert "s2-check" not in ids
    assert any("expect_after 不是短文本" in warning for warning in template.warnings)


def test_entry_only_and_skip_entry_compile_modes(tmp_path):
    md = """---
id: demo
name: 演示
version: 1
screen: [1080, 2400]
coords_known: true
coords_updated: 2026-11-18
entry:
  expect: 我的淘宝
  timeout: 10
  approach:
    - {do: key, code: HOME}
---

## 步骤

```yaml
steps:
  - {id: s1, do: tap, at: [0.5, 0.5], expect_after: 一键价保}
```
"""
    path = tmp_path / "demo.md"
    path.write_text(md, encoding="utf-8")
    template = parse_template(path)
    entry_only = compile_scenario(template, entry_only=True)
    ids = [step["id"] for step in entry_only["steps"]]
    assert ids == ["entry-1", "entry"]
    main_only = compile_scenario(template, include_entry=False)
    assert [step["id"] for step in main_only["steps"]] == ["s1", "s1-check"]


def test_entry_only_requires_an_entry_block(tmp_path):
    md = """---
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
"""
    path = tmp_path / "demo.md"
    path.write_text(md, encoding="utf-8")
    template = parse_template(path)
    with pytest.raises(TemplateError, match="没有 entry"):
        compile_scenario(template, entry_only=True)


def test_check_timeout_controls_the_compiled_assert(tmp_path):
    md = """---
id: demo
name: 演示
version: 1
screen: [1080, 2400]
coords_known: true
coords_updated: 2026-11-18
---

## 步骤

```yaml
steps:
  - {id: s1, do: tap, at: [0.5, 0.8], expect_after: 我知道了, check_timeout: 15}
  - {id: s2, do: tap, at: [0.2, 0.2], expect_after: 已生成}
```
"""
    path = tmp_path / "demo.md"
    path.write_text(md, encoding="utf-8")
    template = parse_template(path)
    scenario = compile_scenario(template)
    by_id = {step["id"]: step for step in scenario["steps"]}
    assert by_id["s1-check"]["timeout"] == 15.0
    assert by_id["s2-check"]["timeout"] == 3.0
