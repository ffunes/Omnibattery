# Anker SOLIX

Omnibattery controls supported Anker SOLIX Solarbank batteries over the local Modbus TCP connection. The setup test reads the model and its live hardware limits before creating the battery.

## Do I need it?

| Supported model | Control connection | Independent solar telemetry |
|---|---|---|
| Solarbank Max AC | Modbus TCP | No; its solar registers are derived from AC measurements |
| Solarbank 4 E5000 Pro | Modbus TCP | Yes |

**Use it if** your supported Solarbank exposes Modbus TCP and the Anker app allows **Third-Party Control**. **Do not add another Modbus client:** the battery accepts only one client session at a time.

## Before you start

- Enable **Third-Party Control** and Modbus TCP in the Anker app.
- Close or disconnect any other application that uses the battery's Modbus connection.
- Give the Solarbank a stable local IP address.
- Note the Modbus slave ID.

## How to add it

1. In the Omnibattery setup flow, choose **Anker SOLIX Solarbank Max AC / 4 E5000 Pro**.
2. Enter a descriptive **Name** and the Solarbank's **Host IP**.
3. Keep **Modbus port** at `502` unless the device uses another port.
4. Enter the **Modbus slave ID**; its default is `1`.
5. Wait for the connection test, then choose state-of-charge and common safety settings.

## What you will see

Anker reports its own charge and discharge ceilings, so Omnibattery uses those detected values rather than asking for power limits during setup. Automatic control and software **Force Mode**, **Set Charge Power**, and **Set Discharge Power** controls are available. Turn on **Battery Manual Control** before sending manual targets, and keep **Third-Party Control** enabled in the app.

The common register map provides state of charge (SOC), battery power, temperature, energy, and state of health (SoH) when the device implements those readings. Anker does not expose pack voltage or individual cell voltages through this connection, so cell-balance and voltage-taper features are unavailable.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The wizard cannot connect | Third-Party Control is off, the address is wrong, or another client owns Modbus | Enable the app options, close the other client, and retry |
| The battery ignores a command | Third-Party Control was disabled after setup | Re-enable it in the Anker app |
| Power stops below the product rating | The live device ceiling or Omnibattery's safety envelope is lower | Check the detected charge and discharge limit entities |
| Solar power is absent on Max AC | This model does not provide an independent solar source through these registers | Use the installation's external solar sensor if needed |
| SoH is unavailable | The model returned an unsupported or zero value | Confirm the reading in diagnostics; zero is treated as unavailable |

??? "Advanced details"
    Omnibattery clamps Anker commands to the live hardware ceilings and to a `3,500 W` integration envelope. The minimum non-zero operating target is `100 W`; smaller targets are changed to idle or to that minimum as appropriate.

    The device's SOC controls allow a maximum of `80–100%` and a minimum of `0–20%`. Hardware cutoffs remain active. Anker does not use the Marstek cell-voltage taper.

    **Battery State of Health (SoH)** uses input register `10015`. The shared map makes it available to supported models, but it has been field-verified only on Solarbank Max AC product code `DMWH`. A raw value of `0` is treated as unavailable instead of 0% health.

    Solarbank 4 E5000 Pro product codes expose an independent solar source. Max AC solar fields are derived from the battery's own AC calculation and are excluded from Omnibattery's solar total.

    On the dashboard, the battery card's **Health & cells** section shows internal temperature and SoH when available. Voltage and cell rows are omitted when the driver has no matching entities, so Anker cards do not show empty placeholders.

    For shared runtime controls and system limits, see [Choose your battery connection](index.md).
