#!/usr/bin/env python3
"""Write a guided run's successful navigation back into a template's entry.approach.

    tools/tpl_learn_approach.py templates/app.md --steps-file /tmp/steps.json --json

The steps file is a JSON list (or {"steps": [...]}) of text/keys actions that
actually reached the start page. Coordinates are never learned here: approach
runs from an unknown state and must stay semantic.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from phone_console.template import TemplateError
from phone_console.template_apply import set_approach


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Save a learned approach into a template")
    parser.add_argument("template", type=Path)
    parser.add_argument("--steps-file", type=Path, required=True)
    parser.add_argument("--today", help="override today, YYYY-MM-DD")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        payload = json.loads(args.steps_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        message = f"读不到步骤文件：{exc}"
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False) if args.json else message)
        return 2
    steps = payload.get("steps") if isinstance(payload, dict) else payload
    if not isinstance(steps, list):
        message = "步骤文件必须是数组或 {steps: [...]}"
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False) if args.json else message)
        return 2

    today = date.fromisoformat(args.today) if args.today else None
    try:
        result = set_approach(args.template, steps, today=today)
    except (TemplateError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False) if args.json else f"ERROR {exc}")
        return 2

    payload = {"ok": True, **result}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"已写入到达路径：{result['steps'] and len(result['steps'])} 步，"
            f"v{result['version']} -> v{result['new_version']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
