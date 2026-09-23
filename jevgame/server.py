"""Local HTTP server: watch Jev fly a fresh Lunar Lander episode live, in a
browser, on your own machine -- no hosting, no Artifact. Run with:

    python -m jevgame.server [port]

Then open http://127.0.0.1:<port> (default 8971). Click "Fly New Episode"
to stream one real guidance-assisted Jev episode live over
Server-Sent Events -- each tick is pushed to the page as soon as Jev
decides it (one batched TypeSafe API request per tick), so the page shows the
actual flight happening in real time rather than a replay computed after
the fact.
"""
from __future__ import annotations

import json
import math
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import uuid

from . import audit, episode, jev_player
from .guidance import build_payload as build_guided_payload
from .physics import ACTIONS, GRAVITY, START_FUEL, GameState, apply_action, terrain_height_at, random_initial_state

WEB_DIR = Path(__file__).parent / "web"
DB_PATH = Path("runs") / "server_jev_calls.db"
DEFAULT_PORT = 8971

# In-memory human-play sessions: episode_id -> GameState. Lets a person fly
# the same physics/terrain Jev flies, via keyboard, to see first-hand why
# it's genuinely hard (tight fuel budget, low thrust-to-weight margin, no
# forgiveness for late braking) rather than assuming Jev is simply bad at
# it. Every tick is still written to the SAME audit db as Jev episodes (see
# _log_human_tick below) -- not a Jev call, so question/raw_answer/confidence
# are null and outcome is "human_input", but it stays one queryable table of
# every run, human or Jev, for later analysis/comparison.
_MANUAL_SESSIONS: dict[str, GameState] = {}
_MANUAL_CONN = None
_MANUAL_LOCK = threading.Lock()
MANUAL_DEFAULT_DT = 0.06  # ~60ms/step, well under a spacebar tap's duration


def _manual_conn():
    global _MANUAL_CONN
    if _MANUAL_CONN is None:
        _MANUAL_CONN = audit.connect(DB_PATH)
    return _MANUAL_CONN


def _log_human_tick(*, episode_id: str, tick: int, action: str, state_before: dict, state_after: dict | None) -> None:
    with _MANUAL_LOCK:
        audit.log_tick(
            _manual_conn(),
            episode_id=episode_id, tick=tick, step="human",
            question={"source": "human", "controls": "keyboard"}, raw_answer=None,
            confidence=None, threshold=0.0, outcome="human_input",
            action_applied=action, state_before=state_before, state_after=state_after,
        )

# Mission settings shared by Jev and manual flights. Distance is horizontal
# distance to the pad center; gravity is always lunar in the web game.
DEFAULT_START_DISTANCE = 60.0
MIN_START_DISTANCE, MAX_START_DISTANCE = 20.0, 480.0
MIN_START_FUEL = 10.0

# Sampled once per episode and shipped to the client so it can draw the
# terrain profile without re-implementing terrain_height_at's seeded
# crater/noise generation in JS -- this stays the single source of truth.
_TERRAIN_SAMPLE_RANGE = (-550.0, 550.0)
_TERRAIN_SAMPLE_STEP = 1.5


