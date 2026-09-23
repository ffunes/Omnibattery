# Wait for solar before charging

Make the most of solar by delaying battery charging while today's forecast can still cover household demand and the energy the battery needs. This avoids filling the battery early from the grid or from low-value morning solar when enough solar is expected later.

## Do I need it?

**Use it if** your battery often starts charging in the morning even though later solar could complete the charge. It can also let solar complete a weekly full charge before the grid is used.

**You do not need it if** you have no usable solar forecast, want charging to begin as soon as energy is available, or another schedule must always control charging time.

## Before you start

- Configure a solar forecast sensor in [Sensors and electrical limits](../configuration/main-sensor.md). A **Remaining Today** sensor gives the clearest input; saved whole-day sensors are converted automatically for older entries.
- Let the [household consumption estimate](consumption-estimate.md) learn your demand. The delay uses the mature time-of-day profile when available and falls back to the daily estimate while it learns.
- Decide whether a deeply discharged battery should first reach a guaranteed state of charge (SOC) before it waits for solar.

## How to enable it

1. Open the Omnibattery sidebar panel and select **Control**.
2. Turn on **Charge Delay**.
3. Set **Charge Delay Margin** to leave enough time to finish charging before expected solar production ends.
4. Optional: turn on **Enable minimum SOC before delay** and set **Charge Delay SOC Setpoint** so the battery charges to that level before waiting.
5. Check the selected **Solar forecast sensor** and save the configuration.

![Configure Solar Charge Delay](../assets/screenshots/configuration/advanced-solar-charge-delay-config.png){ width="650" style="display: block; margin: 0 auto;"}

## What you will see

**Charge Delay** explains the current decision:

| State | Meaning |
|---|---|
| `Disabled` | The feature is off |
| `Charging to setpoint` | Charging is allowed until every controlled battery reaches the optional SOC setpoint |
| `Waiting for forecast` | A configured forecast is temporarily unavailable; the delay remains in place during the grace period |
| `Charging allowed` | The delay has released for the rest of the day |
| Delayed / waiting for solar | The forecast and remaining household demand indicate that solar can complete the charge later |

The sensor also shows the expected release time. During the setpoint phase, `estimated_setpoint_time` and `projected_unlock_time` are projections; after the delay becomes active, `estimated_unlock_time` is the controller's current estimate.

Once a normal forecast decision releases the delay, charging stays allowed for the rest of that local day. To re-evaluate from scratch, turn **Charge Delay** off and on again.

![Solar Charge Delay status and attributes](../assets/screenshots/features/solar-charge-delay-attributes.png){ width="650" style="display: block; margin: 0 auto;"}

See the [daily operation timeline](daily-operation-timeline.md) for the delay marker alongside expected solar, consumption, and battery actions.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| Charging starts immediately | The forecast cannot cover remaining demand and battery energy, or the finish-time margin has been reached | **Charge Delay**, forecast energy, expected unlock time, and margin |
| The battery charges before waiting | The optional SOC setpoint is enabled | `Charging to setpoint` and **Charge Delay SOC Setpoint** |
| The state says `Waiting for forecast` | The forecast sensor is refreshing or has not loaded after restart | The forecast entity; wait for it to recover before changing settings |
| The delay uses a daily fallback | The household profile is not mature | [Expected Home Consumption Profile](consumption-estimate.md) and its maturity attributes |
| The delay never starts | No forecast sensor is configured, the feature is disabled, or the weekly full-charge policy overrides it | **Charge Delay**, the selected forecast sensor, and weekly full-charge settings |
| A previously released delay should apply again today | A genuine release is latched for the day | Toggle **Charge Delay** off and on to force a fresh decision |

??? "Advanced details"
    Omnibattery models the current day's solar production as a sinusoidal curve and compares forecast solar remaining until production ends with remaining household demand and battery energy:

    ```text
    net_solar_for_battery = remaining_solar - remaining_consumption

    if net_solar_for_battery can cover energy_to_charge:
        keep waiting
    else:
        allow charging
    ```

    Omnibattery reads the solar forecast live without a nightly capture or separate forecast store. A saved **Remaining Today** sensor is used directly; untouched legacy whole-day entries are converted into a remaining-production estimate.

    The live balance is recalculated when forecast energy changes by more than `0.05 kWh`, or when its source or conversion changes. A worsening forecast can release the delay immediately; an improving forecast keeps the delay active until another release condition is met.

    A mature household profile supplies the local-time demand remaining from now. Charging windows remain in the requested range because the house continues consuming while the battery operates; battery grid-charging energy is already cancelled by the AC-power term. Demand observed earlier today is not counted again. Diagnostic attributes include `consumption_forecast_source`, `profile_coverage_ratio`, and `profile_days`.

    The energy check uses a `30%` cushion:

    ```text
    release when net_solar < energy_needed × 1.3
    ```

    If only the cushion is missing while the bare energy requirement is still covered, a price-driven predictive mode waits for the cheapest remaining feasible hour, bounded by the projected point where the bare balance would fail. A genuine deficit releases immediately, as does a day without usable price data.

    A configured forecast that becomes `unavailable` or `unknown` does not disable the delay immediately. The state remains `Waiting for forecast` for a `5-minute` grace period. A provisional `0 kWh` **Remaining Today** value is also held during the first hour after local midnight. If data does not recover, the release remains re-evaluable rather than permanently latching a misleading forecast decision.

    The optional SOC setpoint ranges from `12–90%`, defaults to `50%` when enabled, and is disabled by default. Below it, charging is allowed and each battery stops individually on reaching the setpoint while lower batteries catch up. After every controlled battery reaches it, forecast delay applies to the fleet. A reached setpoint re-arms only after SOC falls `3` percentage points below it. During this phase, diagnostic attributes include `soc_setpoint`, `estimated_setpoint_time`, and `projected_unlock_time`.

    **Charge Delay Margin** ranges from `1–6 h` and defaults to `1 h`: a larger value releases earlier, while a smaller value waits longer. **Charge Delay Balance Deadband** defaults to `0.5 kWh` and prevents a near-balanced daily estimate from repeatedly changing the initial grid-needed decision.
