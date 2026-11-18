#!/usr/bin/env python3
"""Benchmark The Eye's OCR pipelines on the synthetic and real screenshots.

Compares the pre-2026-11-11 one-shot RapidOCR call ("legacy") with the
two-stage detect-on-downscaled / recognize-on-native-crops pipeline, and
prints a Markdown table (plus JSON with ``--json``).

Examples::

    .venv/bin/python tools/bench_vision.py
    .venv/bin/python tools/bench_vision.py --repeat 5 --det-max 768
    .venv/bin/python tools/bench_vision.py --images runs/service-real/*.png --json
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import numpy as np
from PIL import Image

from phone_console.vision.ocr import create_engine

REPO = Path(__file__).resolve().parents[1]
DEFAULT_IMAGES = [REPO / "docs" / "assets" / "eye-demo.png"] + sorted(
    (REPO / "runs" / "service-real").glob("find-*.png")
)[:6]


def load(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"))[:, :, ::-1].copy()


def timed(fn, repeat: int) -> list[float]:
    fn()  # warmup: model load / first inference
    samples: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    return samples


def legacy_engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR(use_cls=False, intra_op_num_threads=4)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--images",
        nargs="*",
        type=Path,
        default=DEFAULT_IMAGES,
        help="screenshots to benchmark (default: demo + real service shots)",
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--det-max", type=int, default=1024)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    paths = [path for path in args.images if path.is_file()]
    if not paths:
        parser.error("no images found")
    images = [(path, load(path)) for path in paths]

    legacy = legacy_engine()
    fast = create_engine(det_max_side=args.det_max)
    fast.warmup()
    cached = create_engine(det_max_side=args.det_max)
    cached.warmup()

    rows = []
    legacy_total, fast_total, cached_total = [], [], []
    for path, image in images:
        legacy_ms = statistics.median(timed(lambda: legacy(image), args.repeat))

        def cold():
            fast.clear_cache()
            fast.recognize(image)

        fast_ms = statistics.median(timed(cold, args.repeat))
        cached.clear_cache()
        cached.recognize(image)  # populate the frame cache
        cached_ms = statistics.median(timed(lambda: cached.recognize(image), args.repeat))

        rows.append(
            {
                "image": str(path.relative_to(REPO))
                if path.is_relative_to(REPO)
                else str(path),
                "size": [image.shape[1], image.shape[0]],
                "legacy_ms": round(legacy_ms, 1),
                "two_stage_ms": round(fast_ms, 1),
                "cached_ms": round(cached_ms, 2),
                "speedup": round(legacy_ms / fast_ms, 2),
                "items": len(fast.recognize(image)),
            }
        )
        legacy_total.append(legacy_ms)
        fast_total.append(fast_ms)
        cached_total.append(cached_ms)

    summary = {
        "det_max": args.det_max,
        "repeat": args.repeat,
        "legacy_ms": round(statistics.mean(legacy_total), 1),
        "two_stage_ms": round(statistics.mean(fast_total), 1),
        "cached_ms": round(statistics.mean(cached_total), 2),
        "speedup": round(
            statistics.mean(legacy_total) / statistics.mean(fast_total), 2
        ),
        "images": rows,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print("| image | size | legacy ms | two-stage ms | cached ms | speedup | boxes |")
    print("|---|---|---|---|---|---|---|")
    for row in rows:
        width, height = row["size"]
        print(
            f"| {row['image']} | {width}x{height} | {row['legacy_ms']} "
            f"| {row['two_stage_ms']} | {row['cached_ms']} | "
            f"x{row['speedup']} | {row['items']} |"
        )
    print(
        f"\nmean: legacy {summary['legacy_ms']} ms -> two-stage "
        f"{summary['two_stage_ms']} ms (x{summary['speedup']}), cached "
        f"{summary['cached_ms']} ms; det_max={args.det_max}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
