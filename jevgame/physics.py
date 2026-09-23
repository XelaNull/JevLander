"""Lunar Lander physics: deterministic, no Jev dependency.

GameState -> apply_action(action) -> GameState. Testable and watchable
entirely on its own (README Part 2, item 1).
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Literal

# Both controls are exposed as enumerated PERCENTAGE tiers, not a binary
# yes/no -- Jev's Choice API returns exactly one enumerated value per
# question (no continuous number, no multi-field answer), so "how much"
# has to live on the same enumerated axis as "whether" rather than being a
# separate question: the "yes" side of each question is expanded into
# yes_25/yes_50/yes_75/yes_100 (thrust_<pct> for the main engine,
# rotate_left_<pct>/rotate_right_<pct> for RCS) instead of a single
# undifferentiated "on". The two axes still combine into one composite
# action string, same as before -- still two concurrent questions, just a
# finer-grained enumeration on each, not a third loop.
THROTTLE_PCTS: tuple[int, ...] = (25, 50, 75, 100)
ROTATE_PCTS: tuple[int, ...] = (25, 50, 75, 100)


def _build_actions() -> tuple[str, ...]:
    attitude_parts = [""]
    for pct in ROTATE_PCTS:
        attitude_parts.append(f"rotate_left_{pct}")
        attitude_parts.append(f"rotate_right_{pct}")
    throttle_parts = [""] + [f"thrust_{pct}" for pct in THROTTLE_PCTS]
    actions = set()
    for a in attitude_parts:
        for t in throttle_parts:
            actions.add(combine_action(a, t))
    return tuple(sorted(actions))


def combine_action(attitude_part: str, throttle_part: str) -> str:
    """Join an attitude part ("" / "rotate_left_<pct>" / "rotate_right_<pct>")
    and a throttle part ("" / "thrust_<pct>") into one composite action
    string -- the single place this joining happens so jev_player.py and
    the manual-play keyboard path can't drift apart on the naming scheme."""
    parts = [p for p in (attitude_part, throttle_part) if p]
    return "_".join(parts) if parts else "no_op"


ACTIONS: tuple[str, ...] = _build_actions()
Action = str  # dynamically generated enumerated set -- see ACTIONS/_build_actions()

_ROTATE_RE = re.compile(r"rotate_(left|right)_(\d+)")
_THROTTLE_RE = re.compile(r"thrust_(\d+)$")


def _rotate_info(action: str) -> tuple[str | None, float]:
    """(direction, fraction) for this action's RCS component, or (None, 0.0)
    if it doesn't rotate. Fraction is 0.25..1.0, from the trailing _<pct>
    on the rotate_left_<pct>/rotate_right_<pct> segment."""
    m = _ROTATE_RE.search(action)
    if not m:
        return None, 0.0
    return m.group(1), int(m.group(2)) / 100.0


def _throttle_frac(action: str) -> float:
    """0.0 if this action doesn't thrust, else the fraction (0.25..1.0)
    encoded in its trailing thrust_<pct> segment."""
    m = _THROTTLE_RE.search(action)
    return int(m.group(1)) / 100.0 if m else 0.0

Status = Literal["flying", "landed", "crashed", "out_of_fuel_crashed", "timeout"]

# Tuned so a reasonable pilot (human or Jev) can land in well under MAX_TICKS.
GRAVITY = 1.62          # m/s^2, moon gravity -- unchanged, this was never the issue
DT = 0.5                # seconds per tick
# RCS is a real thruster, not an angle-snapping dial: firing it builds
# ANGULAR MOMENTUM (angle_vel_deg_s) rather than jumping angle_deg directly,
# and -- because this is vacuum with no aerodynamic damping -- that spin
# persists until the opposite-side thruster is fired to cancel it back out,
# exactly like translational vx/vy. rotate_left_<pct>/rotate_right_<pct>
# apply an angular acceleration impulse for pct% of ROTATE_ACCEL_DEG_S2;
# there is no separate "stop spinning" action, same as there's no separate
# "stop moving sideways" action -- you counter it by firing the other way.
ROTATE_ACCEL_DEG_S2 = 60.0  # deg/s^2 of angular accel at 100% RCS
# THRUST_ACCEL raised from 4.0 -> 6.0: the recorded complaint was "doesn't
# slow its descent fast enough" -- net braking deceleration (THRUST_ACCEL -
# GRAVITY) was only 2.38 m/s^2, a thrust-to-weight ratio of ~2.5:1. Real
# lunar landers (Apollo LM descent stage) run closer to 3:1+. Fuel burn rate
# and starting fuel are UNCHANGED on purpose -- more braking authority means
# less TIME spent thrusting to stop, which is a fuel-efficiency win, not a
# difficulty nerf; the tight fuel budget stays exactly as hard as before.
THRUST_ACCEL = 6.0      # m/s^2 along the lander's nose when thrusting
FUEL_BURN_PER_TICK = 2.0
START_FUEL = 100.0
MAX_TICKS = 400

# Safe-landing thresholds.
MAX_LANDING_SPEED = 3.0   # m/s, combined vx/vy magnitude
# The descent-speed taper below targets THIS at touchdown, not
# MAX_LANDING_SPEED itself -- leaves headroom for whatever residual vx
# remains, since the actual landing check is on the COMBINED vx/vy
# magnitude, not vy alone.
DESCENT_TARGET_AT_GROUND_M_S = 1.5
MAX_LANDING_ANGLE_DEG = 15.0
# Absolute tilt safety limit -- explicit request: never tip past this in
# either direction, and certainly never go flat horizontal (90). This is
# independent of altitude/near_ground (unlike MAX_LANDING_ANGLE_DEG, which
# only matters at touchdown) -- a tilt this severe is dangerous at any
# altitude, not just near the ground.
CRITICAL_TILT_DEG = 45.0

