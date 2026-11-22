# Other batteries, read-only through Home Assistant

Omnibattery can take account of a battery it cannot control, as long as some other integration already reports it in Home Assistant. It reads the battery, counts it in the household-consumption calculation, and never sends it a command.

Use this for a vendor inverter with its own Home Assistant integration — a BYD stack behind a Kostal Plenticore, for example — where the battery runs its own self-consumption logic.

## Do I need it?

**Use it if** a second battery shares your grid meter and Omnibattery cannot command it. Without this, that battery's charging is billed to your household consumption and its discharging is subtracted from it, so the learned consumption profile and everything predicted from it drift away from reality.

**You do not need it if** the battery is already supported as a controllable brand. Check [Is my battery compatible?](../../compatibility.md) first — a controllable battery is always the better connection.

**It will not make the battery controllable.** Omnibattery never sends it a charge, discharge or idle command, it is never part of load sharing or the proportional–derivative (PD) controller, and its stored energy does not enter predictive grid charging.

## Before you start

- Install and configure the integration that reads the battery, and confirm it reports **state of charge in percent** and **battery power** (W or kW). Both are required.
- Note how its power sensor is signed. Check it while the battery is charging: several integrations, Kostal among them, report charging as a negative number.
- Find the battery's usable capacity in kWh. Nothing reports it, so you will type it in.
- Check which sensor you configured as Omnibattery's **solar production sensor**. On a hybrid inverter it must measure the **DC** side. See the warning below.

## How to add it

1. Open **Settings → Devices & services → Omnibattery → Configure → Batteries** and raise the battery count by one.
2. Choose **Other battery, read-only via Home Assistant entities** as the brand.
3. Fill in the form:

| Field | What to enter |
|---|---|
| **Name** | A descriptive name, for example `BYD` |
| **State of charge sensor** | The percentage sensor, e.g. `sensor.scb_battery_soc` |
| **Battery power sensor** | The power sensor (W or kW), e.g. `sensor.scb_battery_power` |
| **Power sign convention** | Whether the sensor counts charging or discharging as positive |
| **Charged energy counters** | Optional lifetime counters (Wh, kWh or MWh). Pick several if the source splits them — Kostal reports charging from solar and from the grid separately — and they are added together |
| **Discharged energy counter** | Optional lifetime counter (Wh, kWh or MWh) |
| **Target state of charge** | Optional writable number, e.g. `number.scb_battery_min_soc` |
| **Treat telemetry as lost after** | How long the source integration may stay silent. Raise it for a slow poller |

4. On the next step, enter the **nominal capacity** in kWh. It is the only limit the form asks for.

## What you will see

A device with a state-of-charge sensor, a battery power sensor, and the energy counters you configured. If you pointed at a writable target state of charge, a **Target SOC** number appears as well; you set it yourself, Omnibattery never writes it on its own.

You will not see allow-charge, allow-discharge, manual control, force mode, software SOC limits or power ceilings. Those shape commands this battery never receives, so they are deliberately absent.

The visible effect is on **Home Consumption**: it should now agree with your inverter's own household figure while the battery is charging or discharging. That correction is the reason for this connection.

!!! warning "Check your solar production sensor on a hybrid inverter"
    Omnibattery derives household load as `grid + battery power + solar production`. On a hybrid inverter, pick the sensor that measures **DC** production (the sum of the string inputs). The inverter's AC output already has battery charging subtracted from it, so using that instead subtracts the battery twice once this battery is configured.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The wizard says it could not read some entities | The entity does not exist, is unavailable, or reports text | The error names the missing value. Open **Developer tools → States** and confirm the entity currently shows a number |
| Household consumption is now too low | The power sign convention is inverted | Watch the power sensor while the battery charges and set the field to match |
| Household consumption is still too high while the battery charges | The solar production sensor measures the AC side | See the warning above |
| The battery keeps going unavailable | The source integration polls more slowly than the configured limit | Raise **Treat telemetry as lost after** |
| The system state of charge includes this battery | Working as intended | The system state of charge, total capacity and stored energy describe the installation, and this battery is physically part of it. Only the *plan* leaves it out — see the row below |
| Predictive charging buys grid energy although this battery is full | Working as intended, for now | Its kilowatt-hours are deliberately kept out of the energy balance. See the note below |

!!! warning "Known limitation of this first stage"
    A battery Omnibattery cannot pause can discharge *into* a grid charge it is
    paying for. If the observed battery runs its own self-consumption logic, it
    covers the house from its own cells while Omnibattery is buying cheap grid
    energy for the batteries it does control — so part of what you bought ends
    up replacing energy the other battery would have supplied for free a few
    hours later. Nothing is damaged and nothing is lost twice, but the night
    charge can come out larger than it needed to be.

    There is no setting for this yet. If it costs you real money, the practical
    workaround is to raise the other battery's own minimum SOC during the hours
    Omnibattery grid-charges, so it stops discharging while that happens.

??? "Advanced details"
    Omnibattery publishes this battery's power under `battery_power` only, never `ac_power`. The aggregates then use their documented fallback of `-battery_power`, which is what a battery with no AC port of its own contributes to the household balance.

    The battery counts towards the system state of charge, the total capacity and the stored-energy total, because those describe the installation rather than what Omnibattery intends to do with it. Predictive charging is the one place it is left out.

    The battery is excluded from `_get_available_batteries`, from load sharing, from the discharge ladder and from the primary-battery nomination, and it never reaches `apply_setpoint`. It is also excluded from the DC-coupled scarcity measure that steers the charge order, because room this integration cannot fill would otherwise make every day look scarce and park the batteries that could have taken the energy.

    No code path can command it: besides being filtered out at selection time, both `_set_battery_power` and the coordinator's own `apply_power` refuse any write to it — including an idle 0 W — so a caller that forgets the filter still cannot reach it.

    It is likewise left out of the measured round-trip efficiency that sets the arbitrage margin. Its own counters may be perfectly honest, but the energy an arbitrage purchase buys is round-tripped through the batteries Omnibattery commands, and only their efficiency may decide what price spread that purchase has to clear.

    Its stored energy is currently kept out of predictive charging entirely. Counting it would need the stored half to enter the balance while the headroom half stays out — Omnibattery can neither grid-charge this battery nor release it on demand — and that split is a separate change.

    The **Target SOC** number writes to the configured entity, rounded onto the step the upstream entity accepts and clamped to its range.
