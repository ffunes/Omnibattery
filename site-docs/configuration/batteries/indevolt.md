# Indevolt SolidFlex / PowerFlex

Omnibattery controls an Indevolt battery through the battery's local HTTP API. No cloud connection is required.

## Do I need it?

**Use it if** you own an Indevolt SolidFlex or PowerFlex battery and Home Assistant can reach it on your local network. The SolidFlex 2000 has been tested. The PowerFlex 2000, the SolidFlex and PowerFlex 3000 and the SolidFlex 1200 use the same API; please report your model and firmware if you use one of them.

**Use Omnibattery instead of the official Indevolt integration.** Both poll the same battery, and the battery answers requests one at a time. Omnibattery shows the same measurements, so disable the official integration for a battery that Omnibattery controls.

## Before you start

- In the Indevolt app, enable the **local API** with the **HTTP** protocol (not HTTP Digest), and give the battery a stable IP address. Omnibattery does not use **Modbus TCP**, so you can leave it off. In tests, the battery answered Modbus requests much more slowly than HTTP requests, and the Modbus connection was less stable and needed frequent reconnects.
- In the Indevolt app, set the working mode to **Realtime**. Omnibattery controls the battery only in Realtime mode and never switches the mode itself (see [why](#why-omnibattery-writes-settings-only-when-you-change-them)).
- The battery's **reserve** (backup SOC), **max AC input** and **max AC output** are the same settings as Omnibattery's minimum SOC, max charge power and max discharge power. Omnibattery keeps them in sync, so you can change them in either place.
- Disable the official Indevolt integration for this battery.

## How to add it

1. In the Omnibattery setup flow, choose **Indevolt SolidFlex / PowerFlex**.
2. Enter a descriptive **Name** and the battery's **IP address**.
3. Keep **HTTP port** at `8080` and **Request speed** at **Conservative (5s)** (see [Choosing the request speed](#choosing-the-request-speed)).
4. Omnibattery reads the battery's identity, working mode and power limits. It does not change anything on the battery.
5. If the battery is not in Realtime mode, a notice says so. Setup continues; control starts once you select Realtime in the app.
6. Choose the common power and state-of-charge limits. The first time Omnibattery starts, it takes over the battery's own reserve and power settings instead of writing yours; change them afterwards in either place.

## What you will see

- **Control starts by itself.** As soon as the battery is in Realtime mode, Omnibattery sends a stop command, checks that the battery is idle and then takes over. The same happens after every Home Assistant restart.
- **Control Status** shows what the driver is doing: `active`, `starting`, `realtime_mode_required`, `handover_failed`, `telemetry_stale` or `unknown_model`.
- **Request Interval (Effective)**, **Read Latency (P95)**, **Write Latency (P95)** and **Request Failures** (diagnostic) show how the battery keeps up. **Request Speed** (diagnostic) shows the speed you chose. The interval normally matches it and grows when Omnibattery gives a slow battery more time; each latency is the slowest 5% (P95, so 95% of answers are faster) of the last 200 reads or writes, and shows how quickly the battery answers Omnibattery's requests, not how quickly it changes its power, which takes a few seconds (reads usually take tens of milliseconds, writes a few hundred); failures count timeouts and errors since Home Assistant started.
- **Switching modes in the app is safe.** If you select another working mode, Omnibattery pauses, shows a notification and leaves the battery alone. Switch back to Realtime and it resumes automatically.
- **Response time.** Omnibattery reads the battery once per interval of the request speed (5s by default, 2s at Maximum). A new setpoint goes out as soon as 2s have passed since the previous request (1s at Normal and Fast; at Maximum 0.2s after the previous reply, at most one setpoint per second), and the battery follows within seconds.
- **Settings:** **Discharge Cutoff** (the battery's reserve, which is Omnibattery's minimum SOC), **Max Charge Power** and **Max Discharge Power**. A change here is written to the battery; a change in the Indevolt app shows up here within a few minutes. The power sliders go up to the battery's hardware maximum. **Charge Cutoff** (maximum SOC) has no battery setting and is applied by Omnibattery only.
- **Sensors:** state of charge, battery power, AC power, energy counters, working mode and firmware version. Temperatures: **Inverter Battery Temperature** for the battery built into the inverter unit, and **Expansion Battery N Temperature** for each expansion battery that is present; **Internal Temperature**, which Omnibattery's optional temperature charge limit uses to reduce charging when the battery is hot, is the warmest of those. A transformer temperature sensor is available but disabled by default.

## Choosing the request speed

The **Request speed** sets how often Omnibattery talks to the battery. You choose it per battery when you add it, and change it later with **Reconfigure** on Omnibattery in **Settings → Devices & services**.

| Speed | Interval | When to use it |
|---|---|---|
| Maximum | 1s | Below Indevolt's documented minimum, at your own risk. Only useful with a grid meter that updates every second: a setpoint can go out every second, and a read follows a reply after 0.2s instead of waiting 1s. Reads stay at 2s; at most 1.5 requests per second. Set **PD Min Cycle Interval** to 0 as well (see the tip below). |
| Fast | 2s | A strong, stable connection, and you want the quickest reaction. It sends 50% more requests than Normal. |
| Normal | 3s | A grid meter that updates every 3s: quicker reactions than Conservative at a modest extra load. |
| Conservative (default) | 5s | Recommended by Indevolt, and enough for a grid meter that updates every 5s, the usual rate in Home Assistant. Also the choice on weak Wi-Fi, when other apps also poll the battery, or when Omnibattery reports the battery as slow. |

Everything faster than Indevolt's 5s is at your own risk, and you are responsible for checking that your battery, Home Assistant and network can keep up. On the battery's device page, **Request Interval (Effective)** should keep matching your choice, and **Read Latency (P95)** and **Write Latency (P95)** should stay well below 1s (reads usually take tens of milliseconds, writes a few hundred). If the interval keeps rising above your choice or a slow-battery notification appears, choose a slower speed. Feel free to try a faster speed: when the battery answers slowly or not at all, Omnibattery's Indevolt driver automatically steps back to a calmer pace until the response time has recovered.

A faster speed gets each new setpoint to the battery sooner, so it reacts sooner to meter changes, which can improve net zero. It does not make the battery's own ramp faster, and the battery refreshes its AC power only about every 6s. A speed faster than your grid meter gains nothing: with a meter that updates every 2s, choose Fast rather than Maximum. Apart from Maximum, Omnibattery never sends two requests closer than 1s apart, Indevolt's documented minimum (2s at Conservative). At Maximum it waits 0.2s after each reply instead, still with one request at a time, and steps back to Fast with the 1s spacing when the battery answers slowly. Whatever you choose, it slows down by itself when the battery answers slowly or not at all.

!!! tip "Only Indevolt batteries? Set PD Min Cycle Interval to 0"
    Omnibattery's **PD Min Cycle Interval** (default 1.0s) skips a meter update that arrives too soon after the previous control run, so a fast meter cannot flood a slow Modbus bridge, such as an Elfin EW11, with commands. The Indevolt driver already paces its own requests and only ever sends the newest setpoint, so for an Indevolt the interval protects nothing. It only adds delay: a skipped update waits for the next control run, up to 2s later.

    If every battery in Omnibattery is an Indevolt, set it to `0`: open the Omnibattery sidebar panel, select **Control**, and in **PD controller (advanced)** set **Min cycle interval** to 0. It takes effect at once. The battery gets no extra requests, because the request speed still sets the pace. Keep the default if Omnibattery also controls a battery behind a slow Modbus bridge. The other PD settings are explained in [Follow home consumption](../../features/pd-controller.md).

## Why Omnibattery writes settings only when you change them

!!! info "Realtime commands in working memory, settings only on request"
    A battery keeps two kinds of data. Its settings, such as the working mode, the reserve and the power limits, are stored in flash memory, so they survive a power cut. Flash wears out after a limited number of writes. Realtime commands are kept in working memory (RAM), which can be written as often as needed.

    What wears flash out is how often it is written. A controller like Omnibattery may send a new command every few seconds; if those commands went to flash, they could wear out the battery's controller long before the end of its normal life. A setting you change a few times a year costs nothing.

    We checked this with Indevolt's developers. They confirmed that only the realtime command (charge, discharge or stop, with a power and a target SOC) is kept in working memory, and that every other setting is stored in flash. So:

    - Control only ever writes the realtime command, however often it runs.
    - The reserve and the two power limits are written only when you change them, in Omnibattery's settings or on the device page. Omnibattery waits 3s for you to finish, skips the write when the battery already has the value, and accepts at most 10 changes per setting per 24h. Nothing is written at startup.
    - The working mode and every other setting are never written. Change them in the Indevolt app.

    Safeguards in the code and automated tests enforce this, so a future change cannot add a write by accident.

Omnibattery enforces its own minimum and maximum SOC in software. Each command also carries a target SOC a few percent beyond the current charge in the commanded direction, which Omnibattery moves along as the battery charges or discharges. If Home Assistant stops in the middle of a command, the battery stops by itself within about 5% of state of charge.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The wizard cannot connect | The local API is off, or the address or port is wrong | Enable the local API in the Indevolt app and check the IP address and port `8080` |
| The wizard says the battery asks for a password | The local API is set to HTTP Digest | Select **HTTP** for the local API in the Indevolt app |
| Control Status is `realtime_mode_required` | The battery is in another working mode | Select **Realtime** in the Indevolt app; control resumes automatically |
| A notification says the battery is slow, or Request Interval (Effective) stays above the chosen speed | Another client is polling it too, or the Wi-Fi link is weak | Disable the official Indevolt integration for this battery, check the signal, or choose the **Conservative (5s)** request speed |
| A notification says a setting was not applied | The battery did not accept the value | The battery keeps its value; try again later, or change it in the Indevolt app |
| A notification says a setting was changed too often | More than 10 changes to one setting in 24h, often from an automation | Wait a day, and check automations that set these numbers |
| Control Status is `unknown_model` | The battery reports a device type Omnibattery does not know yet | Open a GitHub issue with the device type from the notification |
| Control Status is `handover_failed` | The battery did not confirm idle after the stop command | Check the battery in the Indevolt app; Omnibattery retries automatically |

??? "Advanced details"
    All requests to one battery share a single budget: one request at a time, at least 1s apart (2s at Conservative and while slowed down to 5s or 10s), measured from start to start. Maximum measures from the previous reply instead: at least 0.2s after it, with commands at least 1s apart and reads every 2s. Reads come once per interval of the request speed. A pending command goes before a due read, at least that spacing after the previous request and at least one interval after the previous command. Omnibattery's own polling only reads a cache, so it never adds requests.

    When the battery struggles, the interval steps along a fixed ladder: 2s, 3s, 5s, 10s (from Maximum, the first step is Fast with the 1s spacing). Two bad replies within 30s (a timeout, an error, a read slower than 1s or a write slower than 2s) move it one step up, at most one step per 10s; a single bad reply is treated as jitter. After 30s without a bad reply it moves one step back down, never below the speed you chose. 10s is the last step: slower would only make control steer on older data for longer. A single timeout does not pause control.

    Telemetry is read in batches: power, AC power, working mode and SOC in every slot, the realtime status about every 30s, limits every few minutes and energy counters every minute. If the battery ever returns an incomplete batch, Omnibattery reads smaller batches until complete replies return.

    The delivered AC power is the battery's total AC power minus the power on its bypass port, so a load or microinverter on that port is not counted as battery output.

    Indevolt's minimum firmware for the local API is CMS V1406.07.002E for the 2000 models, V1409.08.3034 for the 3000 family and V1407.07.202E for the SolidFlex 1200. Omnibattery shows a notification when the battery reports an older version.

    For shared runtime controls and system limits, see [Choose your battery connection](index.md).
