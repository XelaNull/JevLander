"""Durable local audit log of every Jev call, tick-by-tick (README Part 2,
item 3). SQLite file so it's queryable after the fact; never raises --
audit logging must never break the game loop."""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jev_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id TEXT NOT NULL,
    tick INTEGER NOT NULL,
    step TEXT NOT NULL DEFAULT 'action',
    question TEXT NOT NULL,
    raw_answer TEXT,
    confidence REAL,
    threshold REAL,
    outcome TEXT NOT NULL,
    action_applied TEXT NOT NULL,
    state_before TEXT NOT NULL,
    state_after TEXT,
    variant TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute(_SCHEMA)
    # Migration for db files created before `variant` existed -- CREATE
    # TABLE IF NOT EXISTS above is a no-op on an already-existing table, so
    # older files need the column added explicitly. Without this, A/B
    # analysis on an old db has to reconstruct which variant fired from
    # criteria-text length (unreliable) instead of just querying the column.
    try:
        conn.execute("ALTER TABLE jev_calls ADD COLUMN variant TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    return conn


def log_tick(
    conn: sqlite3.Connection,
    *,
    episode_id: str,
    tick: int,
    question: Any,
    raw_answer: Any,
    confidence: float | None,
    threshold: float,
    outcome: str,
    action_applied: str,
    state_before: dict,
    state_after: dict | None,
    step: str = "action",
    variant: str | None = None,
) -> None:
    """Record one question result; attitude and throttle share a request.
    `variant` retains the historical controller identifier (now always D
    for Jev) so new results remain comparable with older audits. Never raises."""
    try:
        conn.execute(
            "INSERT INTO jev_calls (episode_id, tick, step, question, raw_answer, confidence, "
            "threshold, outcome, action_applied, state_before, state_after, variant) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                episode_id,
                tick,
                step,
                json.dumps(question, default=str),
                json.dumps(raw_answer, default=str) if raw_answer is not None else None,
                confidence,
                threshold,
                outcome,
                action_applied,
                json.dumps(state_before),
                json.dumps(state_after) if state_after is not None else None,
                variant,
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 -- audit logging must never block the game loop
        logger.warning("jevgame.audit: failed to log tick %d of %s (%s)", tick, episode_id, exc)
