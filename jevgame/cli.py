"""Entry points: watch the physics sim with no Jev involved, or run N
Jev-piloted episodes and print an honest summary (README Part 2, item 4)."""
from __future__ import annotations

import argparse
import math
import random
import sqlite3
import statistics
import time
from pathlib import Path

from . import jev_player
from .episode import run_episode
from .physics import apply_action, initial_state, random_initial_state
from .render import render_ascii


def _add_token_source_args(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--token-file", type=Path, metavar="PATH",
        help="read the API token from a plain-text file",
    )
    source.add_argument(
        "--token-env", metavar="NAME",
        help="read the API token from this environment variable",
    )


def _configure_token_source(args: argparse.Namespace) -> None:
    try:
        jev_player.configure_token_source(
            token_file=args.token_file, token_env=args.token_env,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _validate_thresholds(args: argparse.Namespace) -> None:
    for value in (args.threshold, args.attitude_threshold, args.throttle_threshold):
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 1):
            raise SystemExit("Confidence thresholds must be between 0 and 1")


def _validate_distance_bounds(min_distance: float, max_distance: float) -> None:
    if (
        not math.isfinite(min_distance)
        or not math.isfinite(max_distance)
        or min_distance < 0
        or min_distance > max_distance
    ):
        raise SystemExit("Distance bounds must be finite, nonnegative and ordered")


def cmd_benchmark(args: argparse.Namespace) -> None:
    from .benchmark import run_benchmark

    _configure_token_source(args)
    if args.episodes < 1:
        raise SystemExit("--episodes must be positive")
    _validate_distance_bounds(args.min_distance, args.max_distance)
    _validate_thresholds(args)
    run_benchmark(
        output=args.output,
        seeds=list(range(args.seed_start, args.seed_start + args.episodes)),
        threshold=args.threshold, attitude_threshold=args.attitude_threshold,
        throttle_threshold=args.throttle_threshold, workers=args.workers,
        min_distance=args.min_distance, max_distance=args.max_distance,
    )


def cmd_watch(args: argparse.Namespace) -> None:
    """Demo the physics sim with a scripted policy, no Jev involved --
    proves the simulation works independently (README Part 2, item 1)."""
    state = initial_state()
    while state.status == "flying":
        # Trivial scripted policy: burn whenever falling faster than 2 m/s.
        action = "thrust_100" if state.vy < -2.0 and state.fuel > 0 else "no_op"
        state = apply_action(state, action)
        if args.render:
            print(render_ascii(state))
            print()
            time.sleep(args.delay)
    print(render_ascii(state))
    print(f"\nFinal status: {state.status}")


def cmd_run(args: argparse.Namespace) -> None:
    """Run N Jev-piloted episodes, log every tick, print an honest summary."""
    _configure_token_source(args)
    if args.episodes < 1:
        raise SystemExit("--episodes must be positive")
    _validate_thresholds(args)
    db_path = Path(args.db)
    rng = random.Random(args.seed)
    results = []
    for i in range(args.episodes):
        start_state = random_initial_state(rng) if args.randomize else None
        result = run_episode(
            db_path=db_path, confidence_threshold=args.threshold,
            start_state=start_state,
            attitude_confidence_threshold=args.attitude_threshold,
            throttle_confidence_threshold=args.throttle_threshold,
        )
        results.append(result)
        print(
            f"episode {i + 1}/{args.episodes} [{result.episode_id}]: "
            f"{result.ticks} ticks -> {result.final_state.status} "
            f"(jev outcomes: {result.outcome_counts})"
        )

    _print_summary(results, db_path)


