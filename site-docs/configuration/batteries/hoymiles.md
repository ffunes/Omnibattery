# Hoymiles MQTT batteries

Omnibattery controls supported Hoymiles batteries through the MQTT integration already configured in Home Assistant. Home Assistant owns the broker connection, so Omnibattery only needs the battery's device ID.

## Do I need it?

| Supported product | Nominal capacity | Integration power ceiling | Solar visible to Omnibattery |
|---|---:|---:|---|
| MS-A2 | 2.24 kWh per unit; up to 4.48 kWh | 1,000 W per unit; up to 2,000 W | No |
| HiBattery 1920 AC | 1.92 kWh per unit; up to 11.52 kWh | 1,000 W per unit; up to 6,000 W | No |
| HiBattery 4020 X | 4.02 kWh per pack; up to 16.08 kWh | 2,500 W charge and discharge | Yes |
| HiBattery 4020 AC | 4.02 kWh per pack; up to 16.08 kWh | 2,500 W charge and discharge | No |

**Use it if** the battery firmware exposes **MQTT Service**, the battery can reach your local MQTT broker, and its complete device ID is known. The device-published MQTT envelope remains authoritative and can reduce the table's ceiling.

## Before you start

- Commission the battery in **S-Miles Home**.
- Install firmware that exposes **MQTT Service**.
- Configure a local MQTT broker through Home Assistant and make it reachable from the battery.
- Record the complete MQTT device ID.
- For an MS-A2, follow the [MS-A2 installation and MQTT guide](../hoymiles-ms-a2.md).

## How to add it

1. In the Omnibattery setup flow, choose **Hoymiles MQTT**.
2. Enter a descriptive **Name** and the complete **MQTT device ID**.
3. Leave **Battery model** on **Auto-detect** unless the firmware publishes an incorrect or generic model.
4. Wait while Omnibattery receives live telemetry and the retained power-control discovery message.
5. Review the detected capacity and power ceilings, then choose the common state-of-charge limits.

## What you will see

Omnibattery sends automatic and manual charge, discharge, and idle targets through MQTT. Turn on **Battery Manual Control** before using the software **Force Mode** and power controls. The command is refreshed while control remains active so the battery does not return to its internal strategy.

State of charge (SOC), battery power, voltage, temperature, capacity, and energy readings appear when published by the model. Only HiBattery 4020 X is treated as a system solar source. No supported Hoymiles profile exposes MPPT telemetry for Marstek-specific MPPT correction.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The wizard reports **Cannot connect** | MQTT is disconnected, the service is disabled, or the device ID is incomplete | Check the Home Assistant MQTT integration, S-Miles Home, and the full ID |
| The wrong model or capacity appears | Firmware published a generic or incorrect model | Reconfigure and choose the installed model explicitly |
| Power is lower than the table | The retained MQTT envelope is lower or asymmetric | Inspect the device's power-control discovery message |
| The battery returns to autonomous control | Command refreshes cannot reach the broker | Check broker availability and MQTT disconnects |
| Solar is absent on an AC model | Only HiBattery 4020 X declares an independent solar source | Use the installation's external solar sensor if needed |

??? "Advanced details"
    Detected MQTT model aliases include `MS-A2`, `MS-A2-FX`, `MS-A2-ZZ`, `HB-1920-AC-SV`, `HB-4020-X`, `HB-4020-XM`, `HB-4020-AC`, and `HB-4020-ACM`. Capacity scales with detected units or packs up to the model total shown above.

    HiBattery 4020 X and 4020 AC profiles use a symmetric `2,500 W` integration ceiling even where larger expansion stacks may support more. Higher-power operation remains outside the current integration scope. A lower retained MQTT `min`/`max` envelope always wins.

    Hoymiles MQTT does not expose writable SOC cutoffs or individual cell voltages. Omnibattery enforces SOC limits in software, and Marstek's cell-balance and voltage-taper features are unavailable.

    Omnibattery publishes `mqtt_ctrl` plus the signed target, then refreshes the exact command every `30 s`. A failed refresh retries after `5 s`. On unload it sends idle and restores the device's general mode.

    Existing entries from the former MS-A2-only flow are corrected during reconfiguration when discovery identifies a different model. Only old MS-A2 defaults are replaced; user-adjusted values are retained.

    Manufacturer references:

    - [Hoymiles MQTT protocol guide](https://www.hoymiles.com/uploadfile/1/202511/9350aa1077.txt)
    - [HiBattery 1920 AC](https://www.hoymiles.com/products/hibattery-1920-ac.html)
    - [HiBattery 4020 X datasheet](https://www.hoymiles.com/uploadfile/1/202606/95d670b3a3.pdf)
    - [HiBattery 4020 X user manual](https://www.hoymiles.com/downloads/user-manual-hb-4020-x-global-en-de-fr-nl.html)
    - [HiBattery 4020 AC](https://www.hoymiles.com/products/hibattery-4020-ac.html)
    - [HiBattery 4020 AC user manual](https://www.hoymiles.com/downloads/user-manual-hb-4020-ac-global-en-de-fr-nl.html)

    For shared runtime controls and system limits, see [Choose your battery connection](index.md).
