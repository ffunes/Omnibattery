# Indevolt SolidFlex / PowerFlex

Omnibattery controls an Indevolt battery through the battery's local HTTP API. No cloud connection is required.

## Do I need it?

**Use it if** you own an Indevolt SolidFlex or PowerFlex battery and Home Assistant can reach it on your local network. The SolidFlex 2000 has been tested. The PowerFlex 2000, the SolidFlex and PowerFlex 3000 and the SolidFlex 1200 use the same API; please report your model and firmware if you use one of them.

**Use Omnibattery instead of the official Indevolt integration.** Both poll the same battery, and the battery answers requests one at a time. Omnibattery shows the same measurements, so disable the official integration for a battery that Omnibattery controls.

## Before you start

- In the Indevolt app, enable the **local API** with the **HTTP** protocol (not HTTP Digest), and give the battery a stable IP address.
- In the Indevolt app, set the working mode to **Realtime**. Omnibattery controls the battery only in Realtime mode and never switches the mode itself (see [why](#why-omnibattery-never-changes-battery-settings)).
- In the Indevolt app, set the battery **reserve** (backup SOC) you want. Omnibattery's minimum SOC should not be lower than this reserve.
- Disable the official Indevolt integration for this battery.

## How to add it

1. In the Omnibattery setup flow, choose **Indevolt SolidFlex / PowerFlex**.
2. Enter a descriptive **Name** and the battery's **IP address**.
3. Keep **HTTP port** at `8080`.
4. Omnibattery reads the battery's identity, working mode and power limits. It does not change anything on the battery.
5. If the battery is not in Realtime mode, a notice says so. Setup continues; control starts once you select Realtime in the app.
6. Choose the common power and state-of-charge limits.

## What you will see

- **Control starts by itself.** As soon as the battery is in Realtime mode, Omnibattery sends a stop command, checks that the battery is idle and then takes over. The same happens after every Home Assistant restart.
- **Control Status** shows what the driver is doing: `active`, `starting`, `realtime_mode_required`, `handover_failed`, `telemetry_stale` or `unknown_model`.
- **Switching modes in the app is safe.** If you select another working mode, Omnibattery pauses, shows a notification and leaves the battery alone. Switch back to Realtime and it resumes automatically.
- **Response time.** Omnibattery sends at most one request every 5 s, as Indevolt recommends. A new setpoint goes out in the next slot, and the battery follows within seconds.
- **Sensors:** state of charge, battery power, AC power, energy counters, working mode, battery reserve, power limits and firmware version. Temperatures: **Inverter Battery Temperature** for the battery built into the inverter unit, and **Expansion Battery N Temperature** for each expansion battery that is present; **Internal Temperature**, which Omnibattery's optional temperature charge limit uses to reduce charging when the battery is hot, is the warmest of those. A transformer temperature sensor is available but disabled by default.

## Why Omnibattery never changes battery settings

!!! info "Only realtime commands, never stored settings"
    A battery keeps two kinds of data. Its settings, such as the working mode, the reserve and the power limits, are stored in flash memory, so they survive a power cut. Flash wears out after a limited number of writes. Realtime commands are kept in working memory (RAM), which can be written as often as needed.

    A controller like Omnibattery may send a new command every few seconds, far more often than anyone changes a setting in an app. If those commands were written to flash, they could wear out the battery's controller long before the end of its normal life.

    We checked this with Indevolt's developers. They confirmed that only the realtime command (charge, discharge or stop, with a power and a target SOC) is kept in working memory, and that every other setting is stored in flash. Omnibattery therefore only ever writes the realtime command. Safeguards in the code and automated tests block any other write, so a future change cannot add one by accident.

    Change the working mode, the reserve and other settings in the Indevolt app.

Omnibattery enforces its own minimum and maximum SOC in software. Each command also carries a target SOC a few percent beyond the current charge in the commanded direction, which Omnibattery moves along as the battery charges or discharges. If Home Assistant stops in the middle of a command, the battery stops by itself within about 5% of state of charge.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The wizard cannot connect | The local API is off, or the address or port is wrong | Enable the local API in the Indevolt app and check the IP address and port `8080` |
| The wizard says the battery asks for a password | The local API is set to HTTP Digest | Select **HTTP** for the local API in the Indevolt app |
| Control Status is `realtime_mode_required` | The battery is in another working mode | Select **Realtime** in the Indevolt app; control resumes automatically |
| A notification says the battery is slow | Another client is polling it too, or the Wi-Fi link is weak | Disable the official Indevolt integration for this battery and check the signal |
| A notification says the reserve is above Omnibattery's minimum | The battery stops discharging at its own reserve first | Lower the reserve in the app, or raise Omnibattery's minimum SOC |
| Control Status is `unknown_model` | The battery reports a device type Omnibattery does not know yet | Open a GitHub issue with the device type from the notification |
| Control Status is `handover_failed` | The battery did not confirm idle after the stop command | Check the battery in the Indevolt app; Omnibattery retries automatically |

??? "Advanced details"
    All requests to one battery share a single budget: one request at a time, at least 5 s apart, measured from start to start. Omnibattery's own polling only reads a cache, so it never adds requests. A pending command takes the next slot. When the battery answers slowly or not at all, the interval grows to up to 20 s and returns to 5 s once replies are healthy again; a single timeout does not pause control.

    Telemetry is read in batches: power, AC power, working mode and SOC in every slot, the realtime status about every 30 s, limits every few minutes and energy counters every minute. If the battery ever returns an incomplete batch, Omnibattery reads smaller batches until complete replies return.

    The delivered AC power is the battery's total AC power minus the power on its bypass port, so a load or microinverter on that port is not counted as battery output.

    Indevolt's minimum firmware for the local API is CMS V1406.07.002E for the 2000 models, V1409.08.3034 for the 3000 family and V1407.07.202E for the SolidFlex 1200. Omnibattery shows a notification when the battery reports an older version.

    For shared runtime controls and system limits, see [Choose your battery connection](index.md).
