"""Guided HID calibration: turn observed corner positions into a new cal range."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .phone import Phone

HID_MAX = 32767


@dataclass
class CalibrationSample:
    name: str
    target_x: int
    target_y: int
    observed_x: int
    observed_y: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": [self.target_x, self.target_y],
            "observed": [self.observed_x, self.observed_y],
        }


@dataclass
class CalibrationResult:
    screen: tuple[int, int]
    old_cal: tuple[int, int, int, int]
    new_cal: tuple[int, int, int, int]
    samples: list[CalibrationSample] = field(default_factory=list)
    valid: bool = True
    message: str = ""
    saved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "screen": list(self.screen),
            "old_cal": list(self.old_cal),
            "new_cal": list(self.new_cal),
            "valid": self.valid,
            "message": self.message,
            "saved": self.saved,
            "samples": [sample.to_dict() for sample in self.samples],
        }


def map_hid(target: float, screen_size: int, cal_lo: int, cal_hi: int) -> float:
    """Firmware's map: lo + target * (hi - lo) / screen_size."""
    if screen_size <= 0:
        raise ValueError("screen size must be positive")
    return cal_lo + target * (cal_hi - cal_lo) / screen_size


def solve_axis(
    target1: float,
    observed1: float,
    target2: float,
    observed2: float,
    screen_size: int,
    cal_lo: int,
    cal_hi: int,
) -> tuple[int, int]:
    """Invert the observed HID->screen line and return a new (lo, hi)."""
    if screen_size <= 0:
        raise ValueError("screen size must be positive")
    h1 = map_hid(target1, screen_size, cal_lo, cal_hi)
    h2 = map_hid(target2, screen_size, cal_lo, cal_hi)
    if abs(h2 - h1) < 1e-9:
        raise ValueError("calibration targets map to the same HID value")
    if abs(observed2 - observed1) < 1e-9:
        raise ValueError("observed positions are identical; cannot solve")
    a = (observed2 - observed1) / (h2 - h1)
    if abs(a) < 1e-9:
        raise ValueError("observed mapping is degenerate")
    b = observed1 - a * h1
    new_lo = -b / a
    new_hi = (screen_size - b) / a
    return int(round(new_lo)), int(round(new_hi))


def calibration_targets(
    screen: tuple[int, int], margin: float = 0.05
) -> list[dict[str, Any]]:
    if not 0.0 <= margin < 0.5:
        raise ValueError("margin must be in [0, 0.5)")
    width, height = screen
    left = int(round(width * margin))
    right = int(round(width * (1.0 - margin)))
    top = int(round(height * margin))
    bottom = int(round(height * (1.0 - margin)))
    return [
        {"name": "top-left", "x": left, "y": top},
        {"name": "top-right", "x": right, "y": top},
        {"name": "bottom-right", "x": right, "y": bottom},
        {"name": "bottom-left", "x": left, "y": bottom},
        {"name": "center", "x": width // 2, "y": height // 2},
    ]


def solve_calibration(
    screen: tuple[int, int],
    cal: tuple[int, int, int, int],
    samples: list[CalibrationSample],
) -> CalibrationResult:
    width, height = screen
    if width <= 0 or height <= 0:
        raise ValueError("screen must be positive")
    if len(cal) != 4:
        raise ValueError("cal must be 4 values")
    if not samples:
        raise ValueError("at least one sample is required")

    # Edge averages only; a center sample (exactly half) must not skew them.
    left = [s for s in samples if s.target_x < width / 2]
    right = [s for s in samples if s.target_x > width / 2]
    top = [s for s in samples if s.target_y < height / 2]
    bottom = [s for s in samples if s.target_y > height / 2]
    if not left or not right or not top or not bottom:
        raise ValueError("samples must cover left/right/top/bottom")

    def avg(values: list[float]) -> float:
        return sum(values) / len(values)

    target_left_x = avg([s.target_x for s in left])
    target_right_x = avg([s.target_x for s in right])
    observed_left_x = avg([s.observed_x for s in left])
    observed_right_x = avg([s.observed_x for s in right])
    target_top_y = avg([s.target_y for s in top])
    target_bottom_y = avg([s.target_y for s in bottom])
    observed_top_y = avg([s.observed_y for s in top])
    observed_bottom_y = avg([s.observed_y for s in bottom])

    new_x0, new_x1 = solve_axis(
        target_left_x,
        observed_left_x,
        target_right_x,
        observed_right_x,
        width,
        cal[0],
        cal[1],
    )
    new_y0, new_y1 = solve_axis(
        target_top_y,
        observed_top_y,
        target_bottom_y,
        observed_bottom_y,
        height,
        cal[2],
        cal[3],
    )
    new_cal = (new_x0, new_x1, new_y0, new_y1)
    valid = (
        all(0 <= value <= HID_MAX for value in new_cal)
        and new_x0 != new_x1
        and new_y0 != new_y1
    )
    message = "ok" if valid else "computed cal is outside 0..32767 or degenerate"
    return CalibrationResult(
        screen=screen,
        old_cal=cal,
        new_cal=new_cal,
        samples=samples,
        valid=valid,
        message=message,
    )


def _parse_observed(value: str, target_x: int, target_y: int) -> tuple[int, int]:
    value = value.strip()
    if not value:
        return target_x, target_y
    parts = [part.strip() for part in value.replace(" ", ",").split(",") if part.strip()]
    if len(parts) != 2:
        raise ValueError("enter two numbers: observed_x,observed_y (or blank)")
    return int(parts[0]), int(parts[1])


def run_calibration(
    phone: Phone,
    *,
    screen: tuple[int, int] | None = None,
    margin: float = 0.05,
    save: bool = False,
    auto: bool = False,
    yes: bool = False,
    output: Callable[[str], None] | None = print,
    prompt: Callable[[str], str] = input,
) -> CalibrationResult:
    def emit(message: str) -> None:
        if output is not None:
            output(message)

    hello = phone.hello()
    actual_screen = tuple(screen or phone.screen)
    if screen is not None and actual_screen != tuple(phone.screen):
        phone.configure(screen=actual_screen)
        emit(f"screen set to {actual_screen[0]}x{actual_screen[1]}")
    emit(
        f"calibration ({actual_screen[0]}x{actual_screen[1]}), "
        f"current cal={phone.cal}"
    )

    samples: list[CalibrationSample] = []
    for target in calibration_targets(actual_screen, margin):
        x, y = int(target["x"]), int(target["y"])
        emit(f"tap {target['name']} at ({x}, {y})")
        phone.tap(x, y)
        if auto:
            observed_x, observed_y = x, y
        else:
            raw = prompt(
                f"  observed x,y for {target['name']} (blank = {x},{y}): "
            )
            observed_x, observed_y = _parse_observed(raw, x, y)
        samples.append(
            CalibrationSample(
                name=str(target["name"]),
                target_x=x,
                target_y=y,
                observed_x=observed_x,
                observed_y=observed_y,
            )
        )

    result = solve_calibration(actual_screen, tuple(phone.cal), samples)
    emit(f"old cal: {result.old_cal}")
    emit(f"new cal: {result.new_cal}")
    emit(f"valid:   {result.valid} ({result.message})")

    if save:
        if not result.valid:
            emit("not saving: computed calibration is invalid")
            return result
        if not yes:
            answer = prompt("save calibration? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                emit("not saved")
                return result
        phone.configure(cal=result.new_cal)
        phone.save()
        result.saved = True
        emit("saved calibration to device")
    return result
