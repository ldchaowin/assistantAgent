"""Event-stream contract used by the DSH plugin to show replay progress."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "tpl_run.py"


def write_template(tmp_path: Path) -> Path:
    """Self-contained template: repo templates are user data and may be edited."""
    frames = (ROOT / "scenarios" / "frames" / "price").as_posix()
    path = tmp_path / "demo.md"
    path.write_text(
        f"""---
id: demo
name: 冒烟模板
version: 1
screen: [1080, 2400]
frames: {frames}
coords_known: true
coords_updated: 2026-11-17
---

## 步骤

```yaml
steps:
  - {{id: s1, do: tap_text, text: 京东}}
  - {{id: s2, do: expect, text: 我的订单, timeout: 3}}
  - {{id: s3, do: tap_text, text: 我的}}
  - {{id: s4, do: expect, text: 客户服务, timeout: 3}}
```
""",
        encoding="utf-8",
    )
    return path


def test_events_json_streams_start_steps_and_done(tmp_path):
    template = write_template(tmp_path)
    result = subprocess.run(
        [sys.executable, str(TOOL), str(template), "--mock", "--events-json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    kinds = [event["event"] for event in events]
    assert kinds[0] == "start"
    assert kinds.count("step") == 4
    assert "summary" in kinds
    assert "review" in kinds
    assert kinds[-1] == "done"

    start = events[0]
    assert start["template"] == "demo"
    assert start["total"] == 4
    assert "runs/demo/" in start["runDir"].replace("\\", "/")

    step = next(event for event in events if event["event"] == "step")
    assert step["index"] == 1
    assert step["ok"] is True
    assert step["total"] == 4

    done = events[-1]
    assert done["ok"] is True
    assert done["steps"] == 4
    assert Path(done["tracePath"]).is_file()
    assert Path(done["reviewPath"]).is_file()


def test_events_json_suppresses_human_output(tmp_path):
    template = write_template(tmp_path)
    result = subprocess.run(
        [sys.executable, str(TOOL), str(template), "--mock", "--events-json", "--no-review"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        assert line.startswith("{"), line
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert not any(event["event"] == "review" for event in events)


def write_failing_template(tmp_path):
    frames = (ROOT / "scenarios" / "frames" / "price").as_posix()
    template = tmp_path / "failing.md"
    template.write_text(
        f"""---
id: failing
name: 起始页不匹配
version: 1
screen: [1080, 2400]
coords_known: true
coords_updated: 2026-11-17
frames: {frames}
---

## 步骤

```yaml
steps:
  - {{id: s1, do: expect, text: 绝不存在的文字, timeout: 1}}
  - {{id: s2, do: expect, text: 京东, timeout: 1}}
```

## 变更记录

| 版本 | 日期 | 改了什么 | 依据 |
|---|---|---|---|
| 1 | 2026-11-17 | 测试 | - |
""",
        encoding="utf-8",
    )
    return template


def test_first_step_failure_skips_auto_apply(tmp_path):
    template = write_failing_template(tmp_path)
    result = subprocess.run(
        [sys.executable, str(TOOL), str(template), "--mock", "--events-json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert not any(event["event"] == "applied" for event in events)
    # The template must stay at its original version/timeout.
    text = template.read_text(encoding="utf-8")
    assert "version: 1" in text
    assert "timeout: 1" in text
