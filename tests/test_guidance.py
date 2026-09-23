from dataclasses import replace
import random

import pytest

from jevgame import jev_player
from jevgame.benchmark import wilson_interval
from jevgame.guidance import build_payload
from jevgame.physics import ACTIONS, apply_action, combine_action, initial_state, random_initial_state


@pytest.mark.parametrize("fuel", [0, 100])
def test_forecasts_match_actual_actuators(fuel):
    state = replace(initial_state(angle_deg=23, vy=-3, fuel=fuel), angle_vel_deg_s=-12)
    payload = build_payload(state, "test")
    for key, question in payload["questions"].items():
        for choice, forecast in question["criteria"].items():
            action = "no_op" if choice == "hold_attitude" else choice
            assert action in ACTIONS
            after = apply_action(state, action)
            if key == "attitude":
                assert forecast["predicted_spin_deg_s"] == pytest.approx(after.angle_vel_deg_s, abs=.001)
            else:
                assert forecast["predicted_vertical_speed_m_s"] == pytest.approx(after.vy, abs=.001)


def test_navigation_reference_feasibility():
    """Offline instrumentation check, explicitly NOT a Jev success test."""
    for seed in range(20):
        state = random_initial_state(random.Random(seed))
        while state.status == "flying":
            payload = build_payload(state, "test")
            choices = []
            for q in payload["questions"].values():
                choice = min(q["criteria"], key=lambda name: (
                    q["criteria"][name]["absolute_tracking_error"], q["criteria"][name]["actuator_percentage"],
                ))
                choices.append("" if choice in ("hold_attitude", "no_op") else choice)
            state = apply_action(state, combine_action(*choices))
        assert state.status == "landed", seed


def test_guided_applies_actual_model_choice_without_override(monkeypatch):
    calls = []
    raw = {"answers": {"attitude": {"choice": "rotate_right_25", "confidence": .9},
                       "throttle": {"choice": "thrust_75", "confidence": .8}}}
    monkeypatch.setattr(jev_player, "_read_api_key", lambda: "test-key")
    def call(payload, key):
        calls.append(payload)
        return raw
    monkeypatch.setattr(jev_player, "_call_jev_api", call)
    # Severe tilt would trigger the legacy override; D must honor Jev.
    action, steps = jev_player.choose_action_guided(initial_state(angle_deg=50), attitude_threshold=.55, throttle_threshold=.55)
    assert action == "rotate_right_25_thrust_75"
    assert len(calls) == 1
    assert set(calls[0]["questions"]) == {"attitude", "throttle"}
    assert all(s["raw_answer"] is raw and s["outcome"] == "applied" for s in steps)


@pytest.mark.parametrize("answer,outcome", [
    ({"choice": "invented", "confidence": .99}, "invalid_choice"),
    ({"choice": "rotate_left_25", "confidence": .2}, "discarded_low_confidence"),
    ({"choice": "rotate_left_25", "confidence": float("nan")}, "invalid_answer"),
    ({"choice": "rotate_left_25", "confidence": 1.1}, "invalid_answer"),
    ({}, "invalid_answer"),
])
def test_question_failure_preserves_raw_and_other_control(monkeypatch, answer, outcome):
    raw = {"answers": {"attitude": answer, "throttle": {"choice": "thrust_50", "confidence": .7}}}
    monkeypatch.setattr(jev_player, "_read_api_key", lambda: "test-key")
    monkeypatch.setattr(jev_player, "_call_jev_api", lambda *args: raw)
    action, steps = jev_player.choose_action_guided(initial_state(), attitude_threshold=.55, throttle_threshold=.55)
    assert action == "thrust_50"
    assert steps[0]["outcome"] == outcome
    assert steps[0]["raw_answer"] is raw
    assert steps[1]["outcome"] == "applied"


def test_api_failure_defaults_both_controls(monkeypatch):
    monkeypatch.setattr(jev_player, "_read_api_key", lambda: "test-key")
    def fail(*args):
        raise TimeoutError()
    monkeypatch.setattr(jev_player, "_call_jev_api", fail)
    action, steps = jev_player.choose_action_guided(initial_state(), attitude_threshold=.55, throttle_threshold=.55)
    assert action == "no_op"
    assert all(s["outcome"] == "api_failure" for s in steps)


def test_wilson_interval_does_not_claim_certainty():
    low, high = wilson_interval(30, 30)
    assert .88 < low < .9
    assert high == pytest.approx(1)
    assert wilson_interval(0, 0) == (0, 1)
