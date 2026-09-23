# Jev landing research — 2026-09-23

Variant D landed **100/100 held-out lunar flights** using real Jev API calls,
the original game physics, and both confidence thresholds at **0.55**.
The pooled 95% Wilson interval is **96.3–100%** for this tested scenario mix.
This is guidance-assisted control: code computes navigation targets and actuator
tracking errors; Jev selects from those forecasts. It is not a prompt-only gain.

## What Jev is, and what other projects actually send

TypeSafe describes Jev as a model for bounded, typed decisions. Choice returns
an option, its distribution, and a confidence statistic. Multiple questions in
one request are evaluated independently against shared state. This supports
batching independent controls without asking one question to infer the other's
answer. [TypeSafe introduction](https://docs.typesafe.ai/introduction)

The documented API accepts structured objects in both instructions and criteria.
Our live responses identified the model as `jev-1.13.0` and included actual
`usage.input_tokens`, contrary to the old project's comment that usage was absent.
[API contract](https://docs.typesafe.ai/api)

TypeSafe explicitly documents weaknesses in numerical precision, indirection,
irrelevant state, and conflicting instructions. Those closely match problems in
the original game prompts. Its recommendation is to compute arithmetic in code
and make the remaining question narrow and explicit.
[Jev 1.13 limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13)

I inspected these implementations and their payload builders, rather than relying
on directory descriptions. They are working public examples; I did **not**
independently validate their authors' gameplay success claims.

| Project and exact payload source | What it supplies to Jev | Useful pattern here |
| --- | --- | --- |
| [TypeSafe Mario](https://github.com/fhshaik/typesafe-mario/blob/main/src/typesafe_mario/policy.py) | Object-centric motion, terrain, hazard and reaction-time data; controller-macro Choice plus independent jump and danger questions | Calculate timing and projected hazards before calling Jev; keep a bounded action choice |
| [TypeSafe Snake](https://github.com/sorrycc/typesafe-snake/blob/master/src/jev/prompt.ts) | Board state plus per-move criteria containing computed food distance, reachable area and escape facts | Put each candidate's concrete consequences directly beside that candidate |
| [Jev Autopilot](https://github.com/arielweinberger/jev-autopilot/blob/main/server/pilot.ts) | Telemetry translated into situation descriptions; separate throttle, yaw, pitch and roll questions | Isolate control dimensions and expose compact motion facts |
| Local Fibril `src/fibril/jev_dedup.py` | Bounded candidate work orders, a yes/no Choice, explicit uncertainty fallback | Strict choice validation, confidence gating and raw-response audit |

The drone example includes self-leveling/hover behavior in its control semantics.
Its inputs are therefore not directly transferable to this game's undamped angular
momentum. Mario and Snake are architectural examples, not evidence that Jev can
solve this lander's navigation unaided.

Confidence describes concentration of a question's answer distribution; it is
not the probability that an entire flight will land. A threshold appropriate for
one question is not automatically appropriate for another.
[TypeSafe confidence documentation](https://docs.typesafe.ai/confidence)

## Problems found in the existing controller

- Variant A's near-ground rule reverses the rotation signs relative to the
  simulator. Other rules in that same prompt use the correct convention.
- Counter-spin, overshoot, near-ground, and strength rules compete. One rule
  altitude-scales an overshoot correction; a later exception says it is always
  full strength. Magnitude instructions also confuse a cap with a minimum.
- At lunar gravity, 25% thrust accelerates upward at 1.5 m/s² before subtracting
  gravity of 1.62 m/s². It still accelerates downward. Asking for a gentle 25%
  burn does not by itself slow descent.
- The old stopping-distance signal assumes upright full thrust even when the
  selected action is partial thrust or the craft is tilted.
- One 100% RCS tick adds 30°/s of angular velocity. Rotation must account for
  that persistent momentum; position-only rules cause overshoot and reversal.
- `near_ground` activates below 25 m, while random starts are only 15–35 m high.
  Suppressing lateral navigation there can prevent reaching the pad at all.
- The default mixed A/B/C every tick. Per-question confidence statistics from
  that mixture cannot establish a landing rate for any individual variant.

The legacy A/B/C controllers and triple loop were removed after evaluation.
Historical baseline payloads and results remain in the audit databases; current
code exposes the guided controller only.

## What D changes

Navigation targets are isolated in `jevgame/guidance.py`, separate from simulation
physics. A position-based approach speed feeds a lateral acceleration/tilt target.
A desired angular velocity accounts for angle error; actual existing spin is
included in every actuator forecast. The tilt target progressively returns upright
near the ground. A height target preserves clearance during traversal and lateral
braking, then permits a slow touchdown. A downward bias prevents hovering forever
just above the pad.

For every existing RCS and throttle option, code computes next-tick angular or
vertical speed and absolute error from the corresponding target. Jev receives
all **nine attitude choices and five throttle choices**. There is no hidden
candidate filtering, scripted action substitution, or post-answer safety override
in D. The two questions share one API request, but keep separate confidence gates.
Rejected or failed answers use `hold_attitude`/`no_op`, and each question retains
the full request, raw response, threshold, outcome, and resulting combined action.

For example, at an upright, centered state with `y=3`, `vy=-3`, and no lateral
motion, the throttle target is −2.3 m/s. These are the actual forecast values
generated by D (instructions abbreviated; attitude question and ancillary targets omitted):

```json
{
  "model": "jev-latest",
  "state": {"flight_targets": {"target_vertical_speed_m_s": -2.3}},
  "questions": {
    "throttle": {
      "type": "choice",
      "instructions": {
        "goal": "Select the engine action whose forecast has the smallest absolute tracking error.",
        "rules": ["If errors are equal, prefer the smaller actuator percentage."]
      },
      "criteria": {
        "no_op": {"predicted_vertical_speed_m_s": -3.81, "absolute_tracking_error": 1.51, "actuator_percentage": 0},
        "thrust_25": {"predicted_vertical_speed_m_s": -3.06, "absolute_tracking_error": 0.76, "actuator_percentage": 25},
        "thrust_50": {"predicted_vertical_speed_m_s": -2.31, "absolute_tracking_error": 0.01, "actuator_percentage": 50},
        "thrust_75": {"predicted_vertical_speed_m_s": -1.56, "absolute_tracking_error": 0.74, "actuator_percentage": 75},
        "thrust_100": {"predicted_vertical_speed_m_s": -0.81, "absolute_tracking_error": 1.49, "actuator_percentage": 100}
      }
    }
  }
}
```

The UI now uses forecast guidance exclusively and displays the actual changing
payload. The experimental variant selector and triple-loop controls were removed
after evaluation; confidence sliders and manual play remain.

## Experiments and results

First, I verified the existing 13 physics tests. I used offline deterministic
selectors to debug the instruments, including rejected versions that hovered or
lost lateral control. An initial grid tested 16 guidance configurations on seeds
0–99; four landed all 100 offline flights. Those are development results, not Jev
measurements. The chosen guidance uses position gain 0.12, velocity gain 0.8,
angular gain 1.0 and lateral-speed clearance multiplier 2.0.

The first live D check landed all eight development seeds. Guidance and prompt
were then frozen before evaluating disjoint holdout seeds. All live tests below
used 100 starting fuel, 0.5-second simulation ticks, lunar gravity 1.62 m/s²,
the existing randomized tilt/drift/terrain, and 0.55 confidence thresholds.

| Live experiment | Seeds | Starting distance | Landed | Audit/results directory |
| --- | --- | --- | --- | --- |
| A baseline | 0–7 | 25–90 m | 0/8 | `runs/astra-baseline-ab/` |
| B baseline | 0–7 | 25–90 m | 0/8 | `runs/astra-baseline-ab/` |
| C baseline | 0–7 | 25–90 m | 0/8 | `runs/astra-baseline/` |
| D development check | 0–7 | 25–90 m | 8/8 | `runs/astra-d1/` |
| D standard holdout | 1000–1059 | 25–90 m | **60/60** | `runs/astra-holdout-standard/` |
| D long UI range | 2000–2019 | 75–120 m | **20/20** | `runs/astra-holdout-long/` |
| D short UI range | 3000–3019 | 20–40 m | **20/20** | `runs/astra-holdout-short/` |

These current baselines do not reproduce the previously reported ~66%; they are
small, pinned-variant tests of the current code, not a reconstruction of every
historical slider/prompt combination. A and B failed the speed criterion in all
eight flights. C showed severe rotation/trajectory instability.

Across the 100 D holdouts there were 3,482 flight ticks and 6,964 question results:
6,957 applied answers, six failed question results from three timed-out batched
requests, and one low-confidence rejection. All episodes, including those with
failures, stayed in the denominator. No D safety overrides were used. Mean returned
confidence was approximately 0.992. Actual reported input usage totaled 4,292,408
tokens; at the project's configured $0.042/M rate that is an estimated $0.18 for
these holdouts, excluding development/baseline calls.

On the standard holdout, 3,854 of 3,856 returned choices selected a minimum-error
forecast. The two nonminimum choices did not prevent landing. Minimum remaining
fuel was 70.5, and mean touchdown speed was about 1.33 m/s. Keeping nine attitude
choices therefore did not inherently cause low confidence; the old ambiguity and
conflicting rules were more consequential in these experiments.

## Interpretation and limits

The target is met for the **combined guidance + Jev controller** on the tested
distribution. The deterministic minimum-error selector also lands all 100 of the
same held-out starts without Jev. D therefore demonstrates that Jev can reliably
consume prepared flight-control choices, **not** that it adds value over that
deterministic selector or independently solves navigation. The numerical control
law does most of the difficult work. This is a useful boundary result for the
original research question, and should not be described as a prompt-only fix.

Neither the observed 100% nor its interval guarantees future flights. The sample
covers the stated random lunar scenarios, not arbitrary initial spin, fuel loss,
other gravity settings, or adversarial states. `jev-latest` may change; the raw
responses record `jev-1.13.0`. All holdouts landed, so these data cannot establish
whether confidence distinguishes successful flights from failed flights. Thresholds
were kept at 0.55 rather than optimized on the holdout.

## Reproduction and verification

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m jevgame.cli benchmark --episodes 60 \
  --seed-start 1000 --output runs/recheck-standard
.venv/bin/python -m jevgame.cli benchmark --episodes 20 \
  --seed-start 2000 --min-distance 75 --max-distance 120 --output runs/recheck-long
.venv/bin/python -m jevgame.cli benchmark --episodes 20 \
  --seed-start 3000 --min-distance 20 --max-distance 40 --output runs/recheck-short
```

Each directory includes a manifest with exact starts/source hashes/settings,
per-episode SQLite audit databases, an append-only episode ledger, and JSON/Markdown
summaries with Wilson intervals. Existing directories are refused, not overwritten.
Code changes after the first holdout were integration/documentation changes; the
guidance and D decision criteria stayed fixed throughout the three holdouts.

Verification: **25 tests passed**, including actuator predictions against actual
physics, failure/invalid-answer handling, direct application of Jev's answer,
per-question isolation, benchmark pairing and usage accounting. Inline JavaScript
syntax and whitespace checks passed. The actual UI SSE endpoint completed a
separate seeded D flight with two audited decisions and the live payload on all
40 ticks. Visual browser verification was unavailable: the Browser runtime
reported no connected browsers.
