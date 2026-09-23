"""Variant D flight instruments, separate from the unchanged game physics.

Code computes navigation targets and one-tick actuator forecasts. Jev chooses
the controls; no candidate is preselected, removed, or substituted after a
valid answer. This is guidance-assisted control, not raw-state piloting.
"""
from __future__ import annotations

import math

from .physics import DT, ROTATE_ACCEL_DEG_S2, THRUST_ACCEL, GameState


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


INSTRUCTIONS = {
    "attitude": {
        "goal": "Select the RCS action whose forecast has the smallest absolute tracking error.",
        "rules": [
            "Tracking error is already computed, including existing angular momentum. Smaller is better.",
            "Compare every candidate, including hold_attitude. Do not infer direction from the action name.",
            "If errors are equal, prefer the smaller actuator percentage.",
        ],
    },
    "throttle": {
        "goal": "Select the engine action whose forecast has the smallest absolute tracking error.",
        "rules": [
            "Tracking error is already computed, including gravity and current tilt. Smaller is better.",
            "Compare every candidate, including no_op. The target already accounts for approach clearance and touchdown speed.",
            "If errors are equal, prefer the smaller actuator percentage.",
        ],
    },
}


def flight_targets(state: GameState) -> dict[str, float]:
    center = (state.pad_x_min + state.pad_x_max) / 2
    offset = state.x - center
    desired_vx = _clamp(-offset * 0.12, -7.0, 7.0)
    lateral_acceleration = _clamp((desired_vx - state.vx) * 0.8, -1.1, 1.1)
    target_angle = math.degrees(math.atan2(lateral_acceleration, state.gravity))
    target_angle *= _clamp(state.y / 8.0, 0.0, 1.0)
    target_spin = _clamp(target_angle - state.angle_deg, -20.0, 20.0)

    # Keep clearance while traversing/braking sideways, then settle onto
    # the pad. A nonzero descent bias prevents asymptotic hovering at y=0.
    target_height = max(0.0, (abs(offset) - 3.0) * 0.35, (abs(state.vx) - 1.0) * 2.0)
    target_vy = _clamp((target_height - state.y) * 0.5 - 0.8, -5.0, 3.0)
    target_vy = max(target_vy, -math.sqrt(1.5**2 + 2 * 0.8 * max(0.0, state.y)))
    return {
        "target_angle_deg": target_angle,
        "target_spin_deg_s": target_spin,
        "target_vertical_speed_m_s": target_vy,
        "approach_height_m": target_height,
    }


def build_payload(state: GameState, model: str) -> dict:
    targets = flight_targets(state)
    attitude = {}
    for tier in range(-4, 5):
        name = "hold_attitude" if tier == 0 else f"rotate_{'left' if tier < 0 else 'right'}_{abs(tier) * 25}"
        spin = state.angle_vel_deg_s + tier / 4 * ROTATE_ACCEL_DEG_S2 * DT
        attitude[name] = {
            "predicted_spin_deg_s": round(spin, 3),
            "absolute_tracking_error": round(abs(spin - targets["target_spin_deg_s"]), 3),
            "actuator_percentage": abs(tier) * 25,
        }
    throttle = {}
    for tier in range(5):
        name = "no_op" if tier == 0 else f"thrust_{tier * 25}"
        acceleration = THRUST_ACCEL * tier / 4 * math.cos(math.radians(state.angle_deg)) if state.fuel > 0 else 0.0
        vy = state.vy + (acceleration - state.gravity) * DT
        throttle[name] = {
            "predicted_vertical_speed_m_s": round(vy, 3),
            "absolute_tracking_error": round(abs(vy - targets["target_vertical_speed_m_s"]), 3),
            "actuator_percentage": tier * 25,
        }
    return {
        "model": model,
        "state": {"flight_targets": {k: round(v, 3) for k, v in targets.items()}},
        "questions": {
            "attitude": {"type": "choice", "instructions": INSTRUCTIONS["attitude"], "criteria": attitude},
            "throttle": {"type": "choice", "instructions": INSTRUCTIONS["throttle"], "criteria": throttle},
        },
    }
