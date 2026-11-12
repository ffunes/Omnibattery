# Predictive charging scenarios

One JSON file per reported night. `tests/test_predictive_scenarios.py` runs every
file in this folder as one test, so a bug report becomes a regression test by
adding a file.

Name a file `<issue>-<slug>.json` (`000-` for cases that are not reports).

## Levels

| `level` | What runs | Use it for |
|---|---|---|
| `allocator` | The file is adapted into a `ChronologicalEvaluationRequest` and passed to `evaluate_chronological_request`. | Bugs in the allocator itself. No peak shaving, guaranteed floor, safety margin or undated solar; the horizon is the next sunrise. |
| `engine` | The real `PricingManager._evaluate_dynamic_pricing` (a `REMAINING` evaluation) on a stub controller, with the controller's own energy balance. Only the price feed, the forecast sensor, the price refresh and the notification are replaced. | Everything else: horizon, fleet power, arbitrage ceilings, peak shaving. Prefer this level. |

An engine scenario fails, even when marked `xfail`, if the planner falls back
(`chronological_plan_reason` missing or starting with `fallback`) or if the
stub did not reach the planner with the file's settings.

## Format

All times are local wall clock without offset (`2026-11-10T19:52:00`).

| Key | Meaning |
|---|---|
| `source` | `issue` (number or `null`), `kind` (`issue`, `discussion`, `synthetic`), `reporter`, `date`, `url`. |
| `note` | Free text, a string or a list of paragraphs. Say what was reported, list **every assumed number**, and say what the planner does today. |
| `level` | `allocator` or `engine`. |
| `xfail` | Optional. The reason a known, unfixed bug makes this file fail. Applied as `xfail(strict=True)`: the test fails loudly once the bug is fixed, and you then delete the key. |
| `now` | The evaluation instant. |
| `time_zone` | IANA name, the Home Assistant time zone. |
| `sun` | `sunrise` and `solar_noon` as `HH:MM`. Sunrise ends the control horizon. |
| `prices` | `slot_minutes` (the feed's slot length) and `blocks` of `start`, `end`, `price`. A block is cut into slots of `slot_minutes`, so an hourly price served in quarter-hours is one block with `slot_minutes: 15`. |
| `consumption` | The household's daily shape: `profile_kw` (steps of `from` `HH:MM` and `kw`, starting at `00:00`) or `intervals_kwh` (96 quarter-hour values). Optional `daily_total_kwh` rescales the shape. |
| `solar` | `remaining_kwh` (what the remaining-today sensor reads) and `periods` (dated `start`, `end`, `kwh`; empty when the provider gives none). |
| `fleet` | Per battery: `capacity_kwh`, `soc`, `min_soc`, `max_soc`, `max_charge_w`, `max_discharge_w`. |
| `settings` | All eight keys are required, so nothing rests on a hidden default: `max_price_threshold`, `min_arbitrage_margin` (`null` = unset), `round_trip_efficiency`, `discharge_reserve` (bool), `peak_shaving` (`null` or `limit_w` + `soc_threshold`), `contracted_power_w`, `guaranteed_floor_soc` (`null` = off), `safety_margin_kwh`. |
| `expected` | At least one of the assertions below. |

Unknown keys are rejected.

## Assertions (`expected`)

Energies are battery-side kWh of the published schedule (the per-slot targets).

| Key | Passes when |
|---|---|
| `reason` | `chronological_plan_reason` equals it. |
| `total_kwh` | The booked total is within `min_kwh` .. `max_kwh` (either may be left out). |
| `windows` | For each `start`, `end`, `min_kwh`, `max_kwh`: the energy of the slots starting in the window is in range. A window of one slot asserts that slot; `max_kwh: 0` asserts nothing is booked there. |
| `no_slot_before` / `no_slot_after` | No slot is booked that starts before / ends after the instant. |
| `horizon_reaches` | The plan's horizon ends at or after the instant. |
| `no_charge_above_use_value` | No booked slot costs more than `use price x round_trip_efficiency`. |

The **use price** of a slot is the average import price of the quarter-hours
that drain its energy when the plan is replayed first in, first out: the
battery covers the house (load less solar) with the oldest stored energy, a
slot charges from its start at the planned power until its energy is stored,
and the grid carries the house meanwhile. Energy still stored when the horizon
ends is not valued. This check is strict per slot. A plan that deliberately
stores the drain of cheap hours to reach a peak (#834) fails it, so only ask
for it where no such crossing is expected.

## Writing a scenario from a report

1. Copy the closest file and replace the inputs with the reported numbers.
2. Where the report lacks a number, pick a plausible one and list it under
   ASSUMED in `note`. Check the replay against what the report does give
   (deficit, depletion time, number of slots, horizon end).
3. Write `expected` as the outcome the user should get, as ranges. A point value
   taken from someone else's replay will not survive your assumed numbers.
4. Run it. If it fails because of the bug, add `xfail` with the cause.

Keep the dates in the past: a clock read that escapes the pinned `now` then
finds every price slot expired and the scenario fails.