# Derived-field tuning for the "aim" signal exposed in as_dict(). A single-shot
# classifier has no memory of prior ticks, so an open-loop question like "does this
# need correcting" repeated every tick can never tell "corrected enough" from
# "keep going" and will overshoot/oscillate. Instead we compute a fresh
# target angle every tick and expose the *error* against that target, so
# each tick's judgment is self-contained and naturally stops once aligned --
# no memory of past ticks required.
#
# IMPORTANT framing fix: target_angle_deg used to be driven off raw vx alone
# ("cancel current drift to zero"). That let the lander zero out its drift
# far from the pad and then just sit there drifting-free but off-pad forever
# -- confirmed by testing a hand-written scripted policy with the same
# vx-only framing against the randomized start scenarios: it only landed
# 2/8, and every crash showed near-zero final vx but large final |x| (e.g.
# x=28.8, x=-20.8 against a pad at [-10,10]). Re-running the same scenario
# set with a position-aware policy -- targeting a *desired* vx proportional
# to distance from the pad, not just zero -- landed 4/8 instead. So the
# state fed to Jev needs the same position-aware target, not a physics
# retune: PAD_APPROACH_GAIN converts distance-from-pad-center into a desired
# closing velocity, and the angle/orientation signal is driven off the ERROR
# between current vx and that desired vx, not off vx alone.
PAD_APPROACH_GAIN = 0.3    # desired closing velocity (m/s) per meter of pad offset
MAX_APPROACH_SPEED = 6.0   # cap on desired closing velocity at high altitude
FLARE_GAIN = 0.3           # additional cap: desired closing speed <= FLARE_GAIN * altitude,
                            # so it decays toward 0 by touchdown regardless of pad distance
# Horizontal counterpart to the altitude-based flare above -- without this,
# desired_vx only starts tapering below MAX_APPROACH_SPEED/PAD_APPROACH_GAIN
# = 20m from the pad (the proportional term's own natural cap kicks in),
# so ground speed stayed at its max right up until quite close in. Explicit
# request: start slowing ground speed by at least two landing-pad widths
# out, so the taper distance is dynamic (scales with the actual pad_x_min/
# pad_x_max span for this episode), not a fixed meter value -- "two pad
# widths" is the real spec, a flat 60m happened to satisfy it only for the
# default 20m-wide pad.
HORIZONTAL_FLARE_PAD_WIDTHS = 2.0
# Vertical counterpart to FLARE_GAIN -- a smooth envelope for how fast
# descent_speed_m_s is ALLOWED to be at a given altitude, easing off well
# before braking_margin_m would force a hard correction, so the whole final
# approach reads as one continuous taper (fast up high, slower and slower
# closer in, near zero at touchdown) in BOTH axes at once instead of just a
# late braking_margin_m-triggered scramble in the vertical one.
MAX_DESCENT_SPEED_HIGH_ALT = 12.0  # allowed descent speed at/above DESCENT_FLARE_ALTITUDE_M
DESCENT_FLARE_ALTITUDE_M = 40.0    # altitude at which the taper begins
AIM_GAIN_DEG_PER_MS = 4.0  # degrees of counter-tilt per m/s of (closing-)velocity error
MAX_AIM_ANGLE_DEG = 30.0
AIM_DEADBAND_DEG = 8.0     # within this error, orientation counts as "good enough"
# Same role as BRAKING_SAFETY_FACTOR but for rotation: gives lead time to
# start countering angular momentum before it actually overshoots the
# target, since decisions are only made once per tick.
AIM_STOPPING_SAFETY_FACTOR = 1.5
BRAKING_SAFETY_FACTOR = 5.0  # lead-time buffer on the braking-distance threshold (see as_dict())
# Re-tuned by grid search over a scripted policy (no Jev calls) across 4
# seeds x 8 episodes for the cratered-terrain, long-horizontal-traverse
# scenario: safety=5.0/band=5.0 was the best combination found (14/32
# landed). Lower than the flat-ground/vertical-drop tuning (24/32) on
# purpose, not a regression -- craters are now a real collision hazard
# during low-altitude traverse (two of the remaining crashes on seed=7 are
# the lander hitting a crater wall, not a bad landing), and fuel is
# unchanged/still tight per the "keep that the hard part" instruction.
# Swept safety in {2.0..6.0} x band in {3.0..12.0}.
# Hysteresis band: a plain single threshold on braking_margin_m chatters --
# traced case: thrust for one tick, margin swings back to +0.82 (deceleration
# from that one tick is enough to look "safe" again), so it lets off, falls
# again, re-triggers seconds later, and the accumulated gaps meant the final
# touchdown speed still exceeded MAX_LANDING_SPEED. Classic bang-bang
# controller chattering. Fix: once thrust has engaged, require the margin to
# clear a wider positive band before recommending stopping, not just >= 0.
BRAKING_HYSTERESIS_BAND_M = 5.0
# How far below zero braking_margin_m has to fall before the throttle
# question should escalate from a half-burn to a full-burn -- gives Jev a
# second enumerated "strength" tier instead of a single always-full thrust.
# First value tried (8.0) was too permissive: braking_margin_m in practice
# oscillates in roughly the 0..-8 band once thrust starts correcting it, so
# it almost never actually crossed -8 -- the throttle question got stuck
# permanently on thrust_half (net decel ~1.38 m/s^2, well under the ~4.38
# m/s^2 physics was tuned around), and landing rate on the standard JevTest
# scenario collapsed from ~62.5% to 0% (10/10 crashed, confirmed via
# runs/20260922-204724/jev_calls.db). Lowered so full thrust engages while
# there's still real margin to use it.
BRAKING_MARGIN_HARD_M = 2.0

