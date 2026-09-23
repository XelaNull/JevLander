"""Terminal ASCII render of a GameState. No Jev dependency."""
from __future__ import annotations

from .physics import GameState, terrain_height_at

WIDTH = 61   # columns, must be odd so x=0 is centered
HEIGHT = 20  # rows
X_SPAN = 220.0  # world units shown left-to-right (wide enough for the long horizontal traverse)
Y_SPAN = 50.0   # world units shown bottom-to-top


def _to_col(x: float) -> int:
    col = round((x + X_SPAN / 2) / X_SPAN * (WIDTH - 1))
    return max(0, min(WIDTH - 1, col))


def _col_to_x(col: int) -> float:
    return col / (WIDTH - 1) * X_SPAN - X_SPAN / 2


def _to_row(y: float) -> int:
    row = round((HEIGHT - 1) * (1 - y / Y_SPAN))
    return max(0, min(HEIGHT - 1, row))


def render_ascii(state: GameState) -> str:
    grid = [[" "] * WIDTH for _ in range(HEIGHT)]

    pad_start = _to_col(state.pad_x_min)
    pad_end = _to_col(state.pad_x_max)
    for col in range(WIDTH):
        x_at_col = _col_to_x(col)
        ground_h = terrain_height_at(x_at_col, state.terrain_seed, state.pad_x_min, state.pad_x_max)
        ground_row = _to_row(ground_h)
        glyph = "=" if pad_start <= col <= pad_end else "^"
        for row in range(ground_row, HEIGHT):
            grid[row][col] = glyph if row == ground_row else "#"

    glyph = {
        "flying": "@",
        "landed": "V",
        "crashed": "X",
        "out_of_fuel_crashed": "X",
        "timeout": "?",
    }[state.status]
    row, col = _to_row(state.y), _to_col(state.x)
    grid[row][col] = glyph

    lines = ["".join(r) for r in grid]
    status_line = (
        f"tick={state.tick:3d}  x={state.x:6.1f} y={state.y:6.1f}  "
        f"vx={state.vx:5.1f} vy={state.vy:5.1f}  angle={state.angle_deg:5.1f}  "
        f"fuel={state.fuel:5.1f}  status={state.status}"
    )
    return "\n".join(lines) + "\n" + status_line
