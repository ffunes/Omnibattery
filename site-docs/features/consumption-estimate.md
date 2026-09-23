# Learn your household consumption

Make the most of solar by letting Omnibattery learn how much energy your home uses and when it uses it. The estimate is automatic; it helps predictive charging and [Solar Charge Delay](solar-charge-delay.md) decide whether stored energy and expected solar can cover demand.

## Do I need it?

**Use it if** you use predictive charging or Solar Charge Delay and want those features to account for your household's normal routine.

**You do not need to manage it** when your recent consumption is representative. Intervene only for holidays, meter outages, or unusual days that should not influence future forecasts.

## Before you start

- Configure a working grid sensor and battery. Omnibattery derives household demand from the same measurements used by the energy-flow diagram.
- Keep Home Assistant Recorder enabled if you want missing recent days to be recovered after installation or a restart.
- [Excluded and additional devices](../configuration/excluded-devices.md) are optional. Their configured power adjustments are included in learning.

## How to enable it

This feature is automatic; there is no learning switch to enable.

1. Open the Omnibattery sidebar panel and check that **Home Consumption** follows your household load.
2. Let the integration collect representative daily use. **Expected Home Consumption Profile** shows when the learned profile is ready.
3. Turn on **Vacation Mode** on the Omnibattery System device while your normal routine is interrupted. Turn it off when the household returns to normal.
4. To remove a past unusual day, open **Developer tools → Actions**, run **Omnibattery: Exclude consumption days**, and choose the first and last local date to ignore.

Vacation Mode pauses only learning. Physical energy counters, the real-consumption chart, and battery control continue normally. Excluded periods are remembered, so Recorder backfill cannot add them again.

![Consumption learning attributes](../assets/screenshots/features/consumption-estimate-attributes.png){ width="700" style="display: block; margin: 0 auto;"}

## What you will see

- **Home Consumption** shows the live household power used for learning.
- **Daily Home Consumption** accumulates today's household energy and resets at local midnight.
- **Expected Home Consumption Profile** shows today's forecast and whether its source is the mature learned profile or a fallback estimate.
- **Current Consumption Profile Capture** shows how much of today's demand has been captured so far.
- **Vacation Mode** reports the fixed vacation baseline while learning is paused.

The integration learns both a daily total and a time-of-day pattern. A mature pattern lets Dynamic Pricing reserve energy before an early demand peak while leaving later demand through the next sunrise flexible by price. Solar Charge Delay uses the same remaining-demand forecast, so demand already observed today is not counted twice.

See the [daily operation timeline](daily-operation-timeline.md) to compare learned consumption with the battery actions planned for today.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The forecast still uses a fallback | The time-of-day profile has not collected enough recent coverage | **Expected Home Consumption Profile** and its `source`, `mature`, and `coverage_ratio` attributes |
| Home Consumption is implausible | A grid, solar, battery, or excluded-device measurement has the wrong sign or is unavailable | The energy-flow diagram and the source entities |
| An unusual day still affects learning | It was recorded before Vacation Mode was enabled | Run **Omnibattery: Exclude consumption days** for that local date |
| No recent history appears after a restart | Recorder has no usable Home Consumption history | Recorder retention, entity availability, and the integration diagnostics |
| Vacation Mode reports a generic baseline | It has not yet observed enough valid overnight data | Leave the mode active through representative nights and inspect its baseline source |

