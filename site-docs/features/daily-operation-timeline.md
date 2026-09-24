# Understand today's battery plan

Use the daily operation timeline to see how Omnibattery made the most of solar and protected your installation today, and what it expects to do next. It combines measured household and solar energy with battery actions and the active charging plan; it is a diagnostic view and does not control the battery.

## Do I need it?

**Use it if** you want to understand why the battery charged, discharged, waited for solar, or left a grid-charging period unused.

**You do not need it** to operate Omnibattery. The controller continues working when the panel is closed, and opening the card does not trigger a control decision.

## Before you start

- Install the Omnibattery sidebar panel and allow the integration to collect current-day data.
- Configure [household consumption learning](consumption-estimate.md) and a solar source for the most complete curves.
- Configure Dynamic Pricing or Time Slot predictive charging if you want future grid-charge decisions on the timeline. Real-Time Price records activations as they happen and intentionally has no future calendar.

## How to enable it

The timeline is automatic; there is no switch to enable.

1. Open the Omnibattery sidebar panel.
2. Select **Overview** and find **Daily Operation Timeline**.
3. Move across a cell to open its details. On touch devices, tap the interval.
4. Use the right navigation arrow or horizontal scrolling to view the next local morning.

## What you will see

The first local day is divided into quarter-hour cells. Solid curves are measured and dashed curves are forecast. Solar and consumption use the energy axis; total battery state of charge (SOC) uses the percentage axis.

| Color or marker | Meaning | How to read it |
|---|---|---|
| Green | Solar charged the battery, or the future plan allocates solar to it | Confirmed past flow or planned future flow |
| Purple | Grid-charge decision | A planned or observed grid charge |
| Blue | Battery discharge | Energy supplied from the battery |
| Grey | Grid charging was considered but not needed | An explicit no-charge decision, not missing data |
| Soft yellow | Solar surplus could charge the battery | An opportunity; it remains yellow until charging is observed |
| Clock marker | Solar Charge Delay is active | The tooltip includes the estimated release time |

Closed intervals show observed data. The open interval combines energy measured so far with a projected remainder, while future cells are informational projections from the active consumption and solar profiles, battery state, and selected plan. The tooltip labels observed and projected values and shows energy that entered or left the battery.

On narrow screens, scroll horizontally by hour. The initial view stays on the current local day; use the right arrow to reveal the additional `12 hours` through the next local noon. Keyboard arrows, touch, and mouse expose the same interval details.

The timeline depends on the [consumption estimate](consumption-estimate.md), [Solar Charge Delay](solar-charge-delay.md), [Dynamic Pricing](../configuration/predictive-charging/dynamic-pricing.md), [Time Slot](../configuration/predictive-charging/time-slot.md), and [Real-Time Price](../configuration/predictive-charging/real-time-price.md).

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| Future cells are empty | A forecast is unavailable or stale, or Real-Time Price mode has no future schedule by design | Profile sources, plan status, and selected predictive mode |
| Past cells are empty after opening the panel | Recorder has no usable history or current-day telemetry is missing | Recorder, grid/solar entities, and integration availability |
| A yellow cell did not become green | Solar charging was projected but no energy entered the battery | Battery limits, SOC, and the interval tooltip |
| A cell shows more than one action | Flows occurred simultaneously | The diagonal pattern and accessible tooltip text |
| The current cell differs from the eventual result | Its unfinished portion is still projected | Recheck after the quarter closes |
| Missing data appears as a gap | Omnibattery preserves unavailable values | Source entity availability; missing values are not converted to zero |

??? "Advanced details"
    The card contains `96` fixed `15-minute` cells for the local day and up to `48` extension cells for the following `12 hours`, for a maximum visible horizon of `144` cells. Energy curves use `kWh/15 min`; SOC uses a `0–100%` axis.

    The grey no-charge decision is published internally as `grid_charge_not_needed`. A cell can contain up to `3` simultaneous actions. Diagonal patterns and accessible text preserve each action in light and dark themes. Actions are combined only when they overlap in time. If the battery changes direction within an interval, the action present for the longest time is shown. The current interval never labels a projected action for its remaining minutes as observed. `Charging to setpoint` is context rather than a separate action color. Hourly Net Balance adds its own legend and cause marker only while that feature is enabled.

    Observed SOC is retained in the daily diary and backfilled from Home Assistant Recorder when the panel opens. The tooltip names the learned solar shape or sinusoidal fallback and reports observed or projected charge/discharge energy for the interval.

    The diagnostic entity is `sensor.omnibattery_daily_operation_timeline`; its state is the local snapshot date. Attributes include `schema_version`, timezone, freshness, profile sources, `96`-value energy series, observed and projected total SOC, operation masks, grid decisions, and delay metadata. The extension is published separately as `extended_horizon` and `extended_projection`, bounded to `48` intervals and excluded from Recorder.

    Forecast simulation uses detached, immutable inputs and has no control side effects. Predictive diagnostics are owned and restored by the pricing lifecycle rather than by rendering the entity. Completed quarter-hours remain immutable; plan reevaluation can replace only the open interval and future intervals. Stored data is restored only for the same local date and temporal fingerprint. Corrupt data degrades to an empty timeline and never blocks battery control.

    Missing telemetry remains `null` rather than becoming zero. If a forecast is unavailable or stale, the observed past remains visible and only unsupported future values become unavailable.
