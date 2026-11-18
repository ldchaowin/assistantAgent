#!/usr/bin/env python3
"""Run a task template on the phone, write a trace, and review it.

    # 真机
    tools/tpl_run.py examples/templates/taobao-price-protection.md --port /dev/cu.usbserial-XXXX --adb
    # 只跑到提交前
    tools/tpl_run.py examples/templates/taobao-price-protection.md --port ... --stop-before s9-tap-apply
    # 回放（无硬件）
    tools/tpl_run.py examples/templates/taobao-price-protection.md --mock

Everything after the recognized flags is forwarded to ``phone run``.

Artifacts:

    runs/<id>/<ts>/report.jsonl    runner 原始报告
    runs/<id>/<ts>/trace.jsonl     每步一行，复盘用
    runs/<id>/<ts>/review.md       复盘结论与建议改动
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from phone_console.template import (
    TemplateError,
    analyse_template,
    collect_traces,
    compile_scenario,
    parse_template,
    render_review,
    report_to_trace,
    write_trace,
)
from phone_console.template_apply import apply_proposals, select_safe


def _report_events(report: Path, offset: int, buffer: str) -> tuple[int, str, list[dict]]:
    """Read new report.jsonl lines; returns (offset, partial, events)."""
    events: list[dict] = []
    if not report.is_file():
        return offset, buffer, events
    try:
        with report.open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            chunk = handle.read()
            offset = handle.tell()
    except OSError:
        return offset, buffer, events
    buffer += chunk
    while "\n" in buffer:
        line, buffer = buffer.split("\n", 1)
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") == "step":
            events.append(
                {
                    "event": "step",
                    "index": entry.get("index"),
                    "action": entry.get("action"),
                    "ok": entry.get("status") == "ok",
                    "attempts": entry.get("attempts"),
                    "ms": round(float(entry.get("elapsed_ms") or 0.0), 1),
                    "error": entry.get("error"),
                }
            )
        elif entry.get("type") == "summary":
            events.append(
                {
                    "event": "summary",
                    "ok": bool(entry.get("ok")),
                    "steps": entry.get("steps"),
                    "failed": entry.get("failed"),
                    "ms": round(float(entry.get("elapsed_ms") or 0.0), 1),
                }
            )
    return offset, buffer, events


def phone_binary() -> str:
    candidate = Path(sys.executable).with_name("phone")
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("phone")
    if not found:
        raise SystemExit("找不到 phone 可执行文件（用 .venv/bin/python 运行本脚本）")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a task template, then trace and review it",
        epilog="未知参数会原样转发给 phone run",
    )
    parser.add_argument("template", type=Path)
    parser.add_argument("--stop-before", metavar="ID")
    parser.add_argument("--screen", metavar="WxH", help="target screen override, e.g. 1264x2800")
    parser.add_argument("--force-coords", action="store_true", help="use taps even if coords are stale")
    parser.add_argument("--entry-only", action="store_true", help="只执行到达段（approach + entry 闸门）")
    parser.add_argument("--skip-entry", action="store_true", help="跳过到达段，只执行主路径")
    parser.add_argument("--run-dir", type=Path, help="default runs/<id>/<timestamp>")
    parser.add_argument("--runs", type=int, default=10, help="how many traces the review reads")
    parser.add_argument("--mock", action="store_true", help="in-memory simulator, no hardware")
    parser.add_argument("--port", help="serial port of the hand")
    parser.add_argument("--tcp", help="HOST[:PORT] for the WiFi hand")
    parser.add_argument("--baud", type=int, help="serial baud rate")
    parser.add_argument("--log", help="append phone command log to this path")
    parser.add_argument(
        "--via-service",
        metavar="URL",
        help="通过 phone serve 执行（服务持有设备连接，避免和它抢手）",
    )
    parser.add_argument("--token", help="phone serve bearer token")
    parser.add_argument(
        "--service-timeout",
        type=float,
        default=900.0,
        help="--via-service 的整体超时秒数",
    )
    parser.add_argument("--no-review", action="store_true")
    parser.add_argument(
        "--no-auto-apply",
        action="store_true",
        help="只写复盘，不自动应用保守修正（降级/调大 timeout/放宽 roi）",
    )
    parser.add_argument(
        "--events-json",
        action="store_true",
        help="输出机器可读的事件流（插件用），人类输出被抑制",
    )
    parser.add_argument("--generated", type=Path, help="where to write the compiled scenario")
    args, forwarded = parser.parse_known_args(argv)

    try:
        template = parse_template(args.template)
        screen = tuple(int(v) for v in args.screen.lower().split("x")) if args.screen else None
        scenario = compile_scenario(
            template,
            screen=screen,  # type: ignore[arg-type]
            stop_before=args.stop_before,
            force_coords=args.force_coords,
            include_entry=not args.skip_entry,
            entry_only=args.entry_only,
        )
    except (TemplateError, ValueError, OSError) as exc:
        print(f"ERROR {args.template}: {exc}", file=sys.stderr)
        return 2

    for warning in template.warnings:
        print(f"! {warning}", file=sys.stderr)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = args.run_dir or Path("runs") / template.id / stamp
    if args.run_dir is None:
        # 同一秒跑两次会撞目录，加序号而不是覆盖上一份 trace
        candidate, index = run_dir, 2
        while candidate.exists():
            candidate = run_dir.with_name(f"{run_dir.name}-{index}")
            index += 1
        run_dir = candidate
    generated = args.generated or Path("scenarios/generated") / f"{template.id}.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text(
        __import__("json").dumps(scenario, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    global_flags: list[str] = []
    if args.mock:
        global_flags.append("--mock")
    if args.port:
        global_flags += ["--port", str(args.port)]
    if args.tcp:
        global_flags += ["--tcp", str(args.tcp)]
    if args.baud:
        global_flags += ["--baud", str(args.baud)]
    if args.log:
        global_flags += ["--log", str(args.log)]

    command = [
        phone_binary(),
        *global_flags,
        "run",
        str(generated),
        "--run-dir",
        str(run_dir),
        *forwarded,
    ]
    events_mode = bool(args.events_json)

    def emit(payload: dict) -> None:
        if events_mode:
            print(json.dumps(payload, ensure_ascii=False), flush=True)

    def say(message: str) -> None:
        if not events_mode:
            print(message)

    if args.via_service:
        say(f"$ phone serve {args.via_service} (scenario: {generated})")
    else:
        say("$ " + " ".join(command))
    emit(
        {
            "event": "start",
            "template": template.id,
            "version": template.version,
            "runDir": str(run_dir.resolve()),
            "total": len(scenario["steps"]),
            "degraded": bool(template.warnings),
        }
    )

    report = run_dir / "report.jsonl"
    if args.via_service:
        from phone_console.client import PhoneClient, PhoneClientError

        client = PhoneClient(args.via_service, args.token or "", timeout=args.service_timeout)
        outcome: dict = {}

        def _post_run() -> None:
            try:
                # The service runs with its own cwd: send absolute paths.
                outcome["result"] = client.run(str(generated.resolve()), str(run_dir.resolve()))
                outcome["ok"] = True
            except Exception as exc:  # noqa: BLE001 - surfaced below
                outcome["error"] = exc

        thread = threading.Thread(target=_post_run, name="tpl-service-run", daemon=True)
        thread.start()
        offset = 0
        partial = ""
        index = 0
        while thread.is_alive():
            offset, partial, events = _report_events(report, offset, partial)
            for event in events:
                if event.get("event") == "step":
                    index += 1
                    event["index"] = index
                    event["total"] = len(scenario["steps"])
                emit(event)
            time.sleep(0.05)
        thread.join()
        offset, partial, events = _report_events(report, offset, partial)
        for event in events:
            if event.get("event") == "step":
                index += 1
                event["index"] = index
                event["total"] = len(scenario["steps"])
            emit(event)

        if "error" in outcome:
            error = outcome["error"]
            code = getattr(error, "code", type(error).__name__)
            emit({"event": "error", "message": f"{code}: {error}"})
            return 1
        result = outcome.get("result") or {}
        if not report.is_file():
            emit({"event": "error", "message": f"没有 {report}"})
            return 1
        completed = subprocess.CompletedProcess(command, 0 if result.get("ok") else 1, "", "")
    elif events_mode:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        offset = 0
        partial = ""
        index = 0
        try:
            while process.poll() is None:
                offset, partial, events = _report_events(report, offset, partial)
                for event in events:
                    if event.get("event") == "step":
                        index += 1
                        event["index"] = index
                        event["total"] = len(scenario["steps"])
                    emit(event)
                time.sleep(0.05)
            process.wait()
            offset, partial, events = _report_events(report, offset, partial)
            for event in events:
                if event.get("event") == "step":
                    index += 1
                    event["index"] = index
                    event["total"] = len(scenario["steps"])
                emit(event)
        finally:
            stderr_tail = ""
            try:
                stderr_tail = (process.stderr.read() or "") if process.stderr else ""
            except Exception:  # noqa: BLE001 - best effort
                stderr_tail = ""
        completed = subprocess.CompletedProcess(command, process.returncode, "", stderr_tail)
        if not report.is_file():
            emit({"event": "error", "message": f"没有 {report}"})
            return completed.returncode
    else:
        completed = subprocess.run(command)
        if not report.is_file():
            print(f"! 没有 {report}，跳过 trace 与复盘", file=sys.stderr)
            return completed.returncode

    header = {
        "id": template.id,
        "v": template.version,
        "ts": stamp,
        "screen": list(screen or template.screen),
        "device": template.meta.get("device"),
        "coords": "usable" if not template.warnings else "degraded",
        "cmd": global_flags + forwarded,
    }
    records = report_to_trace(report, header)
    write_trace(run_dir / "trace.jsonl", records)

    done = next((r for r in records if r.get("t") == "done"), {})
    say(f"trace: {run_dir / 'trace.jsonl'}  ({done.get('steps', '?')} steps, {done.get('ms', 0):.0f} ms)")

    if not args.no_review and not args.entry_only:
        traces = collect_traces(Path("runs") / template.id, limit=args.runs)
        analysis = analyse_template(template, traces)
        (run_dir / "review.md").write_text(render_review(analysis), encoding="utf-8")
        (run_dir / "review.json").write_text(
            json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        say(f"review: {run_dir / 'review.md'} (+ review.json)")
        emit(
            {
                "event": "review",
                "proposals": len(analysis.get("proposals") or []),
                "samples": analysis.get("samples"),
                "reviewPath": str((run_dir / "review.md").resolve()),
            }
        )

        # Templates are only tuned after a SUCCESSFUL run. A failure means the
        # path no longer holds (page moved, app changed); relaxing timeouts or
        # ROI would just hide the real cause and poison the template.
        run_ok = completed.returncode == 0
        safe = select_safe(analysis["proposals"]) if run_ok else []
        if not run_ok and analysis["proposals"]:
            say("  本次执行失败：只保留复盘建议，不改模板（等人工确认）")
        if safe and not args.no_auto_apply:
            result = apply_proposals(template.path, safe, dry_run=False)
            for item in result["applied"]:
                say(f"  自动修正：{item['id']} <- {item['kind']}")
            for item in result["skipped"]:
                say(f"  跳过：{item['id']}({item.get('kind')})：{item['reason']}")
            if result["written"]:
                say(
                    f"  模板已更新：v{result['version']} -> v{result['new_version']}"
                    f"（备份 {result['backup']}）"
                )
            emit(
                {
                    "event": "applied",
                    "applied": result["applied"],
                    "skipped": result["skipped"],
                    "version": result["version"],
                    "newVersion": result["new_version"],
                    "backup": result["backup"],
                }
            )
        elif safe:
            say(f"  有 {len(safe)} 条保守修正未应用（--no-auto-apply）")

    emit(
        {
            "event": "done",
            "ok": completed.returncode == 0,
            "runDir": str(run_dir.resolve()),
            "tracePath": str((run_dir / "trace.jsonl").resolve()),
            "reviewPath": str((run_dir / "review.md").resolve())
            if not args.no_review and not args.entry_only
            else None,
            "steps": done.get("steps"),
            "ms": round(float(done.get("ms") or 0.0), 1),
        }
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
