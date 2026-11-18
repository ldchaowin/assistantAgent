"""Contract test for the JSON interface the DSH plugin shells out to."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "tpl_apply.py"

MD = """---
id: demo
name: 演示
version: 2
screen: [1080, 2400]
coords_known: true
coords_updated: 2026-11-01
coords_ttl_days: 30
---

## 步骤

```yaml
steps:
  - {id: s1, do: tap_text, text: 设置, at: [0.5, 0.5]}
```

## 变更记录

| 版本 | 日期 | 改了什么 | 依据 |
|---|---|---|---|
| 2 | 2026-11-01 | 初始 | 手写 |
"""

PROPOSALS = {
    "proposals": [
        {
            "uid": "s1:promote_coords",
            "kind": "promote_coords",
            "step_id": "s1",
            "value": {"at": [0.5, 0.5], "text": "设置"},
            "reason": "稳定",
        }
    ]
}


def run_tool(args, cwd):
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


def make_workspace(tmp_path: Path) -> Path:
    (tmp_path / "templates").mkdir()
    template = tmp_path / "templates" / "demo.md"
    template.write_text(MD, encoding="utf-8")
    review_dir = tmp_path / "runs" / "demo" / "20260113"
    review_dir.mkdir(parents=True)
    (review_dir / "review.json").write_text(json.dumps(PROPOSALS), encoding="utf-8")
    return template


def test_json_preview_reports_diff_without_writing(tmp_path):
    template = make_workspace(tmp_path)
    before = template.read_text(encoding="utf-8")
    result = run_tool([str(template), "--json"], tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["written"] is False
    assert payload["applied"][0]["kind"] == "promote_coords"
    assert "@@" in payload["diff"]
    assert template.read_text(encoding="utf-8") == before


def test_json_apply_bumps_version_and_reports_backup(tmp_path):
    template = make_workspace(tmp_path)
    result = run_tool([str(template), "--json", "--yes"], tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["written"] is True
    assert payload["new_version"] == 3
    assert Path(payload["backup"]).is_file()
    assert "version: 3" in template.read_text(encoding="utf-8")


def test_json_rollback_restores_previous(tmp_path):
    template = make_workspace(tmp_path)
    run_tool([str(template), "--json", "--yes"], tmp_path)
    result = run_tool([str(template), "--json", "--rollback"], tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["to_version"] == 2
    assert "version: 2" in template.read_text(encoding="utf-8")
