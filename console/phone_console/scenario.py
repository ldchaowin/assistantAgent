"""Deterministic scenario runner for The Hand + The Eye.

A scenario is JSON: a list of steps such as ``wait_for_text``, ``tap_text``,
``swipe``, ``key`` and ``sleep``. The runner works against any :class:`Phone`,
so the same scenario runs on a virtual phone (CI) and a real one.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from .device import DeviceError
from .phone import Phone

if TYPE_CHECKING:
    from .vision.frame import Frame
    from .vision.ocr import OcrEngine, OcrItem

TEXT_ACTIONS = {"wait_for_text", "tap_text", "assert_text"}
ACTION_NAMES = TEXT_ACTIONS | {"tap", "swipe", "key", "text", "sleep"}
SAFE_RETRYABLE = {"BUSY", "RATE_LIMIT"}
DEFAULT_TIMEOUT = 5.0
DEFAULT_RETRIES = 2
DEFAULT_INTERVAL = 0.3
DEFAULT_MIN_SCORE = 0.6
DEFAULT_MODE = "contains"
DEFAULT_RETRY_ON_TIMEOUT = False
MODES = {"contains", "exact", "regex"}


class ScenarioError(ValueError):
    """Invalid scenario file."""


class StepError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str = "",
        detail: dict[str, Any] | None = None,
        attempts: int = 1,
    ) -> None:
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.attempts = attempts
        super().__init__(f"{code}: {message}" if message else code)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()
    return slug or "scenario"


def default_run_dir(name: str, root: str | Path = "runs") -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(root) / f"{stamp}-{slugify(name)}"


def parse_roi(value: Any) -> tuple[int, int, int, int]:
    """Accept ``[x, y, w, h]`` or ``"x,y,w,h"``."""
    if isinstance(value, str):
        parts: list[Any] = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        raise ScenarioError("roi must be [x,y,w,h] or 'x,y,w,h'")
    if len(parts) != 4:
        raise ScenarioError("roi must have 4 values: x,y,w,h")
    try:
        x, y, w, h = (int(part) for part in parts)
    except (TypeError, ValueError) as exc:
        raise ScenarioError("roi values must be integers") from exc
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise ScenarioError("roi needs x,y >= 0 and w,h > 0")
    return x, y, w, h


def _require_int(value: Any, name: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ScenarioError(f"{name} must be >= {minimum}")
    return value


def _require_float(
    value: Any, name: str, minimum: float | None = None, maximum: float | None = None
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScenarioError(f"{name} must be a number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ScenarioError(f"{name} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ScenarioError(f"{name} must be <= {maximum}")
    return number


def _check_mode(value: Any) -> str:
    mode = str(value)
    if mode not in MODES:
        raise ScenarioError(f"mode must be one of {sorted(MODES)}")
    return mode


def _check_common(step: dict[str, Any], index: int) -> None:
    for key in ("timeout", "interval", "min_score", "retries", "mode", "retry_on_timeout"):
        if key not in step:
            continue
        value = step[key]
        if key == "timeout":
            _require_float(value, f"step {index} timeout", minimum=0.0)
        elif key == "interval":
            _require_float(value, f"step {index} interval", minimum=0.0)
        elif key == "min_score":
            _require_float(value, f"step {index} min_score", minimum=0.0, maximum=1.0)
        elif key == "retries":
            _require_int(value, f"step {index} retries", minimum=0)
        elif key == "retry_on_timeout":
            if not isinstance(value, bool):
                raise ScenarioError(f"step {index} retry_on_timeout must be a boolean")
        else:
            _check_mode(value)
    if "roi" in step:
        step["roi"] = parse_roi(step["roi"])


def _normalize_step(step: Any, index: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise ScenarioError(f"step {index} must be an object")
    action = step.get("action")
    if not isinstance(action, str) or action not in ACTION_NAMES:
        raise ScenarioError(
            f"step {index} has unknown action {action!r}; expected one of {sorted(ACTION_NAMES)}"
        )
    normalized = dict(step)
    _check_common(normalized, index)

    if action in TEXT_ACTIONS:
        text = normalized.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ScenarioError(f"step {index} {action} needs a non-empty text")
    elif action == "swipe":
        for key in ("x1", "y1", "x2", "y2"):
            normalized[key] = _require_int(normalized.get(key), f"step {index} {key}")
        normalized["ms"] = _require_int(
            normalized.get("ms", 400), f"step {index} ms", minimum=50
        )
        if normalized["ms"] > 5000:
            raise ScenarioError(f"step {index} ms must be <= 5000")
    elif action == "key":
        code = normalized.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ScenarioError(f"step {index} key needs a non-empty code")
    elif action == "text":
        value = normalized.get("value")
        if not isinstance(value, str) or not value:
            raise ScenarioError(f"step {index} text needs a non-empty value")
    elif action == "tap":
        normalized["x"] = _require_int(normalized.get("x"), f"step {index} x", minimum=0)
        normalized["y"] = _require_int(normalized.get("y"), f"step {index} y", minimum=0)
        if "hold_ms" in normalized:
            normalized["hold_ms"] = _require_int(
                normalized["hold_ms"], f"step {index} hold_ms", minimum=0
            )
    elif action == "sleep":
        normalized["ms"] = _require_int(
            normalized.get("ms"), f"step {index} ms", minimum=0
        )
        if normalized["ms"] > 600_000:
            raise ScenarioError(f"step {index} ms must be <= 600000")
    return normalized


def _normalize_defaults(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ScenarioError("defaults must be an object")
    defaults = {
        "timeout": DEFAULT_TIMEOUT,
        "retries": DEFAULT_RETRIES,
        "interval": DEFAULT_INTERVAL,
        "min_score": DEFAULT_MIN_SCORE,
        "mode": DEFAULT_MODE,
        "retry_on_timeout": DEFAULT_RETRY_ON_TIMEOUT,
    }
    if "timeout" in raw:
        defaults["timeout"] = _require_float(raw["timeout"], "defaults.timeout", minimum=0.0)
    if "retries" in raw:
        defaults["retries"] = _require_int(raw["retries"], "defaults.retries", minimum=0)
    if "interval" in raw:
        defaults["interval"] = _require_float(
            raw["interval"], "defaults.interval", minimum=0.0
        )
    if "min_score" in raw:
        defaults["min_score"] = _require_float(
            raw["min_score"], "defaults.min_score", minimum=0.0, maximum=1.0
        )
    if "mode" in raw:
        defaults["mode"] = _check_mode(raw["mode"])
    if "retry_on_timeout" in raw:
        if not isinstance(raw["retry_on_timeout"], bool):
            raise ScenarioError("defaults.retry_on_timeout must be a boolean")
        defaults["retry_on_timeout"] = raw["retry_on_timeout"]
    return defaults


@dataclass
class Scenario:
    name: str
    steps: list[dict[str, Any]]
    defaults: dict[str, Any]
    description: str = ""
    screen: tuple[int, int] | None = None
    frames_path: Path | None = None
    path: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Scenario":
        source = Path(path)
        if not source.is_file():
            raise ScenarioError(f"scenario not found: {source}")
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ScenarioError(f"invalid JSON in {source}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ScenarioError("scenario must be a JSON object")
        steps_raw = raw.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ScenarioError("scenario needs a non-empty steps array")
        steps = [_normalize_step(step, index) for index, step in enumerate(steps_raw, 1)]
        defaults = _normalize_defaults(raw.get("defaults"))

        screen: tuple[int, int] | None = None
        if "screen" in raw:
            value = raw["screen"]
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise ScenarioError("screen must be [width, height]")
            screen = (
                _require_int(value[0], "screen width", minimum=1),
                _require_int(value[1], "screen height", minimum=1),
            )

        frames_path: Path | None = None
        if raw.get("frames"):
            if not isinstance(raw["frames"], str):
                raise ScenarioError("frames must be a directory path")
            frames_path = (source.parent / raw["frames"]).resolve()

        name = raw.get("name") or source.stem
        if not isinstance(name, str) or not name.strip():
            raise ScenarioError("name must be a non-empty string")

        return cls(
            name=name,
            steps=steps,
            defaults=defaults,
            description=str(raw.get("description", "")),
            screen=screen,
            frames_path=frames_path,
            path=source.resolve(),
            raw=raw,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "screen": list(self.screen) if self.screen else None,
            "frames": str(self.frames_path) if self.frames_path else None,
            "defaults": self.defaults,
            "steps": self.steps,
        }


@dataclass
class StepResult:
    index: int
    action: str
    status: str
    attempts: int
    elapsed_ms: float
    params: dict[str, Any]
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "status": self.status,
            "attempts": self.attempts,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "params": self.params,
            "detail": self.detail,
            "error": self.error,
        }


@dataclass
class RunResult:
    name: str
    ok: bool
    steps: list[StepResult]
    elapsed_ms: float
    run_dir: str | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "run_dir": self.run_dir,
            "error": self.error,
            "warnings": self.warnings,
            "steps": [step.to_dict() for step in self.steps],
        }


class ScenarioRunner:
    def __init__(
        self,
        phone: Phone,
        scenario: Scenario,
        engine: "OcrEngine | None" = None,
        run_dir: str | Path | None = None,
        output: Callable[[str], None] | None = print,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.phone = phone
        self.scenario = scenario
        self.engine = engine
        self.run_dir = Path(run_dir) if run_dir else None
        self.output = output
        self.clock = clock
        self.sleep = sleep
        self._report: Any = None
        self._last_frame: "Frame | None" = None
        self._last_items: list["OcrItem"] = []
        self._last_ocr_ms = 0.0

    # ------------------------------------------------------------------ public
    def run(self) -> RunResult:
        started = self.clock()
        if self.run_dir is not None:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / "scenario.json").write_text(
                json.dumps(self.scenario.raw, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._report = (self.run_dir / "report.jsonl").open("w", encoding="utf-8")

        total = len(self.scenario.steps)
        self._emit(f"scenario: {self.scenario.name} ({total} steps)")
        if self.run_dir is not None:
            self._emit(f"run dir: {self.run_dir}")

        results: list[StepResult] = []
        warnings: list[str] = []
        error: str | None = None
        try:
            try:
                self.phone.hello()
            except Exception as exc:  # noqa: BLE001 - reported as run failure
                raise StepError("HELLO_FAILED", str(exc)) from exc
            if self.scenario.screen and tuple(self.phone.screen) != self.scenario.screen:
                warning = (
                    f"scenario screen {self.scenario.screen} != device screen "
                    f"{tuple(self.phone.screen)}; using device size for mapping"
                )
                warnings.append(warning)
                self._emit(f"WARN: {warning}")

            for index, step in enumerate(self.scenario.steps, 1):
                result = self._run_step(index, step)
                results.append(result)
                self._write_report({"type": "step", **result.to_dict()})
                status = "ok" if result.status == "ok" else "FAILED"
                self._emit(
                    f"[{index}/{total}] {result.action:<14} {status} "
                    f"({result.attempts} attempt(s), {result.elapsed_ms:.0f} ms)"
                )
                if result.status != "ok":
                    error = result.error
                    break
        except Exception as exc:  # noqa: BLE001 - runner must report, not crash
            error = str(exc)
            detail = getattr(exc, "detail", {})
            if not isinstance(detail, dict):
                detail = {}
            self._emit(f"ERROR: {error}")
            self._write_report(
                {
                    "type": "error",
                    "code": getattr(exc, "code", type(exc).__name__),
                    "error": error,
                    "detail": detail,
                    "traceback": traceback.format_exc(),
                }
            )
        finally:
            elapsed_ms = (self.clock() - started) * 1000.0
            ok = error is None and bool(results) and all(
                step.status == "ok" for step in results
            )
            failed = sum(1 for step in results if step.status != "ok")
            self._write_report(
                {
                    "type": "summary",
                    "name": self.scenario.name,
                    "ok": ok,
                    "steps": len(results),
                    "failed": failed,
                    "elapsed_ms": round(elapsed_ms, 2),
                    "error": error,
                    "warnings": warnings,
                }
            )
            if self._report is not None:
                self._report.close()
                self._report = None
            self._emit(
                f"{'PASS' if ok else 'FAIL'} {len(results) - failed}/{total} "
                f"in {elapsed_ms / 1000.0:.2f}s"
            )
            if self.run_dir is not None:
                self._emit(f"report: {self.run_dir / 'report.jsonl'}")

        return RunResult(
            name=self.scenario.name,
            ok=error is None and bool(results) and all(
                step.status == "ok" for step in results
            ),
            steps=results,
            elapsed_ms=(self.clock() - started) * 1000.0,
            run_dir=str(self.run_dir) if self.run_dir else None,
            error=error,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ steps
    def _run_step(self, index: int, step: dict[str, Any]) -> StepResult:
        self._last_frame = None
        self._last_items = []
        self._last_ocr_ms = 0.0
        started = self.clock()
        action = step["action"]
        attempts = 0
        try:
            if action == "tap_text":
                detail, attempts = self._tap_text(step)
            elif action in {"wait_for_text", "assert_text"}:
                detail, attempts = self._wait_text(step, action)
            elif action == "tap":
                detail, attempts = self._device_step(
                    step,
                    lambda: self.phone.tap(
                        int(step["x"]), int(step["y"]), step.get("hold_ms")
                    ),
                )
            elif action == "swipe":
                detail, attempts = self._device_step(
                    step,
                    lambda: self.phone.swipe(
                        int(step["x1"]),
                        int(step["y1"]),
                        int(step["x2"]),
                        int(step["y2"]),
                        int(step.get("ms", 400)),
                    ),
                )
            elif action == "key":
                detail, attempts = self._device_step(
                    step, lambda: self.phone.key(str(step["code"]))
                )
            elif action == "text":
                detail, attempts = self._device_step(
                    step, lambda: self.phone.text(str(step["value"]))
                )
            elif action == "sleep":
                ms = int(step["ms"])
                self.sleep(ms / 1000.0)
                detail, attempts = {"ms": ms}, 1
            else:  # pragma: no cover - loader rejects unknown actions
                raise StepError("BAD_ACTION", action)

            elapsed_ms = (self.clock() - started) * 1000.0
            return StepResult(
                index=index,
                action=action,
                status="ok",
                attempts=attempts,
                elapsed_ms=elapsed_ms,
                params=self._params(step),
                detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 - one failed step must not crash
            elapsed_ms = (self.clock() - started) * 1000.0
            attempts = int(getattr(exc, "attempts", attempts or 1))
            code = getattr(exc, "code", type(exc).__name__)
            message = str(exc)
            detail = getattr(exc, "detail", {})
            if not isinstance(detail, dict):
                detail = {}
            self._save_failure(index, action, code, message, detail)
            return StepResult(
                index=index,
                action=action,
                status="failed",
                attempts=attempts,
                elapsed_ms=elapsed_ms,
                params=self._params(step),
                detail=detail,
                error=message,
            )

    def _tap_text(self, step: dict[str, Any]) -> tuple[dict[str, Any], int]:
        text = str(step["text"])
        defaults = self.scenario.defaults
        retries = int(step.get("retries", defaults["retries"]))
        interval = float(step.get("interval", defaults["interval"]))
        locator = self._locator(step)
        roi = step.get("roi")
        attempts = retries + 1
        last_error: StepError | None = None

        for attempt in range(1, attempts + 1):
            frame, items = self._see(roi)
            match = locator.find(items, text, frame.size)
            if match is None:
                last_error = StepError(
                    "TEXT_NOT_FOUND",
                    f"text not found: {text!r}",
                    {"frame": list(frame.size), "roi": list(roi) if roi else None},
                    attempts=attempt,
                )
                if attempt < attempts:
                    self.sleep(interval)
                    continue
                raise last_error
            try:
                response = self.phone.tap(
                    *match.device_center, hold_ms=step.get("hold_ms")
                )
            except DeviceError as exc:
                if exc.code in self._retryable(step) and attempt < attempts:
                    last_error = StepError(
                        exc.code, str(exc), {"device_error": exc.code}, attempts=attempt
                    )
                    self.sleep(interval)
                    continue
                raise StepError(
                    exc.code, str(exc), {"device_error": exc.code}, attempts=attempt
                ) from exc
            except OSError as exc:
                raise StepError("DISCONNECT", str(exc), attempts=attempt) from exc
            return (
                {
                    "text": match.text,
                    "score": round(match.score, 4),
                    "center": [round(match.center[0], 1), round(match.center[1], 1)],
                    "device_center": list(match.device_center),
                    "ocr_ms": round(self._last_ocr_ms, 2),
                    "tap": response,
                },
                attempt,
            )

        raise last_error or StepError("TEXT_NOT_FOUND", text, attempts=attempts)

    def _wait_text(
        self, step: dict[str, Any], action: str
    ) -> tuple[dict[str, Any], int]:
        text = str(step["text"])
        defaults = self.scenario.defaults
        timeout = float(step.get("timeout", defaults["timeout"]))
        interval = float(step.get("interval", defaults["interval"]))
        locator = self._locator(step)
        roi = step.get("roi")
        deadline = self.clock() + timeout
        attempt = 0

        while True:
            attempt += 1
            frame, items = self._see(roi)
            match = locator.find(items, text, frame.size)
            if match is not None:
                return (
                    {
                        "text": match.text,
                        "score": round(match.score, 4),
                        "center": [
                            round(match.center[0], 1),
                            round(match.center[1], 1),
                        ],
                        "device_center": list(match.device_center),
                        "ocr_ms": round(self._last_ocr_ms, 2),
                    },
                    attempt,
                )
            remaining = deadline - self.clock()
            if remaining <= 0:
                code = "ASSERT_FAILED" if action == "assert_text" else "TEXT_TIMEOUT"
                raise StepError(
                    code,
                    f"{text!r} not found within {timeout:.1f}s",
                    {
                        "frame": list(frame.size),
                        "roi": list(roi) if roi else None,
                        "ocr_items": len(items),
                    },
                    attempts=attempt,
                )
            self.sleep(min(interval, remaining))

    def _device_step(
        self, step: dict[str, Any], action: Callable[[], dict]
    ) -> tuple[dict[str, Any], int]:
        defaults = self.scenario.defaults
        retries = int(step.get("retries", defaults["retries"]))
        interval = float(step.get("interval", defaults["interval"]))
        attempts = retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return {"response": action()}, attempt
            except DeviceError as exc:
                if exc.code in self._retryable(step) and attempt < attempts:
                    self.sleep(interval)
                    continue
                raise StepError(
                    exc.code, str(exc), {"device_error": exc.code}, attempts=attempt
                ) from exc
            except OSError as exc:
                raise StepError("DISCONNECT", str(exc), attempts=attempt) from exc
        raise StepError("DEVICE_ERROR", "action failed", attempts=attempts)

    # ------------------------------------------------------------------ helpers
    def _retryable(self, step: dict[str, Any]) -> set[str]:
        codes = set(SAFE_RETRYABLE)
        enabled = step.get(
            "retry_on_timeout", self.scenario.defaults.get("retry_on_timeout", False)
        )
        if enabled:
            codes.add("TIMEOUT")
        return codes

    def _locator(self, step: dict[str, Any]):
        try:
            from .vision.locate import TextLocator
        except ImportError as exc:
            raise RuntimeError(
                "vision dependencies are missing; install the vision extras:\n"
                "  .venv/bin/pip install -e 'console[vision]'"
            ) from exc
        defaults = self.scenario.defaults
        return TextLocator(
            screen=self.phone.screen,
            min_score=float(step.get("min_score", defaults["min_score"])),
            mode=str(step.get("mode", defaults["mode"])),
        )

    def _engine(self) -> "OcrEngine":
        if self.engine is None:
            try:
                from .vision.ocr import create_engine
            except ImportError as exc:
                raise RuntimeError(
                    "vision dependencies are missing; install the vision extras:\n"
                    "  .venv/bin/pip install -e 'console[vision]'"
                ) from exc
            self.engine = create_engine()
        return self.engine

    def _see(self, roi: Any) -> tuple["Frame", list["OcrItem"]]:

        frame = self.phone.grab()
        self._last_frame = frame
        ocr_frame = frame.crop(roi) if roi else frame
        items = self._engine().recognize(ocr_frame.image)
        if roi:
            from .vision.ocr import offset_items

            items = offset_items(items, roi[0], roi[1])
        self._last_items = items
        self._last_ocr_ms = self._engine().last_elapsed_ms
        return frame, items

    @staticmethod
    def _params(step: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in step.items() if key != "action"}

    def _emit(self, message: str) -> None:
        if self.output is not None:
            self.output(message)

    def _write_report(self, record: dict[str, Any]) -> None:
        if self._report is None:
            return
        self._report.write(
            json.dumps(record, ensure_ascii=False, default=str) + "\n"
        )
        self._report.flush()

    def _save_failure(
        self,
        index: int,
        action: str,
        code: str,
        message: str,
        detail: dict[str, Any],
    ) -> None:
        if self.run_dir is None:
            return
        try:
            payload = {
                "step": index,
                "action": action,
                "code": code,
                "error": message,
                "detail": detail,
            }
            (self.run_dir / f"failure-{index:02d}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if self._last_frame is not None:
                self._last_frame.save(self.run_dir / f"failure-{index:02d}.png")
                try:
                    from .vision.overlay import annotate

                    annotate(
                        self._last_frame,
                        self._last_items,
                        self.run_dir / f"failure-{index:02d}-annotated.png",
                    )
                except Exception:  # noqa: BLE001 - overlay is best effort
                    pass
        except Exception:  # noqa: BLE001 - artifacts are best effort
            pass