# How soon (in projected seconds-to-impact) an undershoot has to be before
# needs_more_vertical_thrust treats it as urgent. Without this gate the
# no-further-input projection is true for most of an ordinary flight (see
# needs_more_vertical_thrust comment in as_dict) and forced thrust_100
# almost constantly -- confirmed via runs/server_jev_calls.db: 3 real
# episodes averaged needs_more_vertical_thrust=true on ~65% of ticks,
# thrust_100 tracking it almost 1:1. This turns it back into a real alarm.
NEEDS_VERTICAL_THRUST_HORIZON_S = 4.0

# Final-approach thresholds -- see near_pad/overshoot_alert in as_dict().
NEAR_PAD_DISTANCE_M = 20.0
OVERSHOOT_GROUND_SPEED_M_S = 3.0

# Hard final-approach speed cap: explicit request, not derived from the
# smooth distance-based taper above -- ground speed must be under 2 m/s
# once within 10m of the pad, a tighter/closer-in rule than
# should_slow_ground_speed's own smoother taper (which starts easing off
# much farther out but doesn't guarantee any specific speed at any specific
# distance). Exposed as its own signal so it can be a hard override in the
# prompt, not folded into should_slow_ground_speed's existing threshold.
FINAL_APPROACH_DISTANCE_M = 10.0
FINAL_APPROACH_MAX_GROUND_SPEED_M_S = 2.0

# A second, wider/softer checkpoint on the same explicit-numeric-spec
# pattern as FINAL_APPROACH_* above: within 30m, ground speed should already
# be down to 5 m/s -- catches it earlier than the 10m/2m/s hard cap, and
# independent of should_slow_ground_speed's own smoother distance-scaled
# taper (which starts easing off farther out but doesn't guarantee any
# specific speed at any specific distance).
MID_APPROACH_DISTANCE_M = 30.0
MID_APPROACH_MAX_GROUND_SPEED_M_S = 5.0

# A single continuous "how heavy-handed can corrections be right now"
# signal, 1.0 (far out -- pick full-strength tiers freely, precision
# doesn't matter yet, there's plenty of room/time to correct again) down to
# 0.0 (right at the pad -- only gentle/precise corrections, an overcorrection
# here is costly and hard to walk back). Meant to replace reasoning strength
# tiers from several separate distance thresholds (near_pad, the two
# approach checkpoints, horizontal_flare_distance) -- one number the prompt
# can anchor "how bold should this correction be" to, instead of Jev having
# to reconcile multiple different distance-gated rules each implying their
# own answer to the same underlying question.
BOLDNESS_REFERENCE_DISTANCE_M = 60.0
ALTITUDE_BOLDNESS_REFERENCE_M = 30.0

# Below this altitude, being upright matters more than the ongoing
# horizontal-correction tilt -- touching down tilted risks tipping the
# lander over regardless of how well-judged the horizontal correction was.
# This is exposed as a signal (near_ground) for the ATTITUDE PROMPT to act
# on as a priority rule, not baked into target_angle_deg itself -- per
# request, behavior tuning here should live in Jev's instructions, not in
# more physics-layer overrides.
# Raised from 15 -> 25: real tuning data (this session) showed touchdown
# angle and speed both converging nicely but speed consistently landing at
# 5-9 m/s combined vs the 3.0 m/s limit -- more altitude/time margin for the
# near_ground "be upright and slow down" behaviors to fully take hold before
# actually touching down, rather than starting the final push too late.
NEAR_GROUND_ALTITUDE_M = 25.0

# Terrain: a moonscape of craters and rolling pockmarks instead of flat
# ground at y=0, with the landing pad as a guaranteed-flat spot among it
# (per-episode, seeded so the simulation stays a pure function of state --
# no hidden mutable terrain array). The pad is forced flat and craters/base
# noise blend smoothly up to full height over PAD_EDGE_MARGIN so there's no
# cliff exactly at the pad boundary.
NUM_CRATERS = 8
CRATER_MIN_RADIUS = 4.0
CRATER_MAX_RADIUS = 14.0
CRATER_MIN_DEPTH = 2.0
CRATER_MAX_DEPTH = 9.0
TERRAIN_X_RANGE = (-140.0, 140.0)  # where craters may be centered
PAD_EDGE_MARGIN = 8.0              # meters outside the pad over which terrain blends in


@lru_cache(maxsize=64)
def _generate_craters(terrain_seed: int) -> tuple[tuple[float, float, float], ...]:
    """(center_x, radius, depth) for each crater, deterministic per seed."""
    rng = random.Random(terrain_seed)
    craters = []
    for _ in range(NUM_CRATERS):
        cx = rng.uniform(*TERRAIN_X_RANGE)
        radius = rng.uniform(CRATER_MIN_RADIUS, CRATER_MAX_RADIUS)
        depth = rng.uniform(CRATER_MIN_DEPTH, CRATER_MAX_DEPTH)
        craters.append((cx, radius, depth))
    return tuple(craters)


def terrain_height_at(x: float, terrain_seed: int, pad_x_min: float, pad_x_max: float) -> float:
    """Ground height under a given x. The pad itself is always exactly 0.0
    (required for the landing-safety logic to keep meaning what it says);
    outside it, a rolling seeded base plus crater bowls, blended up from 0
    over PAD_EDGE_MARGIN so there's no discontinuity at the pad edge."""
    if pad_x_min <= x <= pad_x_max:
        return 0.0

    base = 1.2 * math.sin(x * 0.09 + terrain_seed * 0.31) + 0.6 * math.sin(x * 0.23 + terrain_seed * 2.1)
    height = base
    for cx, radius, depth in _generate_craters(terrain_seed):
        dx = x - cx
        if abs(dx) < radius * 3:
            height -= depth * math.exp(-(dx * dx) / (2 * radius * radius))

    dist_outside_pad = (pad_x_min - x) if x < pad_x_min else (x - pad_x_max)
    if dist_outside_pad < PAD_EDGE_MARGIN:
        height *= dist_outside_pad / PAD_EDGE_MARGIN
    return height


