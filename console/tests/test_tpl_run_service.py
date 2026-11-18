"""`tpl_run --via-service` runs through phone serve instead of owning the hand."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "tpl_run.py"


def write_template(tmp_path: Path) -> Path:
    path = tmp_path / "demo.md"
    path.write_text(
        """---
id: demo
name: 冒烟模板
version: 1
screen: [1080, 2400]
coords_known: true
coords_updated: 2026-11-17
---

## 步骤

```yaml
steps:
  - {id: s1, do: key, code: BACK}
  - {id: s2, do: expect, text: 桌面, timeout: 3}
```
""",
        encoding="utf-8",
    )
    return path


class StubService(BaseHTTPRequestHandler):
    steps = 3

    def log_message(self, *args):  # noqa: D401 - silence test output
        return

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/run":
            self.send_response(404)
            self.end_headers()
            return
        run_dir = Path(body["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"type": "step", "index": i, "action": "tap", "status": "ok", "attempts": 1, "elapsed_ms": 10.0})
            for i in range(1, self.steps + 1)
        ]
        lines.append(json.dumps({"type": "summary", "ok": True, "steps": self.steps, "failed": 0, "elapsed_ms": 30.0}))
        (run_dir / "report.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        payload = json.dumps({"ok": True, "name": "jd-price-protect", "run_dir": str(run_dir)}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_via_service_streams_events_and_returns_ok(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubService)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    template = write_template(tmp_path)
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                str(template),
                "--events-json",
                "--via-service",
                f"http://127.0.0.1:{server.server_port}",
                "--no-review",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    kinds = [event["event"] for event in events]
    assert kinds[0] == "start"
    assert kinds.count("step") == 3
    assert kinds[-1] == "done"
    assert events[-1]["ok"] is True
