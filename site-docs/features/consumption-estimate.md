# Daily and hourly consumption estimate

Predictive charging needs to know how much energy your home consumes to decide whether grid charging is needed. The integration learns a **15-minute consumption profile** from up to 28 complete local days. Until that profile is mature, the existing 7-day daily estimate remains the safe fallback.

## Vacation mode

Turn on the **Vacation Mode** switch on the Omnibattery System device when the
usual household pattern is not representative. Physical energy counters and
the real-consumption chart continue recording, and battery control continues
normally. Only consumption learning pauses: affected calendar days are omitted
from the legacy daily history and only affected quarter-hours are omitted from
the 28-day profile. The periods are stored, so a later Recorder backfill cannot
reintroduce them.

While enabled, all consumption forecasts use a constant vacation baseline. It
is the median load from the last three valid 01:00–05:00 nights (a night needs
at least three hours of coverage). Before the first valid night, the learned
night profile is used, then the daily history divided by 24, and finally the
default estimate. Toggling the switch breaks learning-integrator continuity,
so a sample interval is never attributed across a mode change.

Dynamic Pricing uses this as a chronological curve, not only as a daily total. It can therefore reserve grid energy before an early projected depletion while leaving the rest of the daily deficit flexible by price. The mature profile and temporary curve are normalized to the same aggregate kWh used by the predictive decision.

While the learned profile is immature, that daily total is not distributed
completely flat: a temporary household-shaped curve is used. Overnight
(00:00–06:00) receives the minimum weight, breakfast has a small lift, the
middle of the day receives more weight, and dinner is the main peak. The curve
is normalized so the total remains exactly the estimated daily consumption,
including on daylight-saving transition days, and disappears as soon as a
learned profile with real data is available.

For Dynamic Pricing intraday re-evaluations that forecast from now until
midnight, the curve is also adjusted gradually using today's accumulated real
consumption. The adjustment starts after the first three hours, reaches full
strength at noon and is capped at 30% of the forecast remainder so a one-off
spike cannot distort the rest of the day.

---

## What the estimate measures

The estimate is the **total home consumption over the full local day**, including predictive grid-charging windows. It is averaged over the last 7 calendar days.

### Home consumption source

The per-cycle home power is **derived** from the values the integration already has:

```
home = grid + Σ(battery AC power) + solar
```

This is the same value shown by the energy-flow diagram and the **`sensor.marstek_venus_system_home_consumption`** (Home Consumption, W) sensor. DC-coupled PV (MPPT) does not appear here — it is already netted into each battery's AC power at the inverter.

When the battery charges from the grid, its AC power is negative. That term cancels the corresponding grid import, so battery-charging energy is not mistaken for household consumption. For example, 2.8 kW imported while the battery charges at 2.5 kW produces 0.3 kW of home demand.

!!! note "Legacy household sensor"
    A `household_consumption_sensor` saved on an older install is read directly **instead** of deriving, but **only when no solar production sensor is configured** — with a solar sensor the derived value is exact and preferred. The field is no longer offered in setup.

### Excluded / additional devices

If you have configured [excluded or additional devices](excluded-devices.md), the home power is corrected before accumulation:

- **Excluded** (`included_in_consumption = true`): the device is already in the home/grid reading but the battery should not cover it → its power is **subtracted**.
- **Additional** (`included_in_consumption = false`): the device is not visible to the home reading but the battery should cover it → its power is **added**.

---

## Real-time accumulation

