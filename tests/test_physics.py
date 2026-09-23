"""Physics-only tests -- no Jev involved (README Part 2 discipline: the
simulation must be testable independently of whether Jev is playing)."""
from jevgame.physics import (
    MAX_TICKS,
    apply_action,
    initial_state,
    terrain_height_at,
)


def test_gravity_pulls_down_with_no_input():
    state = initial_state(y=50.0, vy=0.0)
    next_state = apply_action(state, "no_op")
    assert next_state.vy < 0
    assert next_state.y < state.y


def test_thrust_slows_descent():
    state = initial_state(y=50.0, vy=-5.0)
    coasted = apply_action(state, "no_op")
    thrusted = apply_action(state, "thrust_100")
    assert thrusted.vy > coasted.vy


def test_thrust_consumes_fuel():
    state = initial_state(fuel=10.0)
    next_state = apply_action(state, "thrust_100")
    assert next_state.fuel < state.fuel


def test_thrust_is_a_no_op_when_out_of_fuel():
    state = initial_state(fuel=0.0, vy=-1.0)
    thrusted = apply_action(state, "thrust_100")
    no_op = apply_action(state, "no_op")
    assert thrusted.vy == no_op.vy
    assert thrusted.fuel == 0.0


def test_rotate_changes_angle_only():
    state = initial_state(angle_deg=0.0)
    left = apply_action(state, "rotate_left_100")
    right = apply_action(state, "rotate_right_100")
    assert left.angle_deg < 0
    assert right.angle_deg > 0
    assert left.vx == state.vx and left.vy < state.vy  # only gravity moved it


def test_pad_is_always_flat_regardless_of_terrain_seed():
    for seed in (0, 1, 42, 123456):
        for x in (-10.0, -3.0, 0.0, 7.5, 10.0):
            assert terrain_height_at(x, seed, pad_x_min=-10, pad_x_max=10) == 0.0


def test_soft_landing_over_pad_succeeds():
    state = initial_state(x=0.0, y=0.5, vy=-0.5, vx=0.0, angle_deg=0.0, pad_x_min=-10, pad_x_max=10)
    landed = apply_action(state, "no_op")
    assert landed.status == "landed"


def test_hard_landing_crashes():
    state = initial_state(x=0.0, y=0.5, vy=-50.0, vx=0.0, angle_deg=0.0, pad_x_min=-10, pad_x_max=10)
    crashed = apply_action(state, "no_op")
    assert crashed.status == "crashed"


def test_landing_off_pad_crashes_even_if_gentle():
    # Off-pad terrain height varies with the seed (craters/rolling base), so
    # place the lander exactly at that height for this x/seed rather than
    # assuming flat ground -- the test should hold regardless of terrain.
    pad_x_min, pad_x_max, terrain_seed = -10.0, 10.0, 0
    x = 100.0
    ground_h = terrain_height_at(x, terrain_seed, pad_x_min, pad_x_max)
    state = initial_state(
        x=x, y=ground_h + 0.2, vy=-0.5, vx=0.0, angle_deg=0.0,
        pad_x_min=pad_x_min, pad_x_max=pad_x_max, terrain_seed=terrain_seed,
    )
    crashed = apply_action(state, "no_op")
    assert crashed.status == "crashed"


def test_landing_while_tilted_crashes():
    state = initial_state(x=0.0, y=0.5, vy=-0.5, vx=0.0, angle_deg=45.0, pad_x_min=-10, pad_x_max=10)
    crashed = apply_action(state, "no_op")
    assert crashed.status == "crashed"


def test_crash_with_no_fuel_left_is_flagged_out_of_fuel():
    state = initial_state(x=0.0, y=0.5, vy=-50.0, fuel=0.0, pad_x_min=-10, pad_x_max=10)
    crashed = apply_action(state, "no_op")
    assert crashed.status == "out_of_fuel_crashed"


def test_episode_terminates_within_max_ticks():
    state = initial_state(y=1_000_000.0)  # absurdly high, should hit the tick cutoff first
    ticks = 0
    while state.status == "flying" and ticks <= MAX_TICKS + 1:
        state = apply_action(state, "no_op")
        ticks += 1
    assert state.status == "timeout"
    assert ticks <= MAX_TICKS + 1


def test_apply_action_is_a_noop_once_episode_is_over():
    state = initial_state(y=0.0, vy=0.0, vx=0.0, angle_deg=0.0)
    landed = apply_action(state, "no_op")
    assert landed.status == "landed"
    still_landed = apply_action(landed, "thrust_100")
    assert still_landed == landed
