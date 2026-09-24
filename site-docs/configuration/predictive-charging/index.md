# Predictive charging

Predictive charging buys grid energy during cheap periods when the battery and expected solar will not cover household demand. Choose the mode by asking what your tariff tells you about *when* energy is cheap.

| Your tariff or price source | What you want | Recommended mode |
|---|---|---|
| The cheap periods repeat on a known weekly schedule | Allow grid charging only in the windows you choose | **[Time Slot](time-slot.md)** |
| Your provider publishes future prices | Let Omnibattery choose the cheapest periods that still meet the energy deadline | **[Dynamic Pricing](dynamic-pricing.md)** |
| You can read only the price in effect now | Charge whenever the live price is below your threshold | **[Real-Time Price](real-time-price.md)** |
| Energy costs the same all day and there is no fixed cheap window | Keep normal battery control and solar charging | **Do not enable predictive grid charging** |

All three modes calculate whether energy is missing. The difference is who chooses when it may be bought: you, a future price calendar, or the current price.

## Do I need it?

**Use it if** your tariff has cheaper periods and you want Omnibattery to buy only the energy it expects the home to need.

**You do not need it if** grid energy costs the same all day, you want the battery to charge only from solar, or another energy manager already schedules grid charging.

## Before you start

- Configure the battery and a working [main grid sensor](../main-sensor.md).
- Prepare the schedule or price source required by the mode in the table above.
- A solar forecast is optional. A remaining-energy forecast is preferred because it does not count solar already produced; without a usable forecast, Omnibattery plans conservatively with no future solar.
- A local household-consumption profile improves the estimate. See [Daily and hourly consumption estimate](../../features/consumption-estimate.md).

## How to enable it

1. Open **Settings → Devices & services → Omnibattery → Configure** and enable predictive charging configuration.
2. Choose **Time Slot**, **Dynamic Pricing**, or **Real-Time Price** using the table above.
3. Enter that mode's schedule or price source, add an optional solar forecast sensor, and finish the form.
4. Open the Omnibattery **Control** tab and confirm **Predictive Charging** is on.

![Choose a predictive charging mode](../../assets/screenshots/configuration/predictive-charging/mode-selector.png){ width="600" style="display: block; margin: 0 auto;" }

## What you will see

**Predictive Charging Active** shows whether a grid charge is needed, planned, or running. A visible cheap period can be informational when the battery and expected solar already cover demand; it is not always a pending charge.

When grid charging is needed, Omnibattery targets only the calculated deficit instead of filling every battery to its maximum state of charge (SOC). In a multi-battery system, it shares that target according to each battery's available capacity. Reaching the grid-charge target does not block later solar surplus: the battery can continue in a solar-only state.

Use **Guaranteed Minimum SOC** if the whole-day balance looks sufficient but the battery often reaches its minimum before solar production begins. The switch and number entity set a reserve that must be available before the expected solar start.

Use **Re-evaluate Predictive Charging** after a material forecast or setting change. In Dynamic Pricing it rebuilds the remaining schedule immediately. In Time Slot mode it forces a fresh decision on the next control cycle inside an active charging window; pressing it outside a window does not open one. Real-Time Price has no button because it decides again on every control cycle.

Turn off **Predictive Charging** to pause all predictive grid charging and its Dynamic Pricing subfeatures.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| No grid charge is planned | Stored energy and expected solar already cover demand | **Predictive Charging Active** and its decision attributes |
| Cheap periods appear but charging does not start | The calendar is informational, the quota is already met, or a safety/control rule is active | `charging_needed`, the active mode page, battery SOC limits, and **Integration Status** |
| The plan reports an energy shortfall | Eligible periods cannot deliver enough energy before it is needed | Charging power, battery capacity, price ceiling, configured windows, and physical blockers |
| The battery charges too much or too little | The solar or household-demand estimate does not match the remaining day | Forecast sensor type, consumption profile coverage, and **Solar Forecast Safety Margin** |
| Charging pauses while household load rises | Contracted-power, phase, or capacity protection is preserving the import limit | [Capacity protection](../../features/peak-shaving.md) and [Main grid sensor](../main-sensor.md) |
| A setting change has no immediate effect | The active plan has not yet been rebuilt | Press **Re-evaluate Predictive Charging** where available |