??? "Advanced details"
    ### What counts as household consumption

    Omnibattery derives live household power from measurements it already has:

    ```text
    home = grid + Σ(battery AC power) + solar
    ```

    Direct current (DC) solar connected through a maximum power point tracking (MPPT) input is already netted into the battery's alternating current (AC) power and is not added twice. During grid charging, negative battery AC power cancels the corresponding grid import. For example, importing `2.8 kW` while the battery charges at `2.5 kW` produces `0.3 kW` of household demand.

    An older installation may still have a saved `household_consumption_sensor`. Omnibattery reads it directly only when no solar production sensor is configured; with a solar sensor, the derived value is preferred. This field is no longer offered during setup. The derived value is also exposed as `sensor.marstek_venus_system_home_consumption`.

    Before accumulation, an excluded device with `included_in_consumption = true` is subtracted because it already appears in the grid/home reading. An additional device with `included_in_consumption = false` is added because that load is absent from the reading. See [excluded devices](../configuration/excluded-devices.md).

    ### Daily total and legacy estimate

    Every control sample contributes energy using the real elapsed time:

    ```text
    increment (kWh) = home_power (W) × elapsed_time (s) / 3,600,000
    ```

    Charging windows do not pause learning. At `23:55` local time, Omnibattery stores the full-day accumulator if it is at least `1.5 kWh`, then the accumulator resets at midnight. It keeps the latest `7` daily entries and averages them:

    ```text
    expected_consumption = Σ(daily_consumption) / number_of_days
    ```

    Missing startup entries use `5.0 kWh` until Recorder backfill or a real capture replaces them. Backfill integrates **Home Consumption** over each missing local day, including excluded/additional-device adjustments. Histories from older versions that covered only charging windows are discarded and rebuilt so partial-day and full-day totals are never mixed.

    Example:

    ```text
    Daily totals: 5.0, 5.1, 5.3, 4.8, 4.9, 6.3, 6.0 kWh
    Expected consumption = 37.4 / 7 = 5.34 kWh
    ```

    The running full-day value is also exposed as `household_consumption_full_day_kwh` on `binary_sensor.marstek_venus_system_predictive_charging_active` and is persisted across same-day restarts. Its attributes include the daily history and counts of real and fallback entries. In Dynamic Pricing mode, `energy_horizon_end` identifies the local sunrise boundary and `overnight_consumption_kwh` reports forecast demand between midnight and that boundary.

    **Grid at Min SOC** (`sensor.marstek_venus_system_daily_grid_at_min_soc_energy`) resets at local midnight and reports grid energy imported while every battery was at its minimum state of charge (SOC) during a discharge window. It is diagnostic only and is not added to the estimate because derived household consumption already includes that demand.

    ### Learned time-of-day profile

    Omnibattery retains up to `28` local days in `96` quarter-hour intervals. It integrates samples with a trapezoidal rule across interval boundaries, midnight, and daylight saving time changes. A sample gap longer than `5 minutes` breaks continuity; an interval needs at least `675 seconds` (`75%`) of coverage to be usable.

    The profile prefers the matching weekday, then the matching weekday/weekend type, then all usable days. Samples are weighted by age at `1.0`, `0.75`, `0.5`, and `0.25`. A requested range is mature only when it has at least `7` valid days; at least `2` matching samples for `75%` of its intervals; at least `80%` total coverage; and a newest sample no more than `7` days old.

    Until then, Omnibattery distributes the daily estimate over a temporary household-shaped curve: the lowest demand from `00:00–06:00`, a breakfast lift, greater daytime demand, and the strongest dinner peak. The curve is normalized to preserve the exact daily total, including on daylight saving time transition days. Forecasts adjust today's remaining leg after the first `3 hours`, reach full adjustment at `12:00`, and limit that correction to `30%` of today's remaining forecast so a single spike cannot erase later expected demand. A post-midnight leg uses the next day's profile or the historical hourly rate when the profile is unavailable.

    During Vacation Mode, affected calendar days are omitted from daily history and only affected quarter-hours are omitted from the profile. Forecasts use the median load from the last `3` valid `01:00–05:00` nights, where a night needs `3 hours` of coverage. Before that is available, the fallback order is the learned night profile, daily history divided across the day, then the default estimate. Toggling the switch breaks sample continuity so an interval is never attributed across the mode change.

    **Exclude consumption days** accepts dates from the last `35` days, removes them from the daily history, and masks them from the profile without altering physical counters or Recorder. The same action can be called as:

    ```yaml
    action: omnibattery.exclude_consumption_days
    data:
      start_date: "2026-09-07"
      end_date: "2026-09-07"  # optional; defaults to start_date
    ```

    Deleting a day manually from `.storage` does not exclude it: backfill treats it as missing and restores it.

    An immature profile falls back to the legacy daily estimate or a current-rate estimate, depending on the requesting feature. Recorder backfill runs in the background with one query per configured source. Raw profile data is isolated in `omnibattery.<entry_id>.consumption_profile`. Changing a source or load adjustment breaks sample continuity and fills missing days again. A Home Assistant timezone change re-bins stored days; the two edge dates keep their partial hours and are fetched again. Only a stored timezone that no longer exists forces a fresh profile.

    `sensor.omnibattery_expected_home_consumption_profile` exposes the interval and hourly forecast, source, maturity, coverage, and fallback metadata. `sensor.omnibattery_consumption_profile_capture` exposes today's raw capture through `hourly_capture_kwh`, `interval_capture_kwh`, and `interval_coverage_s`; it resets on the next local day. The integration diagnostics include a bounded day-level learning summary.

    This household profile is separate from the solar temporal profile. Household learning estimates absolute demand by local time; the solar profile learns a normalized daylight shape from direct solar power. Neither changes the forecast energy budget.
