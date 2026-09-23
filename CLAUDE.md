# CLAUDE.md

Guidance for Claude Code sessions working in this repo. Read `README.md` first — it is the full spec. This file only covers things not already in the README: repo state, conventions, and the build/test/deploy/feedback loop.

## Repo state

The repository contains the implemented Lunar Lander simulation, guidance-assisted
Jev controller, local web UI, audit logger, and benchmark runner. Read the current
README and inspect the source before changing behavior; the README is product
documentation now, rather than a future implementation brief.

## Key references (outside this repo)

- `~/github/Fibril/JEV.md` — eight shipped production Jev integrations, the canonical examples of the Choice-question shape.
- `~/github/Fibril/src/fibril/jev_dedup.py` — authoritative reference for the Jev HTTP call shape, endpoint, payload, and `_read_api_key()` pattern. Copy this pattern, don't reinvent it.
- `~/.fibril/typesafe_api_key` — plain-text API key, read at runtime only. **Never** copy its value into this repo, into a committed file, or into any doc.

## Non-negotiable discipline (from README Part 1)

Any code that calls Jev must:
1. Isolate the raw HTTP call in one small function with zero error handling inside it; every caller wraps it in `try/except Exception` and the call must never raise past that boundary.
2. Gate trust in the answer behind a confidence threshold that lives in settings, not hardcoded.
3. Fall through to a safe default (here: `no_op`) on low confidence or any failure — never a riskier action than doing nothing.
4. Validate `choice` against the exact enumerated set offered (and against real ids from the state shown, where relevant).
5. Log every call — hit, miss, low-confidence, failure — with the full question and full raw answer, for audit.

## Project layout

- Game simulation: standalone module, no Jev dependency, `GameState -> apply_action(action) -> GameState`.
- Jev-player loop: separate module, serializes `GameState`, sends one choice question per tick, applies the result.
- Logging: durable local file/db of every tick (question, raw answer, confidence, action applied, resulting state).

Keep these modules decoupled — the simulation must remain testable and watchable with no Jev involved.

## Build → Test → Deploy → JevTest+Feedback loop

This repo uses a repeating four-stage loop, defined in `scripts/loop.sh`, and driven by a scheduled cloud agent (see `.claude/workflows/` or the cron entry — check `CronList` if unsure whether one is active).

Stages, run in order every iteration:

1. **Build** — install/compile whatever the current implementation needs (interpreted Python needs no real build step yet; keep this a no-op until there's something to build).
2. **Test** — run the game-simulation unit tests (physics tick, `apply_action`) with no Jev involved. These must pass before touching Jev at all.
3. **Deploy** — no real deploy target (standalone local research toy). "Deploy" here means: run the current build locally and, if a render exists, publish it as an Artifact so it's viewable. Do not stand up servers or push anywhere external.
4. **JevTest + Feedback** — run N episodes of the Jev-player loop against the simulation, collect the tick-by-tick log, and produce a short feedback summary (success rate, failure modes, confidence-vs-outcome correlation) that becomes input to the next iteration's changes.

Each iteration should end with a short written note (where the loop's outputs land, e.g. `runs/<timestamp>/summary.md`) of what changed and what the JevTest feedback suggested trying next — this is what makes it a loop and not a one-off run.

## What not to do

- Don't connect to Fibril's live daemon, database, or any production system — this repo is fully standalone.
- Don't fine-tune or train Jev on gameplay data.
- Don't give Jev actions outside the fixed per-tick taxonomy.
- Don't skip the honest-reporting step — a landing success rate alone isn't the point; explain *why*.
