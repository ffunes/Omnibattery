# Zendure SolarFlow

Omnibattery controls supported Zendure SolarFlow devices through their local network interface. It detects the model during setup and applies the corresponding power envelope.

## Do I need it?

| Supported model | Maximum AC charge | Maximum AC discharge | Solar input visible to Omnibattery |
|---|---:|---:|---|
| SolarFlow 800 / 800 Plus / 800 Pro | 1,000 W | 800 W | No dedicated MPPT telemetry |
| SolarFlow 1600 AC+ | 1,600 W | 1,600 W | No |
| SolarFlow 2400 AC Pro / 2400 AC+ | 2,400 W | 2,400 W | No dedicated MPPT telemetry |
| SolarFlow 4000 Mix AC+ | 4,000 W | 4,000 W | No |
| SolarFlow 4000 Mix Pro | 4,000 W | 4,000 W | Dual MPPT telemetry |

**Use it if** Home Assistant can reach the SolarFlow on the local network and you can leave Zendure's Home Energy Management System (HEMS) disabled. **Do not use both controllers at once:** HEMS overrides Omnibattery's command after a few seconds.

## Before you start

- Disable **HEMS** in the Zendure app.
- Give the device a stable local IP address and confirm Home Assistant can reach it.
- Note the local HTTP port; its default is `80`.
- Know the battery's nominal capacity, because the local report does not provide it.

## How to add it

1. In the Omnibattery setup flow, choose **Zendure SolarFlow**.
2. Enter a descriptive **Name** and the device's **Host IP**.
3. Keep **HTTP port** at `80` unless your network uses another port.
4. Wait while Omnibattery reads the device report and detects the model.
5. Enter nominal capacity and choose power and state-of-charge limits within the detected envelope.

## What you will see

Omnibattery provides automatic control plus software **Force Mode**, **Set Charge Power**, and **Set Discharge Power** controls. Turn on **Battery Manual Control** before using those manual values. The active command is sent through the local device interface; keep HEMS disabled so Zendure does not take control back.

State of charge (SOC), battery power, temperature, energy, and other readings appear when the model reports them. Only the SolarFlow 4000 Mix Pro supplies dedicated maximum power point tracking (MPPT) telemetry through this connection.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The wizard cannot connect | The address or HTTP port is wrong, or the local interface is unreachable | Test the device address from the Home Assistant network |
| Power changes briefly and returns to idle | HEMS is enabled | Disable HEMS in the Zendure app |
| The model or limit is lower than expected | The device report announced another product or a lower hardware cap | Check the product shown by Zendure and the reported charge limit |
| Manual values have no effect | The battery is still in the automatic pool | Turn on **Battery Manual Control** for that battery |
| Stored energy or efficiency is missing | Nominal capacity is absent or incorrect | Reconfigure the battery and enter its usable nominal capacity |

??? "Advanced details"
    Setup reads `/properties/report` and maps the reported product to a model profile. The report remains authoritative when it announces a lower charge limit than the profile.

    Zendure has no Marstek-style force-mode registers. For live control, Omnibattery writes charge or discharge mode and its limit with non-persistent control enabled, then refreshes the target during normal controller cycles. Configuration writes such as SOC limits use persistent mode.

    Existing SolarFlow 2400 AC+ entries can be promoted automatically when the device later reports a SolarFlow 4000 Mix product identifier. Saved user ceilings remain in place until you raise them in the battery options.

    The Zendure minimum SOC setting accepts `5–50%`. Nominal capacity accepts `0.01–100 kWh`. Zendure does not use Marstek's cell-voltage taper.

    For shared runtime controls and system limits, see [Choose your battery connection](index.md).
