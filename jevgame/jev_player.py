"""Guidance-assisted Jev controller: two forecast choices in one request.

Accepted answers are applied directly, with independent confidence gates.
Failures fall back to hold/no thrust; this does not guarantee a safe landing.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

import httpx

from .physics import Action, GameState, combine_action

logger = logging.getLogger(__name__)

_API_URL = "https://api.typesafe.ai/v1/systemone"
_API_KEY_PATH = Path.home() / ".fibril" / "typesafe_api_key"
_MODEL = "jev-latest"
_TIMEOUT_SEC = 10.0

DEFAULT_CONFIDENCE_THRESHOLD = 0.55


def _read_api_key() -> str | None:
    try:
        key = _API_KEY_PATH.read_text().strip()
    except OSError:
        return None
    return key or None


_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=_TIMEOUT_SEC)
    return _client


# UI cost uses an approximate tokenizer; actual usage is retained in audit responses.
JEV_INPUT_COST_PER_MTOK = 0.042
_tiktoken_encoding = None


def _get_tiktoken_encoding():
    global _tiktoken_encoding
    if _tiktoken_encoding is None:
        import tiktoken
        _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
    return _tiktoken_encoding


def _estimate_input_tokens(payload: dict[str, Any]) -> int:
    text = json.dumps(payload)
    try:
        return len(_get_tiktoken_encoding().encode(text))
    except Exception:  # noqa: BLE001 -- token estimate must never break the game loop
        return max(1, len(text) // 4)  # crude fallback if tiktoken is unavailable


def _call_jev_api(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    """Raw HTTP call, zero error handling -- callers catch everything."""
    resp = _get_client().post(
        _API_URL,
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    resp.raise_for_status()
    return resp.json()


def choose_action_guided(
    state: GameState, *, attitude_threshold: float, throttle_threshold: float,
) -> tuple[Action, list[dict[str, Any]]]:
    """Two isolated forecast choices in one request; no hidden overrides."""
    from .guidance import build_payload

    payload = build_payload(state, _MODEL)
    result = None
    try:
        api_key = _read_api_key()
        if not api_key:
            raise ValueError("No TypeSafe API key configured")
        result = _call_jev_api(payload, api_key)
    except Exception as exc:
        logger.warning("Guided Jev request failed (%s)", type(exc).__name__)

    steps = []
    tokens = _estimate_input_tokens(payload)
    for key, fallback, threshold in (
        ("attitude", "hold_attitude", attitude_threshold),
        ("throttle", "no_op", throttle_threshold),
    ):
        choice, confidence, outcome = fallback, None, "api_failure"
        if result is not None:
            try:
                answer = result["answers"][key]
                candidate = answer["choice"]
                confidence = float(answer["confidence"])
                if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    raise ValueError("Invalid confidence")
                if candidate not in payload["questions"][key]["criteria"]:
                    outcome = "invalid_choice"
                elif confidence < threshold:
                    outcome = "discarded_low_confidence"
                else:
                    choice, outcome = candidate, "applied"
            except (KeyError, TypeError, ValueError):
                outcome = "invalid_answer"
        steps.append({
            "step": key, "question_payload": payload, "raw_answer": result,
            "confidence": confidence, "outcome": outcome, "choice": choice,
            "input_tokens_est": tokens / 2,
            "cost_usd_est": tokens / 2 / 1_000_000 * JEV_INPUT_COST_PER_MTOK,
            "variant": "D", "threshold": threshold,
        })
    attitude, throttle = (s["choice"] for s in steps)
    return combine_action(
        "" if attitude == "hold_attitude" else attitude,
        "" if throttle == "no_op" else throttle,
    ), steps