@dataclass(frozen=True)
class GameState:
    x: float
    y: float
    vx: float
    vy: float
    angle_deg: float       # 0 = upright, positive = tilted right
    fuel: float
    terrain_seed: int
    pad_x_min: float
    pad_x_max: float
    tick: int
    status: Status
    # Per-episode gravity (m/s^2), defaulting to GRAVITY (moon).
    # The web game fixes this to Moon; simulations can still set it explicitly.
    gravity: float = GRAVITY
    # Own-actuator telemetry, not "memory" of past judgment: a real
    # spacecraft always knows whether its own engine is currently firing.
    # Feeding this back lets the braking-margin threshold apply hysteresis
    # (see BRAKING_HYSTERESIS_BAND_M) without asking Jev to remember
    # anything about earlier ticks itself.
    was_thrusting: bool = False
    # Angular momentum -- RCS builds this up, it doesn't snap angle_deg
    # directly (see ROTATE_ACCEL_DEG_S2 comment). Own-actuator telemetry
    # like was_thrusting: a real spacecraft always knows its own spin rate.
    angle_vel_deg_s: float = 0.0

    def as_dict(self) -> dict:
        ground_height = terrain_height_at(self.x, self.terrain_seed, self.pad_x_min, self.pad_x_max)
        altitude = self.y - ground_height
        pad_center_x = (self.pad_x_min + self.pad_x_max) / 2
        x_offset = self.x - pad_center_x

        # Trajectory projection, computed early so both the ground-speed and
        # descent-speed tapers below can use it as a guard -- "if no further
        # thrust or rotation happens, where does the CURRENT path lead, and
        # how soon?" -- a forward-looking ballistic extrapolation (constant
        # vx, vy decaying under gravity) of the current x/y/vx/vy, distinct
        # from braking_margin_m (which asks "is there still room to stop")
        # and desired_vx (which asks "what speed should I be closing at").
        # Terrain isn't flat, so the impact point is refined a few times
        # against the actual ground height under the projected x each pass.
        proj_x, proj_t, proj_ground_guess = self.x, 0.0, ground_height
        for _ in range(4):
            # Solve 0 = (y - proj_ground_guess) + vy*t - 0.5*g*t^2 for the
            # positive root, assuming ground stays flat at the current best
            # guess; then refit that guess against the real terrain under
            # the resulting x and repeat -- converges quickly since terrain
            # is smooth relative to how far one tick's velocity travels.
            fall_height = max(0.0, self.y - proj_ground_guess)
            disc = self.vy * self.vy + 2 * self.gravity * fall_height
            proj_t = max(0.0, (self.vy + math.sqrt(disc)) / self.gravity) if self.gravity > 0 else 0.0
            proj_x = self.x + self.vx * proj_t
            proj_ground_guess = terrain_height_at(proj_x, self.terrain_seed, self.pad_x_min, self.pad_x_max)
        projected_x_offset = proj_x - pad_center_x

        # Flare: cap closing speed by remaining altitude too, not just by the
        # fixed MAX_APPROACH_SPEED, so desired_vx decays toward 0 on final
        # approach instead of still calling for a fast close right down to
        # touchdown (observed failure: over_pad=True crashes landing at
        # |vx|~3.2-3.5, well over MAX_LANDING_SPEED, because desired_vx never
        # tapered near the ground).
        altitude_capped_speed = min(MAX_APPROACH_SPEED, FLARE_GAIN * altitude)
        # Same idea, but by remaining HORIZONTAL distance to the pad --
        # scales the allowed closing speed down smoothly starting at
        # horizontal_flare_distance out (two pad widths, see
        # HORIZONTAL_FLARE_PAD_WIDTHS), so ground speed is already easing
        # off well before the final stretch, not just in the last ~20m
        # where the proportional term alone would start to bite.
        pad_width = self.pad_x_max - self.pad_x_min
        horizontal_flare_distance = HORIZONTAL_FLARE_PAD_WIDTHS * pad_width
        # Piecewise-linear, anchored exactly at two explicit checkpoints
        # (2 m/s by 10m, 5 m/s by 30m) instead of a single proportional
        # taper plus separate hard-gated booleans layered on top -- the
        # earlier version exposed should_slow_ground_speed AND two more
        # boolean checkpoints (too_fast_for_mid/final_approach) as
        # independent prompt rules, which regressed confidence across every
        # variant (confirmed via runs/20260922-222702: attitude discard rate
        # rose to 61-72% across A/B/C, including C which had been immune to
        # prompt bloat) -- too many overlapping, sometimes-conflicting
        # priority rules for the same underlying "how fast can I still be
        # going" question. Folding both checkpoints directly into the ONE
        # curve everything else already reads (closing_speed_cap /
        # should_slow_ground_speed) keeps the exact same numeric guarantees
        # without adding rule-stack complexity.
        d = abs(x_offset)
        if d <= FINAL_APPROACH_DISTANCE_M:
            distance_capped_speed = FINAL_APPROACH_MAX_GROUND_SPEED_M_S * (d / FINAL_APPROACH_DISTANCE_M) if FINAL_APPROACH_DISTANCE_M > 0 else 0.0
        elif d <= MID_APPROACH_DISTANCE_M:
            frac = (d - FINAL_APPROACH_DISTANCE_M) / (MID_APPROACH_DISTANCE_M - FINAL_APPROACH_DISTANCE_M)
            distance_capped_speed = FINAL_APPROACH_MAX_GROUND_SPEED_M_S + (MID_APPROACH_MAX_GROUND_SPEED_M_S - FINAL_APPROACH_MAX_GROUND_SPEED_M_S) * frac
        else:
            far = max(horizontal_flare_distance, MID_APPROACH_DISTANCE_M + 1.0)
            frac = min(1.0, (d - MID_APPROACH_DISTANCE_M) / (far - MID_APPROACH_DISTANCE_M))
            distance_capped_speed = MID_APPROACH_MAX_GROUND_SPEED_M_S + (MAX_APPROACH_SPEED - MID_APPROACH_MAX_GROUND_SPEED_M_S) * frac
        closing_speed_cap = min(altitude_capped_speed, distance_capped_speed)
        desired_vx = max(-closing_speed_cap, min(closing_speed_cap, -x_offset * PAD_APPROACH_GAIN))
        vx_error = self.vx - desired_vx
        # Explicit, hard trigger (not just the soft pull through vx_error/
        # target_angle_deg above) -- within two pad widths AND still going
        # faster than the taper allows means "slow the ground speed down
        # NOW", surfaced the same way needs_more_vertical_thrust/
        # overshoot_alert are: as a named boolean Jev's criteria can act on
        # directly, since relying only on the indirect pull through
        # aim_error_deg wasn't producing an early enough reaction in
        # practice.
        ground_speed_excess = max(0.0, abs(self.vx) - closing_speed_cap)
        should_slow_ground_speed = abs(x_offset) <= horizontal_flare_distance and ground_speed_excess > 0
        target_angle_deg = max(-MAX_AIM_ANGLE_DEG, min(MAX_AIM_ANGLE_DEG, -vx_error * AIM_GAIN_DEG_PER_MS))
        aim_error_deg = self.angle_deg - target_angle_deg
        # Angular stopping margin: same shape of signal as braking_margin_m
        # below, applied to rotation now that RCS builds momentum instead of
        # snapping angle_deg directly. If angle_vel_deg_s is carrying
        # angle_deg TOWARD target_angle_deg, check whether there's still
        # room to stop the spin before overshooting past it (using RCS's own
        # deceleration authority); if angle_vel_deg_s is carrying it AWAY
        # from target (or angle_deg is already dead-on but still spinning),
        # that's unambiguously "need to counter-fire now" regardless of how
        # small aim_error_deg looks this instant.
        moving_toward_target = (aim_error_deg > 0 and self.angle_vel_deg_s < 0) or (
            aim_error_deg < 0 and self.angle_vel_deg_s > 0
        )
        if moving_toward_target:
            closing_rate = abs(self.angle_vel_deg_s)
            rotate_stopping_deg = (closing_rate * closing_rate) / (2 * ROTATE_ACCEL_DEG_S2)
            aim_stopping_margin = abs(aim_error_deg) - rotate_stopping_deg * AIM_STOPPING_SAFETY_FACTOR
        elif self.angle_vel_deg_s == 0:
            aim_stopping_margin = abs(aim_error_deg)  # not spinning -- ordinary aim_error_deg applies
        else:
            # Spinning away from target (or dead-on but still spinning): no
            # amount of aim_error_deg makes this safe, counter now.
            aim_stopping_margin = -abs(self.angle_vel_deg_s) - 1.0
        # Braking-distance signal. A first attempt used "free-fall impact
        # speed" (v^2 = vy^2 + 2*g*altitude) -- physically real, but almost
        # always alarming from any real altitude (from y=100 that's ~18 m/s)
        # even when there's plenty of time left to brake later. Jev correctly
        # read that as "always thrust" and burned out of fuel in every single
        # test episode (~205/300 ticks, 96% thrust_main, still 0% landed).
        # The actual decision-relevant question is stopping distance vs.
        # remaining altitude: how much altitude is needed to decelerate from
        # current vy down to a safe landing speed using available thrust,
        # compared to how much altitude is actually left. Only alarming once
        # that margin goes negative -- naturally near-zero at any sane
        # altitude when descending slowly, unlike the free-fall version.
        net_decel = THRUST_ACCEL - self.gravity  # upward accel available once thrusting
        excess_speed = max(0.0, -self.vy - MAX_LANDING_SPEED)  # how far |vy| exceeds safe, in m/s
        stopping_distance = (excess_speed * (excess_speed + 2 * MAX_LANDING_SPEED)) / (2 * net_decel)
        # BRAKING_SAFETY_FACTOR: the exact stopping distance has zero slack,
        # and decisions are only made once per DT=0.5s tick -- between one
        # "still safe" tick and the next, freefall keeps accelerating, so an
        # agent that waits for the margin to hit exactly 0 before thrusting
        # is already too late by the time it sees a negative margin (verified
        # by tracing a crash: margin went from +0.94 to -7.61 in one tick).
        # The safety factor gives lead time so "brake now" fires while there
        # is still actually enough room to.
        braking_margin = altitude - stopping_distance * BRAKING_SAFETY_FACTOR
        if self.was_thrusting:
            # Hysteresis: already braking -- keep the margin biased negative
            # (i.e. "still thrust") until there's real daylight above the
            # threshold, not just the instant it ticks non-negative. This is
            # applied to the SAME field Jev reads, so the thrust question's
            # rule ("thrust_main exactly when braking_margin_m < 0") stays a
            # simple sign check; the asymmetry is baked into the number, not
            # into Jev's judgment.
            braking_margin -= BRAKING_HYSTERESIS_BAND_M

        # Descent-speed taper: the vertical counterpart to desired_vx's
        # FLARE_GAIN cap above -- a smoothly SHRINKING allowed descent speed
        # as altitude drops, scaling linearly from MAX_DESCENT_SPEED_HIGH_ALT
        # at/above DESCENT_FLARE_ALTITUDE_M down to MAX_LANDING_SPEED right
        # at the ground. braking_margin_m above is the hard safety net (don't
        # crash); this is a softer, earlier nudge so the whole final approach
        # reads as one continuous bell-curve taper in BOTH axes together,
        # not just a late scramble once braking_margin_m goes negative.
        # Tapers to DESCENT_TARGET_AT_GROUND_M_S (well under MAX_LANDING_SPEED
        # itself), not all the way to MAX_LANDING_SPEED -- landing needs the
        # COMBINED vx/vy magnitude under MAX_LANDING_SPEED, not vy alone, so
        # tapering vy exactly to the limit left zero margin for any residual
        # vx and real runs were touching down at vy right at ~3.0 with even
        # a small vx pushing the combined speed over. This leaves headroom.
        descent_taper_frac = max(0.0, min(1.0, altitude / DESCENT_FLARE_ALTITUDE_M))
        desired_descent_speed = DESCENT_TARGET_AT_GROUND_M_S + (
            MAX_DESCENT_SPEED_HIGH_ALT - DESCENT_TARGET_AT_GROUND_M_S
        ) * descent_taper_frac
        descent_speed_excess = max(0.0, -self.vy - desired_descent_speed)

        # Undershoot: given which side of the pad we're approaching from,
        # would the projected touchdown happen BEFORE horizontally reaching
        # the pad? That's the concrete, directional version of "gravity is
        # winning the race against the horizontal distance left to cover" --
        # more vertical thrust now (slowing the descent) buys more time to
        # cover that distance before touching down, which is exactly what a
        # gauge reading "still short of the pad when you land" should tell
        # a pilot (human or Jev) to do. Zero once already over the pad.
        if x_offset > 0:       # approaching from the right, traveling -x
            projected_shortfall = proj_x - self.pad_x_max
        elif x_offset < 0:     # approaching from the left, traveling +x
            projected_shortfall = self.pad_x_min - proj_x
        else:
            projected_shortfall = 0.0
        projected_shortfall = max(0.0, projected_shortfall)
        # This projection assumes NO FURTHER INPUT for the rest of the
        # flight -- which is true almost the entire time for a completely
        # ordinary, still-correctable flight (of course a ballistic fall
        # from 30m up with no more thrust ever again lands short). Without
        # a time gate this was true on ~60% of ticks across real episodes
        # and dragged thrust_100 along with it almost 1:1, i.e. it was
        # firing on "you'll eventually need to act" instead of "act now."
        # Gating it to only the ticks where impact is actually imminent
        # turns it into a real urgency alarm instead of background noise.
        needs_more_vertical_thrust = (
            projected_shortfall > 0 and proj_t <= NEEDS_VERTICAL_THRUST_HORIZON_S
        )

        # Final-approach signals: close to the pad, the priorities flip --
        # precision (controlled ground speed, level angle) matters more than
        # raw braking/thrust authority, and a wrong-direction drift needs to
        # be caught immediately rather than a tick or two later. near_pad on
        # its own tells the throttle question to favor gentler burns;
        # overshoot_alert is the "you're drifting the WRONG way, act now"
        # case -- either still closing from one side but already moving back
        # away from the pad, or already over the pad and still carrying
        # enough ground speed to fly past it to the other side.
        near_pad = abs(x_offset) < NEAR_PAD_DISTANCE_M
        wrong_direction = (x_offset > 0 and self.vx > 0) or (x_offset < 0 and self.vx < 0)
        over_pad_fast = self.pad_x_min <= self.x <= self.pad_x_max and abs(self.vx) > OVERSHOOT_GROUND_SPEED_M_S
        overshoot_alert = near_pad and (wrong_direction or over_pad_fast)
        too_fast_for_final_approach = abs(x_offset) < FINAL_APPROACH_DISTANCE_M and abs(self.vx) > FINAL_APPROACH_MAX_GROUND_SPEED_M_S
        final_approach_speed_excess = max(0.0, abs(self.vx) - FINAL_APPROACH_MAX_GROUND_SPEED_M_S) if abs(x_offset) < FINAL_APPROACH_DISTANCE_M else 0.0
        too_fast_for_mid_approach = abs(x_offset) < MID_APPROACH_DISTANCE_M and abs(self.vx) > MID_APPROACH_MAX_GROUND_SPEED_M_S
        mid_approach_speed_excess = max(0.0, abs(self.vx) - MID_APPROACH_MAX_GROUND_SPEED_M_S) if abs(x_offset) < MID_APPROACH_DISTANCE_M else 0.0
        near_ground = altitude < NEAR_GROUND_ALTITUDE_M
        control_boldness = min(1.0, abs(x_offset) / BOLDNESS_REFERENCE_DISTANCE_M) if BOLDNESS_REFERENCE_DISTANCE_M > 0 else 1.0
        # Same idea, but for ROTATION specifically, keyed on ALTITUDE rather
        # than horizontal distance -- rotation's real risk is at touchdown,
        # not at the pad's horizontal edge, so "close to the ground" for
        # attitude purposes means low altitude. A full-power RCS burn is
        # fine high up (plenty of altitude to absorb any overcorrection);
        # the same full-power burn right at touchdown altitude risks tipping
        # the lander over from the correction itself, not just from the
        # thing it was correcting.
        altitude_boldness = min(1.0, altitude / ALTITUDE_BOLDNESS_REFERENCE_M) if ALTITUDE_BOLDNESS_REFERENCE_M > 0 else 1.0

        # Thrust is NOSE-RELATIVE (vx += sin(angle_deg) * THRUST_ACCEL, see
        # apply_action) -- firing the main engine while badly tilted doesn't
        # just waste fuel, it actively pushes horizontal velocity the WRONG
        # way if the tilt points off toward the side already being drifted
        # away from. The throttle question only looks at vertical braking
        # margin; this tells it when "should I burn" also depends on
        # whether the CURRENT angle_deg would help or hurt if it does.
        thrust_horizontal_component = math.sin(math.radians(self.angle_deg))
        thrust_would_push_wrong_way = (
            (thrust_horizontal_component > 0 and desired_vx < 0)
            or (thrust_horizontal_component < 0 and desired_vx > 0)
        )

        return {
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "vx": round(self.vx, 2),
            "vy": round(self.vy, 2),
            # Same numbers as vx/vy, reframed so the sign/magnitude convention
            # doesn't have to be inferred: descent_speed_m_s is positive while
            # FALLING (negative vy) and negative while rising, ground_speed_m_s
            # is the unsigned horizontal speed regardless of which way that is
            # (direction is already covered separately by x_offset_from_pad_center
            # /desired_vx) -- two explicit telemetry readings rather than making
            # Jev (or a human reading the UI) re-derive them from raw vx/vy.
            "descent_speed_m_s": round(-self.vy, 2),
            # The smooth altitude-based taper described above: how fast
            # descent_speed_m_s is allowed to be right now, and by how much
            # it's currently over that -- a positive excess is an early,
            # softer version of "ease off," well before braking_margin_m
            # would force it.
            "desired_descent_speed_m_s": round(desired_descent_speed, 2),
            "descent_speed_excess_m_s": round(descent_speed_excess, 2),
            "ground_speed_m_s": round(abs(self.vx), 2),
            "angle_deg": round(self.angle_deg, 1),
            # RCS builds momentum rather than snapping angle_deg (see
            # ROTATE_ACCEL_DEG_S2) -- angle_vel_deg_s is that spin rate, and
            # it does NOT self-correct. Same shape of problem as vy/braking:
            # a nonzero value here means angle_deg will keep changing on its
            # own even with no further input, and it takes an OPPOSITE-side
            # RCS burn to cancel it back to zero, not just stopping input.
            "angle_vel_deg_s": round(self.angle_vel_deg_s, 2),
            # < 0 means: counter-fire the RCS now, either because the spin
            # is carrying angle_deg away from target_angle_deg, or because
            # (even while still closing on it) there isn't enough stopping
            # room left to cancel the spin before overshooting past it.
            "aim_stopping_margin_deg": round(aim_stopping_margin, 2),
            "aim_needs_counter_thrust": aim_stopping_margin < 0,
            "fuel": round(self.fuel, 1),
            "gravity_m_s2": round(self.gravity, 3),
            "altitude": round(altitude, 2),
            "pad_x_min": self.pad_x_min,
            "pad_x_max": self.pad_x_max,
            "over_pad": self.pad_x_min <= self.x <= self.pad_x_max,
            # Position-aware navigation signal: desired_vx is the closing
            # velocity toward the pad (not zero) that current x_offset calls
            # for, and target_angle_deg/aim_error_deg are driven off the
            # error against THAT, not off raw vx -- so zeroing drift far from
            # the pad no longer counts as "aimed well". See PAD_APPROACH_GAIN
            # comment above for why this replaced a drift-only framing.
            "x_offset_from_pad_center": round(x_offset, 2),
            "desired_vx": round(desired_vx, 2),
            # Explicit "slow the ground speed down now" alarm: true within
            # two pad widths of the pad while ground_speed_m_s exceeds what
            # the smooth distance-based taper allows at this range.
            "ground_speed_excess_m_s": round(ground_speed_excess, 2),
            "should_slow_ground_speed": should_slow_ground_speed,
            "target_angle_deg": round(target_angle_deg, 1),
            "aim_error_deg": round(aim_error_deg, 1),
            "aim_deadband_deg": AIM_DEADBAND_DEG,
            # Braking-distance margin: negative means there isn't enough
            # altitude left to decelerate to a safe landing speed using
            # available thrust -- brake now, not later.
            "stopping_distance_m": round(stopping_distance, 2),
            "braking_margin_m": round(braking_margin, 2),
            "braking_margin_hard_m": BRAKING_MARGIN_HARD_M,
            # Trajectory projection -- where the CURRENT path (no further
            # thrust/rotation) leads: how long until it hits the ground,
            # where, and how far that projected point is from the pad, so
            # "the path I'm on" is a number Jev reads fresh each tick, not
            # something it has to remember or infer from raw vx/vy alone.
            "projected_impact_time_s": round(proj_t, 2),
            "projected_impact_x_m": round(proj_x, 2),
            "projected_distance_from_pad_m": round(projected_x_offset, 2),
            "projected_over_pad": self.pad_x_min <= proj_x <= self.pad_x_max,
            # Directional undershoot gauge: > 0 means the current path would
            # touch down BEFORE horizontally reaching the pad -- descending
            # faster than horizontal progress allows. More vertical thrust
            # (slow the descent) buys time to cover the remaining distance.
            "projected_shortfall_m": round(projected_shortfall, 2),
            "needs_more_vertical_thrust": needs_more_vertical_thrust,
            # Final-approach precision: near_pad true means "favor gentle,
            # controlled corrections over raw power from here on";
            # overshoot_alert true means "you're drifting the WRONG way (or
            # about to fly past the pad) -- react at full strength now,
            # don't wait for it to look severe."
            "near_pad": near_pad,
            "near_ground": near_ground,
            "control_boldness": round(control_boldness, 2),
            "altitude_boldness": round(altitude_boldness, 2),
            "overshoot_alert": overshoot_alert,
            "too_fast_for_final_approach": too_fast_for_final_approach,
            "final_approach_speed_excess_m_s": round(final_approach_speed_excess, 2),
            "too_fast_for_mid_approach": too_fast_for_mid_approach,
            "mid_approach_speed_excess_m_s": round(mid_approach_speed_excess, 2),
            # True means: firing the main engine THIS tick, given the
            # current tilt, pushes horizontally away from the pad rather
            # than toward it -- because thrust is nose-relative, a bad tilt
            # makes burning actively counterproductive, not just wasteful.
            "thrust_would_push_wrong_way": thrust_would_push_wrong_way,
            "was_thrusting": self.was_thrusting,
            "tick": self.tick,
            "status": self.status,
        }