??? "Advanced details"
    ### Energy decision and charge target

    Omnibattery compares usable battery energy above minimum SOC, expected solar production, and expected household consumption over the mode's planning horizon:

    ```text
    if usable_battery + solar_forecast < expected_consumption:
        grid_charge = expected_consumption - usable_battery - solar_forecast
    else:
        grid_charge = 0
    ```

    It then reserves battery space for solar rather than filling to maximum SOC from the grid:

    ```text
    solar_surplus = max(0, solar_forecast − estimated_consumption)
    grid_charge   = max(0, gap_to_max − solar_surplus)
    target_soc    = current_soc + grid_charge / capacity × 100
    ```

    **Example**: the battery needs 5 kWh to reach `max_soc`. Solar forecast is 13 kWh and expected consumption is 10 kWh, leaving a surplus of 3 kWh available for the battery. Omnibattery charges only **2 kWh** from the grid; solar handles the remaining 3 kWh during the day.

    In a multi-battery fleet, the grid target is distributed in proportion to each battery's gap to its configured maximum SOC. Dynamic Pricing and Time Slot can also assign a quota to each period and transfer missed energy only to later periods that still meet its deadline.

    **Solar Forecast Safety Margin** is subtracted from expected solar once. New installations default to approximately 5% of total configured battery capacity; if capacity is unavailable during setup, the fallback is no margin.

    ### Household demand during a charging slot

    A predictive period remains responsible for the batteries until it ends or reaches its target. Normal proportional–derivative (PD) control does not immediately take over when household demand rises because grid import can still include the battery's previous charge command.

    The import ceiling is the lower of contracted power and the capacity-protection limit when that feature is enabled. Omnibattery first reduces charging so the house gets the available grid capacity. If the calculation crosses into discharge during an ordinary overshoot, it keeps the smallest effective positive charge and preserves the incremental PD state.

    A physical excess becomes an emergency only after three consecutive fresh meter publications confirm it. Emergency protection can then wait for inverter response and discharge only the settled excess. After two fresh samples show at least `max(200 W, 2 × PD deadband)` of available capacity, charging resumes from that margin rather than from maximum battery power.

    For example, with a 2,000 W contracted-power limit and a settled physical household load of 2,800 W, emergency protection requests about 800 W of discharge to keep import near 2,000 W. A short inrush that disappears while telemetry settles does not trigger that discharge.

    `0 W` remains reserved for explicit blockers, battery-management-system (BMS) limits, unavailable batteries, critical telemetry, the end of a period, reached SOC, phase protection, or a confirmed safety emergency. Safety discharge can bypass economic price or solar-curtailment blocks, but it cannot bypass minimum SOC, unavailable or manually controlled batteries, backup/RS-485 restrictions, device and system limits, or phase protection.

    If the grid meter stops publishing, a protective command is not increased from an old value. Once telemetry exceeds the stale-data limit, automatically controlled batteries return to idle until fresh data settles.

    A suspended charge keeps its target and undelivered-energy record. Dynamic Pricing tries to move the quota to eligible future periods; Time Slot rebuilds the remaining-window plan from live SOC; Real-Time Price records the shortfall because it has no future price calendar.

    ### Guaranteed minimum SOC

    The total energy balance can be positive while the battery is still projected to empty before solar starts. **Guaranteed Minimum SOC** adds enough energy to preserve the selected floor until effective solar production begins. Dynamic Pricing chooses eligible cheap periods before that deadline; configured price limits and physical blockers still apply, so an impossible guarantee appears as an energy shortfall.

    Charging stops at the floor when this reserve is the only reason to charge. It re-arms after SOC falls five percentage points below the floor, preventing repeated switching at the boundary.

    ### Consumption and solar timelines

    Mature installations use the local 15-minute household-consumption profile. The legacy daily estimate remains a fallback. Dynamic Pricing and its daytime re-evaluations request the remaining local-time horizon through the next sunrise. Predictive charging periods are removed from derived household demand so battery charging is not learned as home consumption.

    Solar total and solar timing are separate inputs. The timeline priority is:

    1. Valid dated periods supplied by the forecast provider.
    2. A mature local profile learned from direct solar and battery maximum-power-point-tracking (MPPT) telemetry.
    3. A sinusoidal daylight curve.
    4. A zero timeline when no safe daylight window exists.

    The learned profile is normalized before the forecast budget is applied. It shapes when forecast energy arrives; it does not predict the total, repair a poor weather forecast, control a solar inverter, or reconstruct curtailed energy.

    Useful decision attributes include `solar_timeline_source`, `solar_remaining_raw_kwh`, `solar_remaining_effective_kwh`, `solar_timeline_fallback_reason`, `solar_profile_mature`, `solar_profile_coverage_ratio`, `chronological_planning_active`, `slot_energy_targets_kwh`, and `total_shortfall_kwh`.

    ### Notifications

    Time Slot can notify one hour before a configured period and when charging starts. Dynamic Pricing also checks before future selected periods, during the late-day assessment, after a material SOC drop, and when tomorrow's prices become available. The active mode pages describe the exact behavior.

    ![Predictive charging notification](../../assets/screenshots/configuration/predictive-charging/notification-example.png){ width="500" style="display: block; margin: 0 auto;" }
