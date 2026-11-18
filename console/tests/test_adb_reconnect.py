"""The Eye reconnect tool: mDNS discovery, saved endpoint, clear failures."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "adb_reconnect.py"

OK_ADB = """#!/bin/sh
case "$1" in
  mdns)
    echo "List of discovered mdns services"
    echo "adb-TEST	_adb-tls-connect._tcp	10.0.0.5:41000"
    ;;
  connect)
    echo "connected to $2"
    ;;
  devices)
    echo "List of devices attached"
    echo "10.0.0.5:41000	device"
    ;;
  *)
    echo ""
    ;;
esac
"""

FAIL_ADB = """#!/bin/sh
case "$1" in
  mdns)
    echo "List of discovered mdns services"
    echo "adb-TEST	_adb-tls-connect._tcp	10.0.0.5:41000"
    ;;
  connect)
    echo "failed to connect to $2"
    exit 1
    ;;
  devices)
    echo "List of devices attached"
    ;;
  *)
    echo ""
    ;;
esac
"""


def run_tool(tmp_path, script, *args):
    adb = tmp_path / "adb"
    adb.write_text(script, encoding="utf-8")
    adb.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ, PHONE_ADB=str(adb), HOME=str(home))
    result = subprocess.run(
        [sys.executable, str(TOOL), "--json", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return result


def test_reconnects_via_mdns_and_remembers_endpoint(tmp_path):
    result = run_tool(tmp_path, OK_ADB)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["endpoint"] == "10.0.0.5:41000"
    saved = tmp_path / "home" / ".dsh" / "phone-service" / "adb-endpoint.json"
    assert json.loads(saved.read_text())["endpoint"] == "10.0.0.5:41000"


def test_failure_names_the_real_problem(tmp_path):
    result = run_tool(tmp_path, FAIL_ADB)
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload.get("endpoint") in (None, "10.0.0.5:41000")
    # The port is reachable (the fake adb does not open it), so at minimum the
    # hint must tell the user what to do next.
    assert "无线调试" in payload["hint"] or "USB" in payload["hint"]
