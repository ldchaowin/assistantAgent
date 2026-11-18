#!/usr/bin/env python3
"""Review existing traces for a task template, without running anything.

    tools/tpl_review.py examples/templates/taobao-price-protection.md
    tools/tpl_review.py examples/templates/settings-wifi.md --runs 20 --out runs/x/review.md

Reads runs/<id>/*/trace.jsonl (newest --runs first), writes a markdown review
and prints it. Exit code is always 0; the review is advice, not a gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from phone_console.template import (
    TemplateError,
    analyse_template,
    collect_traces,
    parse_template,
    render_review,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review a task template from its traces")
    parser.add_argument("template", type=Path)
    parser.add_argument("--runs", type=int, default=10, help="how many traces to read")
    parser.add_argument("--root", type=Path, help="trace root (default runs/<id>)")
    parser.add_argument("--out", type=Path, help="write review.md here as well")
    parser.add_argument("--json-out", type=Path, help="write machine-readable proposals here")
    parser.add_argument("--json", action="store_true", help="print JSON proposals instead of markdown")
    parser.add_argument("--today", help="override today, YYYY-MM-DD (coordinate age)")
    args = parser.parse_args(argv)

    try:
        template = parse_template(args.template)
    except (TemplateError, OSError) as exc:
        print(f"ERROR {args.template}: {exc}", file=sys.stderr)
        return 2

    root = args.root or Path("runs") / template.id
    traces = collect_traces(root, limit=args.runs)
    if not traces:
        print(f"没有找到 {root}/*/trace.jsonl，先跑一次 tpl_run.py", file=sys.stderr)
        return 1

    today = date.fromisoformat(args.today) if args.today else None
    analysis = analyse_template(template, traces, today)
    review = render_review(analysis)
    if args.json:
        print(json.dumps(analysis, ensure_ascii=False, indent=2))
        return 0
    print(review)

    # Default: drop review.md/review.json next to the newest trace so
    # `tpl_apply` can pick the proposals up without extra flags.
    out = args.out
    json_out = args.json_out
    if out is None and json_out is None:
        latest = Path(traces[-1]["path"]).parent
        out = latest / "review.md"
        json_out = latest / "review.json"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(review, encoding="utf-8")
        print(f"-> {out}")
    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"-> {json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
