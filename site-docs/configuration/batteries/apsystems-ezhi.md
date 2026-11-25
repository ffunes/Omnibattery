# APsystems EZHI

Omnibattery controls an APsystems EZHI hybrid microinverter and its battery through the EZHI's local HTTP interface. No cloud connection and no credentials are required.

## Do I need it?

**Use it if** you own an EZHI, Home Assistant can reach it on your network, and you can switch it to **Local Mode** in the AP EasyPower app.

**Expect a 1,200 W limit** in both directions. EZHI support is new: include the firmware version when reporting behavior that differs from this page.

## Before you start

- Enable **Local Mode** in the AP EasyPower app. Without it the EZHI accepts commands but does not act on them.
- Give the EZHI a stable local IP address and confirm Home Assistant can reach it.
- Nothing else is needed: the EZHI reports its own battery capacity. The wizard asks for it only if the device does not.

## How to add it

1. In the Omnibattery setup flow, choose **APsystems EZHI**.
2. Enter a descriptive **Name** and the EZHI's **IP address**.
3. Keep **Port** at `80` unless you changed it, and wait for the connection test.
4. Choose the common power and state-of-charge limits.

## What you will see

Omnibattery provides automatic control and software **Force Mode** and power controls. Turn on **Battery Manual Control** before sending a manual charge, discharge, or idle target. The integration sends one net power target to the EZHI and checks that the device took it.

Entities include battery state of charge (SOC), **Battery Power**, **Solar Power**, **AC Delivered Power**, battery temperature, state of health, and lifetime charged and discharged energy. **Solar Power** counts as system solar for Omnibattery's calculations. **Inverter Temperature** and **Power Setpoint** are created disabled.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The wizard cannot connect | Local Mode is off, or the address or port is wrong | Enable **Local Mode** in AP EasyPower and test that the IP address is reachable |
| The battery ignores commands | The EZHI left Local Mode | Check the system mode in AP EasyPower |
| The wizard asks for the capacity | The EZHI did not report it | Enter the battery's nominal capacity in kWh |
| Manual values have no effect | The battery is still in the automatic pool | Turn on **Battery Manual Control** |

??? "Advanced details"
    The EZHI accepts `-1,200` to `1,200 W`; Omnibattery uses that fixed range for both charge and discharge. There is no cell-voltage taper and no hardware SOC cutoff: the SOC limits are applied in software.

    The driver reads `/getDeviceInfo` (serial, firmware, capacity), `/getOutputData` (SOC, state of health, battery, solar and AC port power, temperatures, lifetime kWh) and `/getPower`, and writes `/setPower?p=<W>`. `/setPower` answers success in every system mode but acts only in Local Mode, so each command is confirmed against the setpoint that `/getPower` reports.

    Solar feeds the EZHI's AC port directly, so battery power and AC port power differ while the sun shines. Delivery checks use the AC port (**AC Delivered Power**), which is what the setpoint controls.

    For shared runtime controls and system limits, see [Choose your battery connection](index.md).