On every control cycle (event-driven, at the grid sensor's cadence), the adjusted home power is integrated throughout the full local day. Predictive charging windows only schedule when the battery may charge from the grid; they never pause household-consumption learning.

```
increment (kWh) = home_power (W) × Δt (s) / 3,600,000
```

`Δt` is the real elapsed time since the previous sample, so it adapts to the variable cadence. The running daily value is exposed as `household_consumption_full_day_kwh` on `binary_sensor.marstek_venus_system_predictive_charging_active`, and is persisted so it survives restarts within the same day.

---

## Daily capture at 23:55

Every day at **23:55 (local time)** the integration snapshots the accumulator into the 7-day history before it resets at midnight. The value is only stored if it is ≥ 1.5 kWh (to discard days without meaningful data).

---

## 7-day history

The integration maintains a rolling history of the last **7 entries** in `(date, kWh)` format, persisted to disk so it survives Home Assistant restarts.

### Fallback value

While fewer than 7 real days have accumulated (e.g. just after installing the integration), missing entries are filled with the fallback value **`DEFAULT_BASE_CONSUMPTION_KWH = 5.0 kWh`**. This acts only as a placeholder and is replaced as soon as real data is available.

### Backfill from recorder history

At startup, the integration recovers missing days by querying the **Home Assistant recorder** for the `sensor.marstek_venus_system_home_consumption` sensor (which already resolves to the derived value, or the legacy household sensor when applicable). For each missing day it integrates that sensor's history over the full local day, applies the excluded/additional-device adjustments, and stores the result exactly as the 23:55 capture would. This builds the history with real data even after an HA restart or a fresh installation. Histories created by older windowed versions are discarded once and rebuilt from Recorder so partial-day and full-day totals are never mixed.

---

## 7-day rolling average

The consumption estimate used by predictive charging is the **arithmetic mean** of all values in the history:

```
expected_consumption = Σ(consumption_i) / n days
```

where `n` may be less than 7 if not enough real days have accumulated yet (fallback values also count in the average until replaced).

---

## Full example

```
Monday:    full-day home consumption = 5.0 kWh
Tuesday:   full-day home consumption = 5.1 kWh
Wednesday: full-day home consumption = 5.3 kWh
Thursday:  full-day home consumption = 4.8 kWh
Friday:    full-day home consumption = 4.9 kWh
Saturday:  full-day home consumption = 6.3 kWh
Sunday:    full-day home consumption = 6.0 kWh

Expected consumption = (5.0 + 5.1 + 5.3 + 4.8 + 4.9 + 6.3 + 6.0) / 7 = 5.34 kWh
```

---

## Diagnostic sensor

| Sensor | Description | Reset |
|---|---|---|
| `sensor.marstek_venus_system_daily_grid_at_min_soc_energy` | Grid energy imported while all batteries were at min SOC during a discharge window — household demand the battery could not cover | Midnight (local time) |

This **Grid at Min SOC** sensor is informational: it shows demand the battery missed because it was empty. It is no longer summed into the consumption estimate (the derived home consumption already captures total house load, including the part served from the grid).

The `binary_sensor.marstek_venus_system_predictive_charging_active` sensor exposes the 7-day consumption history and the count of real vs. fallback entries in its attributes, useful to verify the learning status.

![Consumption history attributes in HA](../assets/screenshots/features/consumption-estimate-attributes.png){ width="700"  style="display: block; margin: 0 auto;"}

## 28-day quarter-hour profile

The integration also captures adjusted household demand continuously, 24 hours
per day, in **96 local quarter-hour intervals**. Each sample is integrated with a
trapezoidal rule and split across midnight, quarter-hour boundaries and daylight
saving transitions. A gap longer than five minutes breaks continuity; an interval
is usable only after at least 675 seconds (75%) of observed coverage. Charging
windows are not applied while learning or forecasting household demand: they
schedule battery charging but do not remove the home's load from the day.

The profile uses a hierarchy of matching weekday, weekday/weekend type and global
samples. Recent days are weighted `1.0`, `0.75`, `0.5` and `0.25`. It is considered
mature only when it has at least seven valid days, at least two samples for 75%
of the requested intervals, at least 80% coverage of the requested range and a
sample no older than seven days. An immature profile automatically falls back to
the legacy daily average or the current-rate estimate, depending on the caller.

For the temporary daily curve, remaining consumption is gradually reconciled
with the part of the daily budget not yet consumed. The correction reaches full
strength at noon and is capped at 30% of the remaining curve so a one-off spike
cannot erase household demand that can still be expected later in the day.

Recorder backfill runs in the background after startup and uses one query per
configured source. Raw profile data is isolated in
`omnibattery.<entry_id>.consumption_profile`. Nothing you configure erases what
it has learned: changing the source or a load adjustment only breaks sample
continuity and triggers a backfill of the days still missing, and changing the
Home Assistant timezone re-bins the stored days by the offset between the two
zones instead of dropping them (the two edge days keep only part of their hours
and are re-fetched by backfill). Only a stored timezone that no longer exists
forces a fresh learn.

The diagnostic sensor
`sensor.omnibattery_expected_home_consumption_profile` exposes the current
forecast, 96-interval/hourly values, source, maturity, coverage and fallback
metadata. The integration diagnostics endpoint contains the bounded day-level
learning summary. Predictive charging, Solar Charge Delay and Dynamic Pricing
use the profile only when the maturity contract is satisfied.

To check how the current day is being captured, the diagnostic sensor
`sensor.omnibattery_consumption_profile_capture` reports the kWh captured so far.
Its `hourly_capture_kwh`, `interval_capture_kwh` and `interval_coverage_s`
attributes locate that energy across the 24 hours and 96 quarter-hour bins. This
sensor exposes the raw current-day capture rather than the forecast and resets at
the next local day.

This household profile is separate from the solar temporal profile. Consumption
learns absolute home demand over local clock intervals and can provide a load
fallback; the solar profile learns only a normalized daylight shape from direct
PV power. Neither profile changes the forecast kWh budget.
