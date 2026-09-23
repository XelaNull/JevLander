# JevGame

[![GitHub stars](https://img.shields.io/github/stars/XelaNull/JevLander?style=flat-square)](https://github.com/XelaNull/JevLander/stargazers)
[![Open issues](https://img.shields.io/github/issues/XelaNull/JevLander?style=flat-square)](https://github.com/XelaNull/JevLander/issues)
[![Latest commit](https://img.shields.io/github/last-commit/XelaNull/JevLander?style=flat-square)](https://github.com/XelaNull/JevLander/commits/master)
[![Repository size](https://img.shields.io/github/repo-size/XelaNull/JevLander?style=flat-square)](https://github.com/XelaNull/JevLander)

JevGame is a small Lunar Lander experiment for [TypeSafe AI's Jev System One
model](https://typesafe.ai/). Jev chooses attitude and throttle actions while
deterministic game code handles physics, navigation targets, safety checks, and
logging.

The project includes a local web console, manual play, replay, SQLite audit
logs, and a reproducible benchmark runner. It sends Jev typed `Choice`
questions through the TypeSafe System One API using the `jev-latest` model
alias. The model is hosted; this repository does not contain model weights.

Useful links: [TypeSafe AI](https://typesafe.ai/) · [Jev release announcement](https://typesafe.ai/blog/introducing-system-one-models-and-jev) · [Get Jev access](https://console.typesafe.ai/) · [JevGame research report](docs/jev-research.md)

## Why look at Jev?

Jev is designed for focused decisions inside software: choose an option, score
something, or answer yes/no with a probability and confidence value. TypeSafe
currently lists input pricing at **$0.042 per million tokens ($42 per billion)**
with output tokens free. Its release report lists **70–500 ms** end-to-end
response times and **40–200× faster** results for comparable System One
queries. The homepage highlights one workflow evaluation at **193.6× faster**
and **444.6× cheaper**.

Those are TypeSafe's published results for specific workflows, not a promise
about every request. Actual cost and latency depend on the request, network,
and comparison model. JevGame is a practical way to explore the model's
strengths and limits in a concrete control loop.

## Screenshots

The live console shows the flight stage, Jev request health, both control loops,
mission settings, and the current forecast criteria.

![JevGame live flight console](docs/screenshots/flight-console.png)

Completed flights can be scrubbed and inspected after the fact. The replay view
keeps the exact prompt criteria and audit summary beside the recorded state.

![JevGame replay and audit view](docs/screenshots/replay-audit.png)

## Quick start

JevGame requires Python 3.10 or newer.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m jevgame.server
```

Open <http://127.0.0.1:8971>. The local server starts without a token, so you
can inspect the console and play manually right away. Guided Jev flights need
an API token; without one, guided requests fall back safely and are recorded as
API failures while the simulation remains usable.

The simulation uses Moon gravity at 1.62 m/s², fixed terrain and fuel rules,
and fixed landing thresholds. Mission distance and starting fuel apply to the
next flight. Replay speed is independent of mission difficulty.

## Try the physics without Jev

Run the terminal demo with a scripted policy and no API calls:

```sh
.venv/bin/python -m jevgame.cli watch --render
```

Manual web play uses the same physics and terrain. Arrow keys control attitude;
Space controls the main engine.

## How the controller works

The controller is guidance-assisted. For each tick, ordinary code computes
navigation targets and forecasts the result of every available actuator choice.
Jev then answers two independent `Choice` questions in one request:

- **Attitude:** nine choices — `hold_attitude` plus four strength levels in
  each direction.
- **Throttle:** five choices — `no_op` plus 25%, 50%, 75%, and 100% thrust.

The selected controls are combined and applied concurrently. Separate
confidence thresholds gate the two answers. Missing keys, failed requests,
malformed answers, invalid choices, and low-confidence answers use a safe
fallback for that control. Every result records the request, raw response,
confidence, threshold, outcome, and state before and after the action.

This design gives Jev prepared, bounded decisions instead of asking it to
discover the physics or plan the entire flight from raw state. The project does
not claim that Jev independently solves navigation or improves on the
deterministic forecast selector.

## Run guided flights with Jev

The server and CLI accept one of two token sources. They are mutually
exclusive, and the token is never stored in this repository.

Read the token from an environment variable:

```sh
export API_TOKEN='your-token'
.venv/bin/python -m jevgame.server --token-env API_TOKEN
```

Read the token from a plain-text file:

```sh
.venv/bin/python -m jevgame.server \
  --token-file ~/.config/jevgame/token
```

Run guided episodes and write one SQLite audit database:

```sh
.venv/bin/python -m jevgame.cli run \
  --episodes 10 --seed 1000 --db runs/jev_calls.db \
  --token-env API_TOKEN
```

Run a reproducible holdout benchmark:

```sh
.venv/bin/python -m jevgame.cli benchmark \
  --episodes 60 --seed-start 1000 --output runs/my-holdout \
  --token-env API_TOKEN
```

The benchmark output directory must not already exist. It contains starting
states, source hashes, model versions, raw requests and responses,
per-episode results, a summary, and a 95% Wilson interval.

Use `--token-env NAME` for an environment-variable name or `--token-file PATH`
for a file containing only the token. Guided runs also accept independent
`--attitude-threshold` and `--throttle-threshold` values. Benchmark distance
can be bounded with `--min-distance` and `--max-distance`. A seed reproduces
the initial world; a remote model's answers need not be deterministic.

If neither token option is supplied, guided requests fall back safely and
record an API failure while the local simulation remains usable.

## Audit data and research

The `jev_calls` table stores one row per control question. It includes the
episode and tick, full request and response JSON, confidence, threshold,
applied composite action, state snapshots, and the outcome. Human-play commands
are stored in the same database with `step = 'human'` so manual and Jev runs
can be compared without changing the physics.

Benchmark directories also contain `manifest.json`, `episodes.jsonl`,
`summary.json`, `summary.md`, and one SQLite database per episode. Existing
directories are refused rather than overwritten.

The [research report](docs/jev-research.md) records the payload design,
experiments, measured results, and limitations. In the tested 100-flight
holdout, the guidance-assisted controller landed all 100 flights. The
deterministic selector landed the same starts too, so the result shows that Jev
can reliably consume narrow, prepared actuator choices; it does not isolate an
independent navigation advantage for Jev.

## Project layout

- `jevgame/physics.py` — deterministic Lunar Lander physics with no Jev dependency.
- `jevgame/guidance.py` — navigation targets and actuator forecasts.
- `jevgame/jev_player.py` — TypeSafe API boundary, validation, confidence gates, and fallbacks.
- `jevgame/episode.py` — shared tick loop for CLI runs and live SSE streaming.
- `jevgame/audit.py` — durable SQLite audit logging.
- `jevgame/server.py` and `jevgame/web/index.html` — local web game, manual controls, and replay.
- `tests/` — physics, controller, server, benchmark, and web-script checks.

The project stays standalone: it does not connect to an external daemon or
database, train Jev, or expose open-ended actions.
