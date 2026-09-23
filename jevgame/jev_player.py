"""Guidance-assisted Jev controller: two forecast choices in one request.

Accepted answers are applied directly, with independent confidence gates.
Failures fall back to hold/no thrust; this does not guarantee a safe landing.
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any

import httpx

from .physics import Action, GameState, combine_action

logger = logging.getLogger(__name__)

_API_URL = "https://api.typesafe.ai/v1/systemone"
_MODEL = "jev-latest"
_TIMEOUT_SEC = 10.0

_TOKEN_FILE: Path | None = None
_TOKEN_ENV: str | None = None

DEFAULT_CONFIDENCE_THRESHOLD = 0.55


def configure_token_source(*, token_file: str | Path | None = None,
                           token_env: str | None = None) -> None:
    """Select the access-token source used by subsequent API calls."""
    if token_file is not None and token_env is not None:
        raise ValueError("Choose either a token file or an environment variable, not both")
    if token_env is not None and not token_env.strip():
        raise ValueError("Token environment variable name cannot be empty")

    global _TOKEN_FILE, _TOKEN_ENV
    _TOKEN_FILE = Path(token_file).expanduser() if token_file is not None else None
    _TOKEN_ENV = token_env.strip() if token_env is not None else None


def _read_token() -> str | None:
    if _TOKEN_FILE is not None:
        try:
            token = _TOKEN_FILE.read_text().strip()
        except OSError:
            return None
        return token or None
    if _TOKEN_ENV is not None:
        token = os.environ.get(_TOKEN_ENV, "").strip()
        return token or None
    return None


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


def _call_jev_api(payload: dict[str, Any], token: str) -> dict[str, Any]:
    """Raw HTTP call, zero error handling -- callers catch everything."""
    resp = _get_client().post(
        _API_URL,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
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
        token = _read_token()
        if not token:
            raise ValueError("No API token configured")
        result = _call_jev_api(payload, token)
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
