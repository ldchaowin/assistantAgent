"""BLE provisioning for the ESP32 hand (Espressif Network Provisioner).

The firmware advertises ``PROV_XXXX`` (last four chars of the device id) and
accepts WiFi credentials over BLE using protocomm security 1 (X25519 + PoP).
The client is Espressif's official ``esp_prov`` tool, vendored under
``vendor/esp_prov`` (Apache-2.0) together with the generated protobuf modules.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
ESP_PROV = VENDOR_DIR / "esp_prov" / "esp_prov.py"

SUCCESS_MARKER = "Provisioning was successful"
FAILURE_MARKER = "Provisioning failed"


class ProvisionError(RuntimeError):
    """Provisioning failed, timed out, or the client is missing."""


def device_name_for(device_id: str) -> str:
    """BLE name advertised by the firmware for a given device id."""
    text = str(device_id or "").strip()
    if not text:
        raise ValueError("device_id must not be empty")
    return f"PROV_{text[-4:]}"


def default_pop(device_id: str) -> str:
    """Proof of possession used by the firmware (the full device id)."""
    return str(device_id)


def pop_from_device_name(name: str) -> str:
    """Best-effort PoP for ``PROV_XXXX`` when the full device id is unknown."""
    text = str(name or "").strip()
    suffix = text.split("_", 1)[-1] if text.startswith("PROV_") else text
    return f"hand-{suffix}"


def scan_devices(
    timeout: float = 6.0, prefix: str | None = "PROV_"
) -> list[dict[str, Any]]:
    """Scan BLE for provisioning-capable devices."""
    try:
        from bleak import BleakScanner
    except ImportError as exc:  # pragma: no cover - depends on env
        raise ProvisionError(
            "bleak is not installed; install the provisioning extras:\n"
            "  .venv/bin/pip install -e 'console[provision]'"
        ) from exc

    async def _scan() -> list[dict[str, Any]]:
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        devices: list[dict[str, Any]] = []
        for device, adv in found.values():
            name = device.name or getattr(adv, "local_name", None) or ""
            if prefix and not name.startswith(prefix):
                continue
            devices.append(
                {
                    "name": name,
                    "address": device.address,
                    "rssi": getattr(adv, "rssi", None),
                }
            )
        devices.sort(key=lambda item: item["name"])
        return devices

    return asyncio.run(_scan())


def _parse_output(output: str) -> bool:
    if FAILURE_MARKER in output:
        return False
    return SUCCESS_MARKER in output


def provision(
    ssid: str,
    password: str,
    device: str | None = None,
    pop: str | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Run the official client against one device and return a result dict."""
    if not ssid:
        raise ValueError("ssid must not be empty")
    if pop is None and device:
        # Security 1 always needs a PoP; default to the device id so a
        # non-interactive caller never falls into esp_prov's getpass prompt.
        pop = pop_from_device_name(device)
    if not ESP_PROV.is_file():  # pragma: no cover - vendored file is present
        raise ProvisionError(f"vendored esp_prov not found: {ESP_PROV}")

    command = [
        sys.executable,
        str(ESP_PROV),
        "--transport",
        "ble",
        "--ssid",
        str(ssid),
        "--passphrase",
        str(password),
        "--sec_ver",
        "1",
    ]
    if pop:
        command += ["--pop", str(pop)]
    if device:
        command += ["--service_name", str(device)]

    environment = dict(os.environ)
    environment.setdefault("PYTHONUNBUFFERED", "1")
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            cwd=str(ESP_PROV.parent),
        )
    except subprocess.TimeoutExpired as exc:
        raise ProvisionError(
            f"BLE provisioning timed out after {timeout:.0f}s "
            f"(device={device or 'auto'})"
        ) from exc

    output = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0 and _parse_output(output)
    return {
        "ok": ok,
        "device": device,
        "ssid": ssid,
        "returncode": proc.returncode,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
        "output": output.strip(),
    }
