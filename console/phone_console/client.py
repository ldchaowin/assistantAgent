"""Small stdlib client for the local phone control service."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class PhoneClientError(RuntimeError):
    def __init__(self, code: str, message: str = "", status: int = 0) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}" if message else code)


class PhoneClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        data = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                if raw:
                    return payload
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            raw_body = exc.read()
            try:
                payload = json.loads(raw_body)
            except (ValueError, TypeError):
                payload = {}
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            raise PhoneClientError(
                str(error.get("code", f"HTTP_{exc.code}")),
                str(error.get("message", raw_body.decode("utf-8", "replace"))),
                exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise PhoneClientError("UNREACHABLE", str(exc.reason)) from exc

    # ------------------------------------------------------------------ api
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/status")

    def tap(self, x: int, y: int, hold_ms: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"x": int(x), "y": int(y)}
        if hold_ms is not None:
            body["hold_ms"] = int(hold_ms)
        return self._request("POST", "/tap", body)

    def swipe(
        self, x1: int, y1: int, x2: int, y2: int, ms: int = 400
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/swipe",
            {
                "x1": int(x1),
                "y1": int(y1),
                "x2": int(x2),
                "y2": int(y2),
                "ms": int(ms),
            },
        )

    def key(self, code: str) -> dict[str, Any]:
        return self._request("POST", "/key", {"code": str(code)})

    def text(self, value: str) -> dict[str, Any]:
        return self._request("POST", "/text", {"value": str(value)})

    def find(
        self,
        text: str,
        min_score: float = 0.6,
        mode: str = "contains",
        roi: Any = None,
        index: int = 0,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "text": text,
            "min_score": float(min_score),
            "mode": mode,
            "index": int(index),
        }
        if roi is not None:
            body["roi"] = roi
        return self._request("POST", "/find", body)

    def screenshot(
        self,
        path: str | Path | None = None,
        max_width: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"include_base64": True}
        if max_width is not None:
            body["max_width"] = int(max_width)
        payload = self._request("POST", "/screenshot", body)
        if path is not None and payload.get("base64"):
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(payload["base64"]))
            payload["saved_path"] = str(target)
        return payload

    # ------------------------------------------------------------- provisioning
    def provision_devices(self, timeout: float = 8.0) -> dict[str, Any]:
        return self._request("GET", f"/provision/devices?timeout={float(timeout)}")

    def arm_provisioning(self) -> dict[str, Any]:
        return self._request("POST", "/provision/arm", {})

    def provision_status(self) -> dict[str, Any]:
        return self._request("GET", "/provision/status")

    def start_provision(
        self,
        ssid: str,
        password: str,
        device: str | None = None,
        pop: str | None = None,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ssid": str(ssid),
            "password": str(password),
            "timeout": float(timeout),
        }
        if device:
            body["device"] = str(device)
        if pop:
            body["pop"] = str(pop)
        return self._request("POST", "/provision", body)

    def run(self, scenario: str, run_dir: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"scenario": str(scenario)}
        if run_dir is not None:
            body["run_dir"] = str(run_dir)
        return self._request("POST", "/run", body)
