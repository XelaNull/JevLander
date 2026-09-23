"""Reproducible live API evaluation of the guidance-assisted controller.

Each seed creates its own RNG and audit database. No completed run is
overwritten. Interrupted runs retain their manifest and completed results.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .episode import run_episode
from .physics import GRAVITY, MAX_LANDING_ANGLE_DEG, MAX_LANDING_SPEED, random_initial_state


def wilson_interval(wins: int, total: int) -> tuple[float, float]:
    if not total:
        return 0.0, 1.0
    z = 1.959963984540054
    p = wins / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return center - half, center + half


def failure_reasons(state) -> list[str]:
    if state.status == "landed":
        return []
    reasons = []
    if not state.pad_x_min <= state.x <= state.pad_x_max:
        reasons.append("off_pad")
    if math.hypot(state.vx, state.vy) > MAX_LANDING_SPEED:
        reasons.append("speed")
    if abs(state.angle_deg) > MAX_LANDING_ANGLE_DEG:
        reasons.append("tilt")
    if state.fuel <= 0:
        reasons.append("fuel_exhausted")
    if state.status == "timeout":
        reasons.append("timeout")
    return reasons


def run_benchmark(*, output: Path, seeds: list[int],
                  threshold: float = 0.55, attitude_threshold: float | None = None,
                  throttle_threshold: float | None = None, workers: int = 4,
                  min_distance: float = 25, max_distance: float = 90,
                  gravity: float = GRAVITY) -> dict:
    # Preserve the D audit identifier and result schema for historical comparisons.
    variants = ("D",)
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).parent
    manifest = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "variants": variants, "seeds": seeds, "threshold": threshold,
        "attitude_threshold": attitude_threshold, "throttle_threshold": throttle_threshold,
        "workers": workers, "min_distance": min_distance, "max_distance": max_distance,
        "gravity": gravity,
        "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob("*.py")},
        "starts": {str(seed): asdict(random_initial_state(random.Random(seed),
                   min_distance=min_distance, max_distance=max_distance, gravity=gravity)) for seed in seeds},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def run_one(variant: str, seed: int) -> dict:
        path = output / f"{variant}-{seed}.db"
        result = run_episode(
            db_path=path, confidence_threshold=threshold,
            attitude_confidence_threshold=attitude_threshold,
            throttle_confidence_threshold=throttle_threshold,
            start_state=random_initial_state(random.Random(seed), min_distance=min_distance,
                                             max_distance=max_distance, gravity=gravity),
        )
        with sqlite3.connect(path) as conn:
            rows = conn.execute("SELECT tick, confidence, raw_answer FROM jev_calls").fetchall()
        confidences = [r[1] for r in rows if r[1] is not None and math.isfinite(r[1])]
        models, input_tokens, seen = set(), 0, set()
        for tick, _, raw in rows:
            answer = json.loads(raw) if raw else {}
            if isinstance(answer, dict):
                if answer.get("model"):
                    models.add(answer["model"])
                # D logs the same batched request on both question rows.
                if tick not in seen:
                    input_tokens += answer.get("usage", {}).get("input_tokens", 0)
                seen.add(tick)
        return {
            "variant": variant, "seed": seed, "episode_id": result.episode_id,
            "status": result.final_state.status, "ticks": result.ticks,
            "final_state": asdict(result.final_state), "failure_reasons": failure_reasons(result.final_state),
            "outcomes": result.outcome_counts, "mean_confidence": sum(confidences) / len(confidences) if confidences else None,
            "models": sorted(models), "input_tokens": input_tokens, "audit_db": path.name,
        }

    results = []
    with (output / "episodes.jsonl").open("x") as log, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_one, variant, seed) for variant in variants for seed in seeds]
        for future in as_completed(futures):
            row = future.result()
            results.append(row)
            log.write(json.dumps(row) + "\n")
            log.flush()
            print(f"{row['variant']} seed={row['seed']}: {row['status']} ({row['ticks']} ticks)", flush=True)

    summary = {}
    lines = ["# Live Jev benchmark", "", "Guidance-assisted controller on reproducible seeded starts.", ""]
    for variant in variants:
        group = [r for r in results if r["variant"] == variant]
        wins = sum(r["status"] == "landed" for r in group)
        low, high = wilson_interval(wins, len(group))
        outcomes = Counter()
        for row in group:
            outcomes.update(row["outcomes"])
        summary[variant] = {
            "landed": wins, "episodes": len(group), "rate": wins / len(group),
            "wilson_95": [low, high], "outcomes": dict(outcomes),
            "failure_reasons": dict(Counter(reason for r in group for reason in r["failure_reasons"])),
            "input_tokens": sum(r["input_tokens"] for r in group),
        }
        lines += ["## Guided controller", "", f"Landed {wins}/{len(group)} ({wins / len(group):.1%}); 95% Wilson interval {low:.1%}–{high:.1%}.",
                  f"Question outcomes: `{dict(outcomes)}`.", ""]
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (output / "summary.md").write_text("\n".join(lines))
    print(json.dumps(summary, indent=2), flush=True)
    return summary
