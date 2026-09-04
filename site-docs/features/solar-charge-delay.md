# Solar charge delay

Delays morning battery charging (both from solar and from the grid) while the expected solar production is sufficient to cover the required energy. Avoids charging the battery early — whether from solar or the grid — when the sun will be able to do it later.

## When it applies

- Morning charge after the battery has discharged overnight.
- Weekly 100% charge (waits for the sun to complete the charge before resorting to the grid).

## Solar model

The integration uses a **sinusoidal model** based on the current day's solar forecast to estimate hour-by-hour solar production throughout the day. It compares the expected cumulative production from the current hour until sunset with the remaining energy needed.

```
If remaining_solar_production >= energy_to_charge:
    Wait (the sun will charge it)
Else:
    Start charging (solar or grid)
```

## Live forecast

The integration reads the solar forecast sensor live, with no nightly capture or storage. A saved **Remaining Today** sensor is used directly and replaces the legacy whole-day setting. Untouched legacy entries remain supported during the transition and are converted to an estimate of the remaining production.

Every time the sensor value changes by more than 0.05 kWh, the integration re-evaluates the energy balance:

- **Forecast degrades** until `(usable_energy + forecast) < avg_daily_consumption` → the delay unlocks and charging starts immediately.
- **Forecast improves** while the delay is still active → the system keeps waiting for the sun to charge the battery.

Once the delay is unlocked it stays unlocked for the rest of the day.

## Household demand horizon

When the 28-day quarter-hour household profile is mature, Solar Charge Delay uses
the same local-time remaining-consumption range as predictive charging. The
profile keeps predictive charging windows in the requested range because the
household keeps consuming while the battery operates; battery grid-charging
energy is already cancelled by the AC-power term. Demand already observed today
is never counted again. During learning, the delay falls back to the legacy daily
estimate. The Charge Delay sensor attributes expose `consumption_forecast_source`,
`profile_coverage_ratio` and `profile_days` for this handoff.

!!! note "Cushion-only shortfall waits for the cheapest hour"
    The energy-balance unlock fires at `net_solar < energy_needed × 1.3`, where the 30 % is a safety cushion rather than the target itself. When only that cushion is missing (`net_solar` is still at or above the bare `energy_needed`) and predictive charging runs in a price-driven mode, the delay does not release on the spot: it holds for the cheapest remaining hour, bounded by the moment the unfactored balance is projected to break. Self-charging then lands in the midday price trough instead of the morning export peak, and the SOC target stays reachable. A genuine deficit (`net_solar < energy_needed`) still unlocks immediately, as does any day without usable price data.

!!! note "Transient forecast gaps and manual re-evaluation"
    A configured forecast sensor that reads `unavailable`/`unknown` for a moment — while it refreshes, or during the window after a Home Assistant restart before all sensors have loaded — no longer disables the delay for the whole day. The same protection covers a provisional `0 kWh` from the **Remaining Today** sensor during the first hour after midnight. The delay is held through a short grace window (sensor state `Waiting for forecast`) and only unlocks if the reading does not recover in time. If the delay did already unlock and you want it back the same day, **toggle the Solar Charge Delay switch off and then on**: that re-evaluates the delay from scratch instead of waiting for the midnight reset.

## SOC setpoint

An optional SOC setpoint (12–90 %, disabled by default) splits charging into two phases:

1. **Below the setpoint** — the battery charges freely (solar and grid), the delay is inactive. Sensor state: `Charging to setpoint`.
2. **At or above the setpoint** — the solar delay logic activates and decides whether to keep charging or wait.

This is useful when the battery is deeply discharged and needs a guaranteed minimum charge before the solar decision is made. For example, with a setpoint of 50 % the battery charges to 50 % without restrictions; above 50 % the system evaluates whether remaining solar production is enough to complete the charge and waits if it is.

During this first phase, the sensor publishes `soc_setpoint`, `estimated_setpoint_time`, and `projected_unlock_time`. Both timestamps are informational projections; once the solar delay is active, `estimated_unlock_time` becomes the controller's authoritative estimate.

The setpoint is enabled with a dedicated checkbox in the configuration. When disabled, the delay applies from the very start of charging. The minimum value is 12 % — the minimum discharge SOC of the Venus batteries.

## Dashboard configuration

| Field | Description | Default |
|---|---|---|
| **Safety margin (h)** | Hours before sunset by which charging must be complete. | 1 h |
| **Solar forecast sensor** | Used when a forecast sensor was not configured during the initial setup. | — |
| **Enable minimum SOC before delay** | When enabled, the battery charges to the configured SOC before the solar delay starts. | Disabled |
| **Minimum SOC (%)** | Battery SOC to reach before the solar delay takes effect. | — |
| **Balance deadband (kWh)** | Tolerance for the energy-balance check. If battery plus forecast is below expected consumption, the delay lasts longer. | `0.5 kWh` |

A larger margin (for example, 180 minutes) unlocks grid charging earlier in the day; a smaller margin waits longer for the sun to cover the energy.

![Solar charge delay configuration](../assets/screenshots/configuration/advanced-solar-charge-delay-config.png){ width="650" style="display: block; margin: 0 auto;"}

## Requirements

- Solar forecast sensor configured in the [initial setup step](../configuration/main-sensor.md).

![Solar charge delay attributes](../assets/screenshots/features/solar-charge-delay-attributes.png){ width="650"  style="display: block; margin: 0 auto;"}