def initial_state(
    *,
    x: float = 0.0,
    y: float = 100.0,
    vx: float = 0.0,
    vy: float = 0.0,
    angle_deg: float = 0.0,
    fuel: float = START_FUEL,
    pad_x_min: float = -10.0,
    pad_x_max: float = 10.0,
    terrain_seed: int = 0,
    gravity: float = GRAVITY,
) -> GameState:
    return GameState(
        x=x, y=y, vx=vx, vy=vy, angle_deg=angle_deg, fuel=fuel,
        terrain_seed=terrain_seed, pad_x_min=pad_x_min, pad_x_max=pad_x_max,
        tick=0, status="flying", gravity=gravity,
    )


DEFAULT_MIN_START_DISTANCE = 25.0
DEFAULT_MAX_START_DISTANCE = 90.0


def random_initial_state(
    rng, *,
    min_distance: float = DEFAULT_MIN_START_DISTANCE,
    max_distance: float = DEFAULT_MAX_START_DISTANCE,
    gravity: float = GRAVITY,
    fuel: float = START_FUEL,
) -> GameState:
    """A randomized start state: the lander begins to one side of the pad,
    already moving toward it mostly horizontally (a side-scroller-style
    approach shot over cratered terrain), rather than dropping mostly
    straight down. `min_distance`/`max_distance` set how far from the pad
    center it starts; equal bounds give an exact horizontal distance.
    `fuel` sets the starting supply without changing fuel consumption.
    Each episode gets its own random terrain (see terrain_height_at)."""
    side = rng.choice((-1.0, 1.0))
    distance = rng.uniform(min_distance, max_distance)
    return initial_state(
        x=side * distance,
        y=rng.uniform(15.0, 35.0),
        vx=-side * rng.uniform(4.0, 10.0),  # already closing on the pad, horizontally dominant
        vy=rng.uniform(-2.0, 0.0),
        angle_deg=rng.uniform(-15.0, 15.0),
        terrain_seed=rng.randint(0, 2**31 - 1),
        gravity=gravity,
        fuel=fuel,
    )


