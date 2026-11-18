#!/usr/bin/env python3
"""Apply review proposals to a template (proposal-first, human decides).

    tools/tpl_apply.py examples/templates/taobao-price-protection.md                 # 预览 diff
    tools/tpl_apply.py examples/templates/taobao-price-protection.md --yes           # 落盘
    tools/tpl_apply.py examples/templates/taobao-price-protection.md --only s3,s5 --yes
    tools/tpl_apply.py examples/templates/taobao-price-protection.md --from runs/x/2026.../review.json
    tools/tpl_apply.py examples/templates/taobao-price-protection.md --rollback
    tools/tpl_apply.py examples/templates/taobao-price-protection.md --rollback --to 2

Only mechanical edits are applied (coordinates, timeout, ROI). Semantic changes
and anything touching irreversible steps stay as proposals in review.md.
Every write backs the old version up to templates/.versions/<id>/vN.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from phone_console.template import TemplateError, parse_template
from phone_console.template_apply import apply_proposals, load_proposals, rollback_template


def latest_review_json(template_id: str, template_path: Path | None = None) -> Path | None:
    """Newest review.json, from cwd/runs or the sibling runs/ of templates/."""
    roots = [Path("runs") / template_id]
    if template_path is not None:
        roots.append(template_path.resolve().parent.parent / "runs" / template_id)
    seen: list[Path] = []
    for root in roots:
        seen.extend(root.glob("*/review.json"))
    candidates = sorted(set(seen), key=lambda path: path.parent.name)
    return candidates[-1] if candidates else None


def find_proposals(template, review_path: Path | None, only: list[str], template_path: Path | None = None) -> tuple[list[dict], Path | None]:
    if review_path is None:
        review_path = latest_review_json(template.id, template_path)
    if review_path is None:
        raise SystemExit(
            f"没有找到 runs/{template.id}/*/review.json，先跑一次 tpl_run 生成复盘"
        )
    proposals = load_proposals(review_path)
    if only:
        wanted = set(only)
        proposals = [
            proposal
            for proposal in proposals
            if proposal.get("uid") in wanted or proposal.get("step_id") in wanted or proposal.get("id") in wanted
        ]
        if not proposals:
            raise SystemExit(f"--only {','.join(only)} 没有匹配到任何建议")
    return proposals, review_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply review proposals to a task template")
    parser.add_argument("template", type=Path)
    parser.add_argument("--from", dest="review", type=Path, help="review.json（默认取最新一次 tpl_run）")
    parser.add_argument("--only", help="只应用这些建议：uid 或 step id，逗号分隔")
    parser.add_argument("--yes", action="store_true", help="真正写入；不加只打印 diff")
    parser.add_argument("--rollback", action="store_true", help="回滚到上一个版本")
    parser.add_argument("--to", type=int, help="配合 --rollback：回滚到指定版本")
    parser.add_argument("--today", help="覆盖今天，YYYY-MM-DD")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON（给插件用）")
    args = parser.parse_args(argv)

    try:
        template = parse_template(args.template)
    except (TemplateError, OSError) as exc:
        print(f"ERROR {args.template}: {exc}", file=sys.stderr)
        return 2

    if args.rollback:
        try:
            result = rollback_template(args.template, to_version=args.to)
        except TemplateError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(
            f"已回滚 {result['file']}：v{result['from_version']} -> v{result['to_version']}"
            f"（备份 {result['backup']}）"
        )
        return 0

    today = date.fromisoformat(args.today) if args.today else None
    only = [item.strip() for item in (args.only or "").split(",") if item.strip()]
    try:
        proposals, review_path = find_proposals(template, args.review, only, args.template)
    except (OSError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    result = apply_proposals(args.template, proposals, today=today, dry_run=not args.yes)
    if args.json:
        payload = {
            "review": str(review_path),
            "proposals": len(proposals),
            **result,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"建议来源：{review_path}（{len(proposals)} 条）")
    if result["diff"]:
        print(result["diff"], end="")
    for item in result["applied"]:
        print(f"  应用：{item['id']} <- {item['kind']}")
    for item in result["skipped"]:
        print(f"  跳过：{item['id']} ({item.get('kind')})：{item['reason']}")

    if not result["applied"]:
        print("没有可应用的机械改动（语义改动请看 review.md）")
        return 0
    if result["written"]:
        print(
            f"已写入 {result['file']}：v{result['version']} -> v{result['new_version']}"
            f"（旧版本备份 {result['backup']}）"
        )
    else:
        print("预览模式，未写入；加 --yes 落盘。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
