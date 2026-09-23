#!/usr/bin/env bash
# Build -> Test -> Deploy -> JevTest+Feedback loop for JevGame.
# See CLAUDE.md for what each stage means.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

RUN_DIR="runs/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN_DIR"
SUMMARY="$RUN_DIR/summary.md"
EPISODES="${JEVGAME_EPISODES:-10}"

log() { echo "$@" | tee -a "$SUMMARY"; }

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

log "== Build =="
.venv/bin/pip install -q -e ".[dev]"
log "Installed jevgame + dev deps into .venv."

log "== Test =="
if .venv/bin/pytest -q 2>&1 | tee -a "$SUMMARY"; then
  log "Physics-only tests passed (no Jev involved)."
else
  log "Physics tests FAILED -- stopping before touching Jev."
  exit 1
fi

log "== Deploy =="
log "No real deploy target (standalone local toy). Running the no-Jev scripted demo to confirm the sim works end to end:"
.venv/bin/python -m jevgame.cli watch --no-render 2>&1 | tail -5 | tee -a "$SUMMARY"

log "== JevTest + Feedback =="
log "Running $EPISODES Jev-piloted episodes..."
.venv/bin/python -m jevgame.cli run --episodes "$EPISODES" --db "$RUN_DIR/jev_calls.db" 2>&1 | tee -a "$SUMMARY"

log ""
log "Run recorded at $SUMMARY"