def _landing_outcome(state: GameState) -> Status:
    speed = math.hypot(state.vx, state.vy)
    over_pad = state.pad_x_min <= state.x <= state.pad_x_max
    upright = abs(state.angle_deg) <= MAX_LANDING_ANGLE_DEG
    if over_pad and upright and speed <= MAX_LANDING_SPEED:
        return "landed"
    return "crashed"


def apply_action(state: GameState, action: Action, dt: float = DT) -> GameState:
    """Advance the simulation by one tick of `dt` seconds (default DT,
    0.5s -- what Jev's episodes always use, one real decision per tick).
    `dt` is only ever overridden by human-play mode (jevgame/server.py's
    /api/manual/step), which polls the keyboard and steps physics at a much
    finer resolution (~60ms) than Jev's 0.5s decision cadence so a brief key
    tap actually registers -- everything below scales by dt/DT so a human
    session covers the same simulated ground at the same rates (fuel burn,
    rotation speed, thrust accel) per second of SIMULATED time regardless of
    how finely it's stepped; only the granularity of control changes.
    Deterministic, pure."""
    if state.status != "flying":
        return state  # episode already over; caller shouldn't keep ticking

    angle = state.angle_deg
    angle_vel = state.angle_vel_deg_s
    fuel = state.fuel
    vx, vy = state.vx, state.vy
    dt_ratio = dt / DT

    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action!r}")

    # Attitude (RCS) and main-engine (throttle) commands are independent
    # real spacecraft controls that fire concurrently -- an Apollo-style
    # lander doesn't have to choose between reorienting and burning, it does
    # both at once. Thrust direction uses the angle at the START of this
    # tick (the RCS rotation for this tick hasn't taken effect yet), matching
    # how a discrete-tick approximation of concurrent continuous control
    # would resolve order-independence.
    #
    # RCS builds ANGULAR VELOCITY, it doesn't set angle directly -- firing
    # rotate_left adds negative angular accel, rotate_right adds positive;
    # once you let go, that spin keeps going (vacuum, no damping) until the
    # opposite thruster cancels it back out, same as vx/vy. angle_deg then
    # integrates from angle_vel_deg_s every tick, exactly like x/y from vx/vy.
    rotate_dir, rotate_frac = _rotate_info(action)
    if rotate_dir == "left":
        angle_vel -= ROTATE_ACCEL_DEG_S2 * rotate_frac * dt
    elif rotate_dir == "right":
        angle_vel += ROTATE_ACCEL_DEG_S2 * rotate_frac * dt
    angle += angle_vel * dt

    throttle_frac = _throttle_frac(action)
    did_thrust = throttle_frac > 0 and fuel > 0
    if did_thrust:
        fuel = max(0.0, fuel - FUEL_BURN_PER_TICK * throttle_frac * dt_ratio)
        rad = math.radians(state.angle_deg)
        # Nose-relative thrust: straight up when angle==0.
        vx += math.sin(rad) * THRUST_ACCEL * throttle_frac * dt
        vy += math.cos(rad) * THRUST_ACCEL * throttle_frac * dt

    vy -= state.gravity * dt  # gravity always applies
    x = state.x + vx * dt
    y = state.y + vy * dt
    tick = state.tick + 1

    new_state = replace(
        state, x=x, y=y, vx=vx, vy=vy, angle_deg=angle, angle_vel_deg_s=angle_vel,
        fuel=fuel, tick=tick, was_thrusting=did_thrust,
    )

    ground_height = terrain_height_at(x, state.terrain_seed, state.pad_x_min, state.pad_x_max)
    if y <= ground_height:
        new_state = replace(new_state, y=ground_height)
        outcome = _landing_outcome(new_state)
        if outcome == "crashed" and fuel <= 0:
            outcome = "out_of_fuel_crashed"
        return replace(new_state, status=outcome)

    if tick >= MAX_TICKS:
        return replace(new_state, status="timeout")

    return new_state
