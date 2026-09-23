# Predictive charging with fixed time slots

Time Slot mode buys only the energy your home is expected to need during the cheap weekly windows you define. It never opens an unconfigured charging window.

## Do I need it?

**Use it if** your tariff has predictable off-peak periods that repeat on known days, including tariffs with separate night and midday periods.

**You do not need it if** your provider publishes changing future prices and you want Omnibattery to pick the cheapest periods; use [Dynamic Pricing](dynamic-pricing.md). If you know only the current price, use [Real-Time Price](real-time-price.md).

## Before you start

- Decide which days and start/end times are cheap. You can configure up to three predictive charging windows.
- A solar forecast sensor is optional. Prefer a remaining-energy sensor when available; a whole-day forecast is supported as a fallback.
- Configure the common predictive-charging requirements described in [Which mode should I choose?](index.md).

## How to enable it

1. Open **Settings → Devices & services → Omnibattery → Configure** and choose **Time Slot** as the predictive charging mode.
2. Enter both the start and end time for **Charging window 1**, then select its active days.
3. Add **Charging windows 2 and 3** only if your tariff has more cheap periods; complete both times for every window you use.
4. Select an optional solar forecast sensor, finish the form, and confirm **Predictive Charging** is on in the Omnibattery **Control** tab.

![Configure fixed predictive charging windows](../../assets/screenshots/configuration/predictive-charging/time-slot-form.png){ width="650" style="display: block; margin: 0 auto;" }

## What you will see

At the start of an active window, Omnibattery checks the remaining energy balance and gives that window a quota. It can share the required energy across several windows, so the first window does not automatically consume the whole flexible daily target. Charging stops when the quota is stored or the window ends.

**Predictive Charging Active** shows the decision and any uncovered energy. **Re-evaluate Predictive Charging** forces a fresh decision on the next control cycle while a configured window is active. Outside a window, the button invalidates the old reference for the next window but does not evaluate immediately or start charging.

The plan adapts inside an active window when SOC changes materially, the guaranteed floor is crossed or recovered, the provider revises the solar forecast, a relevant battery or forecast setting changes, or [capacity protection](../../features/peak-shaving.md) changes the power available to the plan.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| Charging never starts | Today or the current time does not match a complete configured window | Start/end times, active days, and windows that cross midnight |
| The current window appears but no charge is needed | Battery energy and expected solar cover the remaining demand | **Predictive Charging Active** decision attributes |
| A changed forecast does not start charging at midday | No configured charging window is open | Wait for the next window or add a tariff-valid window |
| The first window stops before reaching the full daily target | Energy has been assigned to a later window | Per-window quota and remaining selected windows |
| The plan reports an energy shortfall | The configured windows or physical charge power cannot deliver enough energy before its deadline | Window duration, charge limits, battery capacity, and active blockers |
| Pressing re-evaluate does nothing | The button was pressed outside an active window | Press it during a configured window; outside one it prepares the next entry |

??? "Advanced details"
    ### Evaluation and quotas

    On window entry, Omnibattery evaluates immediately when no solar forecast is configured or the configured forecast is readable. If the forecast is temporarily unavailable, it retries during a five-minute grace period; after that it evaluates conservatively with zero solar.

    The planner simulates consumption, solar, and usable battery energy in 15-minute intervals until midnight. Energy needed before a projected minimum-SOC crossing is assigned only to windows that can deliver it in time. Later energy is distributed across the remaining configured windows. If no window can meet a deadline, the uncovered kWh is reported as an energy shortfall instead of being assigned to a window that is too late.

    Each window receives its own kWh quota. Charging stops when the live battery reaches that quota or the window ends. A suspended quota remains attached to the plan and is rebuilt from live SOC when charging can continue.

    ### Re-evaluation triggers

    Inside an active charging window, the balance is reconsidered when:

    - average battery SOC moves by at least 30 percentage points from the last evaluation;
    - **Guaranteed Minimum SOC** is crossed or recovered;
    - the provider revises remaining solar by at least 1.5 kWh in either direction;
    - battery minimum/maximum SOC, **Solar Forecast Safety Margin**, or the guaranteed floor changes;
    - capacity protection changes whether the planned charge can be delivered; or
    - you press **Re-evaluate Predictive Charging**.

    Solar-revision checks compare the new reading with a projection that subtracts solar already produced, so the normal decline of a remaining-energy forecast does not look like a provider revision. They use a 30-minute cooldown and allow up to four re-evaluations per day. An unavailable forecast is not interpreted as the forecast collapsing.

    Only a re-evaluation that reverses the current decision replaces its notification; other updates are silent. These triggers operate only inside a configured window because this mode cannot buy grid energy outside one.

    ### Solar timeline and household demand

    Time Slot and Dynamic Pricing share a dated solar timeline and one remaining-energy budget. Explicit provider periods have priority; a mature locally learned solar profile comes next, then the sinusoidal daylight fallback. The timeline changes deadlines but never increases the forecast total or creates a charging window.

    Household-consumption history still covers the whole day. Battery AC charging is removed from derived household demand so a predictive window does not teach the profile that the battery itself is a recurring home load.

    **Solar Forecast Safety Margin** is subtracted from expected solar. For a new installation its initial value is approximately 5% of configured fleet capacity; it falls back to no margin when capacity is unknown during setup. The margin is a live control entity rather than a field in this mode's setup form.
