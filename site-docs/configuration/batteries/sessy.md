# Sessy Home Battery

Omnibattery controls a Sessy through the local interface on its network dongle. No cloud connection is required.

## Do I need it?

**Use it if** Home Assistant can reach the Sessy dongle and you have the username and password printed on it. **Expect a delayed first response:** a Sessy leaving standby can take up to 65 s to follow its first non-zero command.

Sessy support is still looking for more testers. Include the model and firmware when reporting behavior that differs from this page.

## Before you start

- Give the Sessy dongle a stable local address and confirm Home Assistant can reach it.
- Find the dongle's API username and password.
- Know the battery's nominal capacity; the local interface does not report it.
- Keep the connection local to your trusted network.

## How to add it

1. In the Omnibattery setup flow, choose **Sessy**.
2. Enter a descriptive **Name** and the dongle's **Host**.
3. Keep **HTTP port** at `80` unless the dongle uses another port.
4. Enter the dongle **Username** and **Password** and wait for the connection test.
5. Enter nominal capacity and choose the common power and state-of-charge limits.

## What you will see

Omnibattery provides automatic control and software **Force Mode** and power controls. Turn on **Battery Manual Control** before sending a manual charge, discharge, or idle target. The integration sends one net power target through the Sessy local interface.

Battery state of charge (SOC), power, temperature, energy, and the device's PV reading can appear as entities. The PV reading is informational: the Sessy driver does not advertise it as solar telemetry for Omnibattery's control calculations.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The wizard cannot connect | The address, port, username, or password is wrong | Copy the credentials from the dongle and test network reachability |
| The first command appears to do nothing | The battery is waking from standby | Wait up to 65 s before treating the command as failed |
| Stored energy or predictive calculations are missing | Nominal capacity was not entered correctly | Reconfigure the battery and enter its capacity |
| Manual values have no effect | The battery is still in the automatic pool | Turn on **Battery Manual Control** |
| PV is visible but not counted as system solar | Sessy does not declare this reading as a controller solar source | Configure the installation's normal solar sensor if required |

??? "Advanced details"
    Sessy uses asymmetric limits: `2,200 W` for charging and `1,700 W` for discharging. The setup form seeds those ceilings and accepts nominal capacity from `0.01–100 kWh`.

    The local interface uses positive generation/discharge values, while Omnibattery's internal sign uses positive charge and negative discharge. The driver converts the sign before sending the net setpoint and reads it back for confirmation.

    Sessy's strategy and power target are controlled through its authenticated local API. It has no Marstek-style force-mode registers, hardware SOC writes, cell-voltage taper, or cell-balance telemetry.

    For shared runtime controls and system limits, see [Choose your battery connection](index.md).
