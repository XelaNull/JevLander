from dataclasses import replace
from http.server import ThreadingHTTPServer
import json
import threading

import httpx
import pytest

from jevgame import server
from jevgame.guidance import build_payload
from jevgame.physics import GRAVITY, START_FUEL


@pytest.fixture
def local_server():
    instance = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


def test_only_guided_prompts_and_controls_exposed(local_server):
    with httpx.Client() as client:
        response = client.get(local_server + "/api/prompts")
        assert response.status_code == 200
        questions = response.json()
        assert set(questions) == {"attitude", "throttle"}
        assert len(questions["attitude"]["criteria"]) == 9
        assert len(questions["throttle"]["criteria"]) == 5
        for question in questions.values():
            assert question["state_keys"] == ["flight_targets"]
            assert not set(question).intersection({"A", "B", "C", "D"})
        page_response = client.get(local_server + "/")
        assert page_response.headers["cache-control"] == "no-store"
        page = page_response.text
        for removed in ("variantGroup", "tripleLoopCheck", "prompt-variant-tab", "selectedVariants",
                        "gravitySlider", "gravityValue", "data-duration", "Playback duration"):
            assert removed not in page
        for retained in ("attitudeThresholdSlider", "throttleThresholdSlider", "manualBtn",
                         "distanceSlider", "fuelSlider", "2 control loops"):
            assert retained in page


def test_stream_ignores_retired_selectors_and_preserves_thresholds(local_server, monkeypatch):
    def fake_episode(*, db_path, start_state, attitude_confidence_threshold,
                     throttle_confidence_threshold):
        assert attitude_confidence_threshold == 0
        assert throttle_confidence_threshold == .8
        assert abs(start_state.x) == 110
        assert start_state.fuel == 25
        assert start_state.gravity == GRAVITY
        payload = build_payload(start_state, "test-model")
        steps = [{"step": key, "outcome": "applied", "confidence": .95,
                  "question_payload": payload, "variant": "D"}
                 for key in ("attitude", "throttle")]
        yield "start", start_state, None, "test", {}, None
        final = replace(start_state, tick=1, status="landed")
        yield "tick", final, "no_op", "test", {"applied": 2}, steps
        yield "done", final, None, "test", {"applied": 2}, None

    monkeypatch.setattr(server.episode, "iter_episode", fake_episode)
    events = []
    # Stop on done: the SSE connection deliberately stays open until the client closes it.
    with httpx.stream("GET", local_server + "/api/episode/stream",
                      params={"variants": "A,B,C", "triple_loop": "1",
                              "distance": "110", "fuel": "25", "gravity": "9.81",
                              "attitude_threshold": "0", "throttle_threshold": ".8"}) as response:
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                events.append(event)
                if event["type"] in ("done", "error"):
                    break
    assert events[-1]["type"] == "done"
    start = next(event["frame"] for event in events if event["type"] == "frame")
    assert abs(start["x"]) == 110
    assert start["fuel"] == 25
    tick = [event for event in events if event.get("steps")][0]
    assert [step["step"] for step in tick["steps"]] == ["attitude", "throttle"]
    assert all("question_payload" in step for step in tick["steps"])


@pytest.mark.parametrize("distance,fuel,expected_distance,expected_fuel", [
    (None, None, 60, START_FUEL),
    ("20", "10", 20, 10),
    ("480", "100", 480, 100),
    ("-5", "-1", 20, 10),
    ("9999", "9999", 480, 100),
    ("bad", "bad", 60, START_FUEL),
    ("nan", "inf", 60, START_FUEL),
    ("-inf", "nan", 60, START_FUEL),
    ("", "", 60, START_FUEL),
])
def test_mission_settings(distance, fuel, expected_distance, expected_fuel):
    handler = object.__new__(server.Handler)
    qs = {"seed": ["42"], "gravity": ["9.81"], "duration": ["90"]}
    if distance is not None:
        qs["distance"] = [distance]
    if fuel is not None:
        qs["fuel"] = [fuel]
    state = handler._start_state_from_qs(qs)
    assert abs(state.x) == expected_distance
    assert state.fuel == expected_fuel
    assert state.gravity == GRAVITY
    assert state == handler._start_state_from_qs(qs)
    # Changing mission difficulty must not change seeded terrain or velocity.
    baseline = handler._start_state_from_qs({"seed": ["42"]})
    assert replace(state, x=baseline.x, fuel=baseline.fuel) == baseline


def test_manual_start_uses_same_mission_settings(local_server, monkeypatch):
    monkeypatch.setattr(server, "_MANUAL_SESSIONS", {})
    monkeypatch.setattr(server, "_log_human_tick", lambda **kwargs: None)
    response = httpx.get(local_server + "/api/manual/start",
                         params={"distance": "480", "fuel": "30", "gravity": "9.81"})
    assert response.status_code == 200
    frame = response.json()["frame"]
    assert abs(frame["x"]) == 480
    assert frame["fuel"] == 30
    assert frame["gravity_m_s2"] == GRAVITY
    terrain = response.json()["terrain_profile"]
    assert terrain[0][0] <= -545
    assert terrain[-1][0] >= 545
