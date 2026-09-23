# Protect each phase of a three-phase supply

Three-phase current protection limits automatic battery commands on the phase where each battery is connected. It helps keep battery charging and discharging within the current margin you reserve for that phase.

## Do I need it?

**Use it if** your batteries share a three-phase installation and you need a separate current ceiling for each physical phase.

**You do not need it if** your installation is single-phase, your electrical protection already handles the required operating envelope without software limits, or you cannot measure signed current on each phase you want to protect.

## Before you start

- Use a signed root mean square (RMS) current sensor in `A` or `mA` for every protected phase. Positive must mean grid import and negative must mean grid export after the global meter-sign setting is applied.
- Know the current limit for each phase and leave operating margin below the fuse nameplate rating.
- Confirm which physical phase each battery uses. Omnibattery cannot discover the wiring.
- Treat this feature as an additional software guard, not as a replacement for breakers, inverter protection, or electrical design.

## How to enable it

1. Open **Settings → Devices & services → Omnibattery → Configure → Sensors** and enable **Three-phase current protection**.
2. For each protected phase, select its **L1/L2/L3 grid current sensor** and enter the matching **L1/L2/L3 fuse size (A)**. Leave both fields empty for an unused phase.
3. In each battery's setup, set **Physical battery phase** to its actual conductor. Choose **Unassigned** only when the battery is outside the protected phase layout.
4. Finish setup, then turn on **Three-Phase Current Protection** from the Omnibattery dashboard or device page.
5. Confirm that **Three-Phase Protection Status** reports **Active** under normal load.

## What you will see

**Three-Phase Protection Status** shows one of these states:

| State | Meaning |
|---|---|
| **Disabled** | The runtime protection switch is off |
| **Active** | Configured phase sensors are healthy and no battery command is being reduced |
| **Limiting Batteries** | At least one automatic battery command is being capped |
| **Degraded / Failsafe** | A configured phase cannot provide a valid current reading |

When the protection switch is on, each battery's **Physical Battery Phase** selector is available for runtime correction. A changed assignment persists and applies on the next automatic control cycle. The global grid-power sensor remains the control signal; phase sensors only limit the result.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| Status is **Degraded / Failsafe** | A configured current sensor is missing, stale, unavailable, non-numeric, or in the wrong unit | Check that the entity reports a fresh numeric value in `A` or `mA` |
| Battery charging on one phase stays at zero | Its protected phase has no valid current reading | Check that phase's sensor, limit, and physical assignment |
| A battery is never limited | It is **Unassigned**, or its phase has no complete sensor-and-limit pair | Set the physical phase and configure both fields for that phase |
| Import and export limits act in the wrong direction | The phase-current sign is reversed | Compare a known grid import with the sensor and review **Inverted meter sign** |
| The phase meter still exceeds the configured limit | An external load, command latency, or a manual command caused the excess | Reduce the configured operating limit and keep manual commands within the electrical envelope |

??? "Advanced details"
    For each phase, Omnibattery reconstructs current that does not come from the battery, then intersects the meter limit with an absolute battery-current limit:

    ```text
    base_current = phase_current - battery_current_on_phase
    battery_current_min = max(-phase_limit, -phase_limit - base_current)
    battery_current_max = min(+phase_limit, +phase_limit - base_current)
    charge_budget_current = max(0, battery_current_max)
    discharge_budget_current = max(0, -battery_current_min)
    ```

    Battery commands use positive watts for charge and negative watts for discharge. Current budgets are converted with a nominal 230 V and 0.90 power factor, then rounded down in 5 W steps. Normal battery selection and proportional allocation run first. Power rejected by a phase cap may move to healthy phases with available battery capacity, following the normal state of charge (SOC) and energy priority.

    The envelope applies to proportional–derivative (PD) control, direct tracking, predictive grid charging, automatic time-slot control, active balancing, and the final shared automatic-command guard. The accepted total is fed back to the controller to prevent windup.

    A current reading older than 65 seconds is stale. If a configured phase has no valid reading, new charging on that phase is capped at 0 W. A previously measured safe discharge may be held because stopping it would hand the household load back to the grid and could increase phase current. Healthy phases continue. A phase with no configured sensor-and-limit pair has no phase cap. An **Unassigned** battery also remains outside the envelope.

    The status entity exposes diagnostic attributes including `limited_batteries`, `limited_battery_details`, `unassigned_batteries`, `degraded_phases`, and per-phase readings, budgets, and assignments under `phases`.

    Manual register writes and manual time-slot commands can bypass this envelope. Home Assistant creates a Repair while protection is enabled to remind you to keep those commands within the configured current limits. The guard cannot remove current caused by an external load, detect incorrect conductor mapping, or issue simultaneous charge on one phase and discharge on another.
