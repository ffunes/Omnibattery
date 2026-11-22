# Keep large loads and EV chargers off the battery

Load exclusion stops a large device from draining the battery as if it were ordinary household demand. You can still decide whether solar should go to the device first, whether the battery may cover the rest of the home, and whether a self-regulating wallbox needs room to react.

## Do I need it?

**Use it if** a large load is bigger than the battery, should be paid from the grid, or already has its own solar-surplus controller.

**You do not need it if** you want the battery to cover the device exactly like every other household load.

Choose the behavior that matches your goal:

| Goal | Behavior to use |
|---|---|
| Keep all or part of a large device off the battery | Exclusion and **Exclusion percentage** |
| Let an EV or another load take available solar before the battery charges | **Solar Surplus** |
| Let the battery cover household demand while solar and grid supply the large device | **Cover Home**, together with Solar Surplus |
| Coordinate with a wallbox that changes its own power from the grid meter | **Dynamic Power Control**, together with Solar Surplus |
| Manage a charger that reports charging state but no watts | **EV charger without power telemetry** |
| Keep predictive charging from promising the same solar to both battery and EV | **Expected remaining demand** and, when needed, its presence entity |

## Before you start

- Decide whether the main meter already includes the device.
- Identify whether the device exposes power telemetry, only an activity state, or both.
- For solar priority and Cover Home, configure a real-time external solar-production sensor when required by the behavior.
- Complete the sensor and field checklist in [Configure a large load or EV charger](../configuration/excluded-devices.md).

## How to enable it

1. Add the device through **Settings → Devices & services → Omnibattery → Configure → Excluded devices** using the [configuration checklist](../configuration/excluded-devices.md#how-to-enable-it).
2. Open the Omnibattery system device or dashboard and turn on **Device – Enabled**.
3. For an excluded device (one the main meter already includes), set **Device – Exclusion %** to the share the battery should ignore.
4. Turn on only the live behavior controls that match your goal: **Solar Surplus**, **Dynamic Power Control**, or **Cover Home**.
5. Start the device and watch grid flow, battery power, and the device's power or activity entity.

![Excluded-device controls in Home Assistant](../assets/screenshots/features/load-exclusion-entities.png){ width="700" style="display: block; margin: 0 auto;"}

## What you will see

- **Device – Enabled** turns the whole correction on or off. When off, automatic control treats the device according to the unadjusted meter reading.
- **Device – Exclusion %** chooses how much demand remains off the battery. At 100%, the battery covers none of the device; at 0%, it treats all of the demand as ordinary load. Intermediate values split the demand.
- **Device – Solar Surplus** gives the active device priority over battery charging when solar is available. The battery still does not discharge for the excluded share.
- **Device – Cover Home** lets the battery continue covering genuine household demand while only the device's grid share remains excluded.
- **Device – Dynamic Power Control** gives a flexible wallbox time to detect and claim changing export before the battery charges from the remainder.
- **Device – Status** shows what Omnibattery does with the device right now:

| State | Meaning |
|---|---|
| Paused | **Device – Enabled** is off; the device counts as ordinary home load |
| No power reading | The power sensor is unavailable or its value is not a number (a state-only EV charger: its activity sensor is unavailable) |
| Idle | The device draws no power (a state-only EV charger: it is not charging) |
| Priority (yielding) | Dynamic Power Control is holding battery charging back so the wallbox can claim the export |
| On solar surplus | The device runs on solar the battery would otherwise have charged from |
| Excluded from the battery | The battery does not power the excluded share (a state-only EV charger: while it charges, the battery does not discharge) |
| Covered by the battery | An additional device (not in the main meter) is added to demand, and the battery covers it |

Its attributes carry the device power, the adjustment applied (positive: kept from the battery; negative: added for the battery to cover), the Dynamic Power Control phase and any charge or discharge block. While the battery is grid charging or in manual mode the rule is not re-evaluated: the state still shows *Idle* or *No power reading* as soon as the device stops, but a device that starts keeps its last state until normal control resumes.

On the **Excluded devices** card each device has its own block, sorted by name and separated by a line: Enabled, Solar Surplus, Dynamic Power Control, Cover Home, Exclusion %, Status. A tag shows the device type, *excluded* (included in the main meter) or *additional*. Options show only where they apply: all of them only while the device is enabled, **Dynamic Power Control** and **Cover Home** only while **Solar Surplus** is on, and **Exclusion %** only for excluded devices. Each tooltip ends with its default (Enabled on, Solar Surplus, Dynamic Power Control and Cover Home off, Exclusion % 100%).

A state-only EV charger pauses battery charge and discharge when charging first appears. After the pause, the battery may charge from solar surplus but remains blocked from discharging for the EV until charging ends.

If the device has expected remaining demand configured, predictive charging subtracts that claim from the solar available to the battery. A presence entity releases the claim when the EV or device is absent. The [configuration page](../configuration/excluded-devices.md) contains the accepted units and presence rules.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The battery still covers the whole device | The exclusion is disabled or its percentage is zero | Check **Device – Enabled**, **Device – Exclusion %** and **Device – Status** |
| The battery ignores too much household demand | **Cover Home** is off while Solar Surplus is giving the device priority | Turn on **Cover Home** if you want the battery to cover the rest of the home |
| Battery and wallbox compete for changing solar | Dynamic Power Control is off or its activity signal is wrong | Turn on **Dynamic Power Control** and verify the configured activity entity changes before or with demand |
| The battery never charges while the flexible load is active | The wallbox keeps requesting priority, or no genuine export remains | Check device power, activity state, external solar production, and grid export |
| A state-only charger does not pause the battery | Its activity entity does not report a recognized active state | Check **Device active / EV charging sensor** in the device setup |
| Predictive charging skips cheap energy and the battery stays low | Solar promised to the device is not reserved | Configure **Expected remaining demand (kWh)** and a presence entity if the demand persists after disconnection |

??? "Advanced details"
    **Load correction**

    For a device already included in the main meter, Omnibattery removes the excluded share before proportional–derivative (PD) control calculates its adjustment:

    ```text
    effective consumption = grid consumption - excluded device power
    control error = effective consumption - grid target
    ```

    If the main meter does not see the device, Omnibattery first adds its power to reconstruct total demand, then applies the configured treatment. This prevents the same load from being removed twice.

    **Dynamic Power Control timing**

    A metered device is considered to be drawing above 100 W. On first demand, battery charging yields for 30 seconds. A rise of at least 200 W in available margin, calculated from solar production minus device power, starts another 20-second yield. If no solar-production sensor is available, Omnibattery probes for 20 seconds every 5 minutes instead.

    When device power falls, discharge remains blocked for 5 minutes and charging receives a shorter restart grace. An active activity sensor also blocks charging before measured power appears, avoiding a cold-start deadlock where the battery absorbs export before the wallbox starts. Dynamic Power Control applies only when the device is enabled, included in the main consumption sensor, metered, not in state-only EV mode, and both Solar Surplus and Dynamic Power Control are on.

    **State-only EV timing**

    When charging is first detected, Omnibattery commands 0 W, blocks both directions, and freezes PD state for 5 minutes. The pause gives the charger time to negotiate current with the vehicle. Afterward, solar-surplus charging may resume while discharge stays blocked until the activity state stops reporting charging.