def _terrain_profile(terrain_seed: int, pad_x_min: float, pad_x_max: float) -> list[list[float]]:
    profile = []
    x = _TERRAIN_SAMPLE_RANGE[0]
    while x <= _TERRAIN_SAMPLE_RANGE[1]:
        profile.append([round(x, 1), round(terrain_height_at(x, terrain_seed, pad_x_min, pad_x_max), 2)])
        x += _TERRAIN_SAMPLE_STEP
    return profile


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # quiet stdout, keep it uncluttered
        pass

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 -- http.server's naming convention
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            body = (WEB_DIR / "index.html").read_bytes()
            self._send_bytes(200, body, "text/html; charset=utf-8")
            return

        if parsed.path == "/api/episode/stream":
            self._stream_episode(parse_qs(parsed.query))
            return

        if parsed.path == "/api/manual/start":
            self._manual_start(parse_qs(parsed.query))
            return

        if parsed.path == "/api/prompts":
            self._prompts()
            return

        self.send_error(404, "Not found")

    def _prompts(self) -> None:
        # Example values from the same payload builder used by live flights.
        sample = build_guided_payload(random_initial_state(random.Random(0)), jev_player._MODEL)
        guided_questions = {
            key: {**question, "state_keys": ["flight_targets"],
                  "note": "Example forecast values. Guidance recomputes these criteria each tick; live flights show the actual payload."}
            for key, question in sample["questions"].items()
        }
        self._send_bytes(200, json.dumps(guided_questions).encode("utf-8"), "application/json")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/manual/step":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_bytes(400, b'{"error":"bad json"}', "application/json")
                return
            self._manual_step(body)
            return
        self.send_error(404, "Not found")

    def _start_state_from_qs(self, qs: dict) -> GameState:
        seed = int(qs["seed"][0]) if "seed" in qs and qs["seed"][0] else None

        def setting(key: str, default: float, minimum: float, maximum: float) -> float:
            try:
                value = float(qs[key][0]) if qs.get(key) else default
            except (ValueError, TypeError):
                value = default
            if not math.isfinite(value):
                value = default
            return max(minimum, min(maximum, value))

        distance = setting("distance", DEFAULT_START_DISTANCE, MIN_START_DISTANCE, MAX_START_DISTANCE)
        fuel = setting("fuel", START_FUEL, MIN_START_FUEL, START_FUEL)
        rng = random.Random(seed)
        start_state = random_initial_state(
            rng, min_distance=distance, max_distance=distance, gravity=GRAVITY, fuel=fuel,
        )
        return start_state

    def _confidence_thresholds_from_qs(self, qs: dict) -> tuple[float, float]:
        """Per-control confidence thresholds from the UI's two sliders --
        attitude and throttle can be gated independently since one
        question's phrasing/framing can be more reliable than the other's
        (confirmed via the audit db: attitude's discard rate ran far
        higher than throttle's under the same shared threshold)."""
        def _one(key: str) -> float:
            try:
                v = float(qs[key][0]) if key in qs and qs[key][0] else jev_player.DEFAULT_CONFIDENCE_THRESHOLD
            except ValueError:
                v = jev_player.DEFAULT_CONFIDENCE_THRESHOLD
            return max(0.0, min(1.0, v))
        return _one("attitude_threshold"), _one("throttle_threshold")

    def _manual_start(self, qs: dict) -> None:
        start_state = self._start_state_from_qs(qs)
        episode_id = "human-" + uuid.uuid4().hex[:12]
        _MANUAL_SESSIONS[episode_id] = start_state
        _log_human_tick(
            episode_id=episode_id, tick=start_state.tick, action="start",
            state_before=start_state.as_dict(), state_after=None,
        )
        self._send_bytes(200, json.dumps({
            "episode_id": episode_id,
            "frame": start_state.as_dict(),
            "pad_x_min": start_state.pad_x_min,
            "pad_x_max": start_state.pad_x_max,
            "terrain_seed": start_state.terrain_seed,
            "terrain_profile": _terrain_profile(
                start_state.terrain_seed, start_state.pad_x_min, start_state.pad_x_max,
            ),
        }).encode("utf-8"), "application/json")

    def _manual_step(self, body: dict) -> None:
        episode_id = body.get("episode_id")
        action = body.get("action", "no_op")
        # Human play steps physics much finer than Jev's fixed 0.5s decision
        # tick (see apply_action's dt docstring) so a quick spacebar tap is
        # actually resolvable instead of being all-or-nothing over half a
        # second. Clamped to a sane range regardless of what the client sends.
        try:
            dt = float(body.get("dt", MANUAL_DEFAULT_DT))
        except (TypeError, ValueError):
            dt = MANUAL_DEFAULT_DT
        dt = max(0.01, min(0.5, dt))
        state = _MANUAL_SESSIONS.get(episode_id)
        if state is None:
            self._send_bytes(404, b'{"error":"unknown episode_id"}', "application/json")
            return
        if action not in ACTIONS:
            action = "no_op"
        if state.status != "flying":
            self._send_bytes(200, json.dumps({
                "frame": state.as_dict(), "action": action, "done": True,
            }).encode("utf-8"), "application/json")
            return

        state_before = state.as_dict()
        new_state = apply_action(state, action, dt=dt)
        _MANUAL_SESSIONS[episode_id] = new_state
        _log_human_tick(
            episode_id=episode_id, tick=new_state.tick, action=action,
            state_before=state_before, state_after=new_state.as_dict(),
        )
        done = new_state.status != "flying"
        if done:
            del _MANUAL_SESSIONS[episode_id]
        self._send_bytes(200, json.dumps({
            "frame": new_state.as_dict(), "action": action, "done": done,
        }).encode("utf-8"), "application/json")

    def _stream_episode(self, qs: dict) -> None:
        start_state = self._start_state_from_qs(qs)
        attitude_threshold, throttle_threshold = self._confidence_thresholds_from_qs(qs)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send_event(obj: dict) -> None:
            self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        try:
            meta_sent = False
            last_tick_at = time.monotonic()
            for kind, state, action, episode_id, outcome_counts, steps in episode.iter_episode(
                db_path=DB_PATH, start_state=start_state,
                attitude_confidence_threshold=attitude_threshold,
                throttle_confidence_threshold=throttle_threshold,
            ):
                if not meta_sent:
                    send_event({
                        "type": "meta",
                        "episode_id": episode_id,
                        "pad_x_min": start_state.pad_x_min,
                        "pad_x_max": start_state.pad_x_max,
                        "terrain_seed": start_state.terrain_seed,
                        "terrain_profile": _terrain_profile(
                            start_state.terrain_seed, start_state.pad_x_min, start_state.pad_x_max,
                        ),
                    })
                    meta_sent = True

                if kind in ("start", "tick"):
                    now = time.monotonic()
                    tick_ms = round((now - last_tick_at) * 1000, 1)
                    last_tick_at = now
                    send_event({
                        "type": "frame", "frame": state.as_dict(), "action": action,
                        "tick_ms": tick_ms,
                        # Per-question outcome/confidence for THIS tick's batched
                        # request (None for the initial "start" frame, which made
                        # no call) -- lets the page show live query/error stats.
                        "steps": [
                            {
                                "step": s["step"], "outcome": s["outcome"], "confidence": s["confidence"],
                                "choice": s.get("choice"), "input_tokens_est": s.get("input_tokens_est"),
                                "cost_usd_est": s.get("cost_usd_est"), "variant": s.get("variant"),
                                "question_payload": s["question_payload"],
                            }
                            for s in steps
                        ] if steps is not None else None,
                    })
                elif kind == "done":
                    send_event({
                        "type": "done", "outcome": state.status, "ticks": state.tick,
                        "outcome_counts": outcome_counts,
                    })
        except (BrokenPipeError, ConnectionResetError):
            pass  # viewer navigated away mid-flight -- the episode still finished and got logged
        except Exception as exc:  # noqa: BLE001 -- report to the page, don't crash the server
            try:
                send_event({"type": "error", "message": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"JevGame server running at http://127.0.0.1:{port} -- Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
