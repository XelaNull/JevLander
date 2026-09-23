"""Runs one full Jev-piloted episode: tick loop, apply_action, audit log
(README Part 2, items 2-3). Uses the guidance-assisted Jev controller:
two independent Choice questions in one request per tick.

iter_episode() is the single source of truth for the tick loop -- both
run_episode() (CLI/batch use) and jevgame.server's live SSE stream consume
it, so the audit-logging and control logic can't drift between the two."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

from . import audit, jev_player
from .physics import Action, GameState, apply_action, initial_state

TickKind = Literal["start", "tick", "done"]


@dataclass
class EpisodeResult:
    episode_id: str
    final_state: GameState
    ticks: int
    outcome_counts: dict[str, int]  # Jev call outcomes across both steps: applied/discarded_low_confidence/...
    frames: list[dict] | None = None    # per-tick state.as_dict(), only when collect_trace=True
    actions: list[str] | None = None    # per-tick applied action, only when collect_trace=True


def iter_episode(
    *,
    db_path: Path,
    confidence_threshold: float = jev_player.DEFAULT_CONFIDENCE_THRESHOLD,
    attitude_confidence_threshold: float | None = None,
    throttle_confidence_threshold: float | None = None,
    start_state: GameState | None = None,
) -> Iterator[tuple[TickKind, GameState, Action | None, str, dict[str, int], list[dict[str, Any]] | None]]:
    """Generator: yields (kind, state, action, episode_id, outcome_counts,
    steps) for the initial state ("start", action=None, steps=None), each
    subsequent tick ("tick", steps=the attitude+throttle step dicts for
    THIS tick only, each with outcome/confidence), and a final ("done")
    once the episode ends -- one real batched Jev request per tick, yielded as
    soon as it's decided, so a caller can stream ticks live instead of
    waiting for the whole episode. Writes the same audit log regardless of
    who's consuming it."""
    state = start_state if start_state is not None else initial_state()
    episode_id = uuid.uuid4().hex[:12]
    conn = audit.connect(db_path)
    outcome_counts: dict[str, int] = {}

    try:
        yield "start", state, None, episode_id, outcome_counts, None

        while state.status == "flying":
            state_before = state.as_dict()

            action, steps = jev_player.choose_action_guided(
                state,
                attitude_threshold=(confidence_threshold if attitude_confidence_threshold is None
                                    else attitude_confidence_threshold),
                throttle_threshold=(confidence_threshold if throttle_confidence_threshold is None
                                    else throttle_confidence_threshold),
            )
            new_state = apply_action(state, action)

            for i, s in enumerate(steps):
                outcome_counts[s["outcome"]] = outcome_counts.get(s["outcome"], 0) + 1
                # Both steps log the same combined action for traceability;
                # state_after is only meaningfully attached to the last one.
                # threshold is the PER-STEP value actually used (attitude and
                # throttle can now be tuned independently), not just the
                # shared confidence_threshold default.
                is_last = i == len(steps) - 1
                audit.log_tick(
                    conn, episode_id=episode_id, tick=state.tick, question=s["question_payload"],
                    raw_answer=s["raw_answer"], confidence=s["confidence"],
                    threshold=s.get("threshold", confidence_threshold),
                    outcome=s["outcome"], action_applied=action, state_before=state_before,
                    state_after=new_state.as_dict() if is_last else None, step=s["step"],
                    variant=s.get("variant"),
                )

            state = new_state
            yield "tick", state, action, episode_id, outcome_counts, steps
    finally:
        conn.close()

    yield "done", state, None, episode_id, outcome_counts, None


def run_episode(
    *,
    db_path: Path,
    confidence_threshold: float = jev_player.DEFAULT_CONFIDENCE_THRESHOLD,
    start_state: GameState | None = None,
    collect_trace: bool = False,
    attitude_confidence_threshold: float | None = None,
    throttle_confidence_threshold: float | None = None,
) -> EpisodeResult:
    frames: list[dict] | None = [] if collect_trace else None
    actions: list[str] | None = [] if collect_trace else None
    episode_id = ""
    outcome_counts: dict[str, int] = {}
    final_state: GameState | None = None

    for kind, state, action, episode_id, outcome_counts, _steps in iter_episode(
        db_path=db_path, confidence_threshold=confidence_threshold, start_state=start_state,
        attitude_confidence_threshold=attitude_confidence_threshold,
        throttle_confidence_threshold=throttle_confidence_threshold,
    ):
        if collect_trace and kind in ("start", "tick"):
            frames.append(state.as_dict())
            if action is not None:
                actions.append(action)
        if kind != "start":
            final_state = state

    assert final_state is not None
    return EpisodeResult(
        episode_id=episode_id, final_state=final_state, ticks=final_state.tick,
        outcome_counts=outcome_counts, frames=frames, actions=actions,
    )