def _print_summary(results: list, db_path: Path) -> None:
    n = len(results)
    if n == 0:
        print("No episodes run.")
        return

    counts: dict[str, int] = {}
    for r in results:
        counts[r.final_state.status] = counts.get(r.final_state.status, 0) + 1

    print("\n=== Summary ===")
    print(f"Episodes: {n}")
    for status, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {status}: {c} ({100 * c / n:.0f}%)")

    landed = counts.get("landed", 0)
    print(f"Landing success rate: {100 * landed / n:.1f}%")

    conn = sqlite3.connect(db_path)
    try:
        episode_ids = [r.episode_id for r in results]
        placeholders = ",".join("?" for _ in episode_ids)
        rows = conn.execute(
            "SELECT confidence, action_applied, outcome, state_after FROM jev_calls "
            f"WHERE confidence IS NOT NULL AND episode_id IN ({placeholders})", episode_ids,
        ).fetchall()
        # For action-distribution purposes, count exactly one row per tick.
        # 'orientation'/'attitude' steps share the SAME action_applied value
        # as the step that follows them in the same tick (both log the final
        # combined action for traceability), so excluding them avoids
        # double-counting a single tick's action.
        decision_rows = conn.execute(
            "SELECT action_applied FROM jev_calls WHERE state_after IS NOT NULL "
            f"AND episode_id IN ({placeholders})", episode_ids,
        ).fetchall()
    finally:
        conn.close()

    if rows:
        confidences = [r[0] for r in rows]
        print(f"\nJev confidence across all logged ticks: mean={statistics.mean(confidences):.2f} "
              f"stdev={statistics.pstdev(confidences):.2f} (n={len(confidences)})")

        applied_confidences = [r[0] for r in rows if r[2] == "applied"]
        low_conf_confidences = [r[0] for r in rows if r[2] == "discarded_low_confidence"]
        if applied_confidences:
            print(f"  applied ticks: mean confidence={statistics.mean(applied_confidences):.2f} "
                  f"(n={len(applied_confidences)})")
        if low_conf_confidences:
            print(f"  discarded-low-confidence ticks: mean confidence={statistics.mean(low_conf_confidences):.2f} "
                  f"(n={len(low_conf_confidences)})")

        action_counts: dict[str, int] = {}
        for (action,) in decision_rows:
            action_counts[action] = action_counts.get(action, 0) + 1
        print(f"  action distribution across ticks: {action_counts}")
    else:
        print("\nNo Jev calls were logged with a confidence value (all api_failure/invalid_choice, "
              "or no API token configured) -- see jev_calls table for outcome breakdown.")

    print(f"\nFull tick-by-tick audit log: {db_path}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="jevgame")
    sub = parser.add_subparsers(dest="command", required=True)

    watch = sub.add_parser("watch", help="run the physics sim with a scripted policy, no Jev")
    watch.add_argument("--render", action="store_true", default=True)
    watch.add_argument("--no-render", dest="render", action="store_false")
    watch.add_argument("--delay", type=float, default=0.05)
    watch.set_defaults(func=cmd_watch)

    run = sub.add_parser("run", help="run N Jev-piloted episodes")
    run.add_argument("--episodes", type=int, default=10)
    run.add_argument("--threshold", type=float, default=jev_player.DEFAULT_CONFIDENCE_THRESHOLD)
    run.add_argument("--db", type=str, default="runs/jev_calls.db")
    run.add_argument("--randomize", action="store_true", default=True,
                      help="vary start altitude/drift/tilt per episode (default on)")
    run.add_argument("--no-randomize", dest="randomize", action="store_false",
                      help="use the fixed canonical upright/centered start every episode")
    run.add_argument("--seed", type=int, default=None)
    run.add_argument("--attitude-threshold", type=float)
    run.add_argument("--throttle-threshold", type=float)
    _add_token_source_args(run)
    run.set_defaults(func=cmd_run)

    bench = sub.add_parser("benchmark", help="live guided API trials on reproducible seeds, full audit")
    bench.add_argument("--episodes", type=int, default=30)
    bench.add_argument("--seed-start", type=int, default=1000)
    bench.add_argument("--threshold", type=float, default=jev_player.DEFAULT_CONFIDENCE_THRESHOLD)
    bench.add_argument("--attitude-threshold", type=float)
    bench.add_argument("--throttle-threshold", type=float)
    bench.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    bench.add_argument("--min-distance", type=float, default=25)
    bench.add_argument("--max-distance", type=float, default=90)
    bench.add_argument("--output", type=Path, required=True)
    _add_token_source_args(bench)
    bench.set_defaults(func=cmd_benchmark)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
