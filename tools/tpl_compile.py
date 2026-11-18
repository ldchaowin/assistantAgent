#!/usr/bin/env python3
"""Compile a task template (.md) into a runnable scenario (.json).

    tools/tpl_compile.py templates/jd-price-protect.md --check
    tools/tpl_compile.py templates/jd-price-protect.md --out scenarios/generated/x.json
    tools/tpl_compile.py templates/game-daily.md --screen 1264x2800 --json
    tools/tpl_compile.py templates/jd-price-protect.md --stop-before s9-tap-apply

Coordinates are normalized 0-1, so --screen only changes the compiled pixel
values. If the template's coordinates are unknown or older than
coords_ttl_days, tap steps are compiled as tap_text (slow but correct) unless
--force-coords is given.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from phone_console.template import (
    TemplateError,
    compile_scenario,
    coord_state,
    parse_template,
    px,
    validate_steps,
)


def parse_screen(value: str) -> tuple[int, int]:
    import re

    match = re.fullmatch(r"(\d+)x(\d+)", value.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError("screen must be WIDTHxHEIGHT, e.g. 1080x2400")
    return int(match.group(1)), int(match.group(2))


def summarize(template, screen, scenario) -> str:
    state = coord_state(template.meta)
    lines = [
        f"{template.id}  v{template.version}  [{template.meta.get('status', 'draft')}]",
        f"  name   : {template.meta.get('name', '')}",
        f"  screen : {screen[0]}x{screen[1]}  frames: {template.meta.get('frames') or '-'}",
        f"  coords : {'可用' if state['usable'] else '不可用'} — {state['reason']}",
        f"  steps  : {len(template.steps)}"
        + (f" -> {len(scenario['steps'])} compiled" if scenario else ""),
    ]
    if scenario:
        for step, source in zip(scenario["steps"], template.steps):
            sid = step["id"]
            if step["action"] == "tap":
                lines.append(f"    {sid:<22} tap    ({step['x']}, {step['y']})")
            elif step["action"] == "tap_text":
                lines.append(f"    {sid:<22} tap_text {step['text']!r}")
            elif step["action"] == "assert_text":
                lines.append(f"    {sid:<22} expect {step['text']!r} timeout={step['timeout']}s")
            else:
                lines.append(f"    {sid:<22} {step['action']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile a task template into a scenario")
    parser.add_argument("template", type=Path)
    parser.add_argument("--out", type=Path, help="write scenario JSON here")
    parser.add_argument("--screen", type=parse_screen, help="target screen, e.g. 1264x2800")
    parser.add_argument("--stop-before", metavar="ID", help="drop this step and everything after")
    parser.add_argument("--check", action="store_true", help="validate only, write nothing")
    parser.add_argument("--json", action="store_true", help="print the scenario JSON")
    parser.add_argument("--force-coords", action="store_true", help="use taps even if coords are stale")
    args = parser.parse_args(argv)

    try:
        template = parse_template(args.template)
        validate_steps(template)
        screen = args.screen or template.screen
        scenario = compile_scenario(
            template,
            screen=screen,
            stop_before=args.stop_before,
            force_coords=args.force_coords,
        )
    except (TemplateError, OSError) as exc:
        print(f"ERROR {args.template}: {exc}", file=sys.stderr)
        return 2

    if not args.json:
        print(summarize(template, screen, scenario if not args.check else scenario))
        for warning in template.warnings:
            print(f"  ! {warning}")

    if args.check:
        print("OK")
        return 0

    payload = json.dumps(scenario, ensure_ascii=False, indent=2)
    if args.json:
        print(payload)
        return 0

    if not args.out:
        parser.error("--out is required unless --check or --json")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload + "\n", encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
