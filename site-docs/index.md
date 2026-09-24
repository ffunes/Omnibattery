![Omnibattery](assets/logo-github.png){ width="420" }

# Use your home battery to reduce grid costs

Omnibattery connects compatible home batteries to Home Assistant so they can follow household demand, store available solar energy and charge when your schedule or electricity price makes it worthwhile.

## Is Omnibattery for me?

**Use it if** you already have a supported battery and a Home Assistant sensor that measures power imported from or exported to the grid. It is especially useful when you want one place to coordinate batteries, solar forecasts, electricity prices and household loads.

**You do not need it if** the manufacturer's own control already meets your needs, or if you only want to view battery data without Home Assistant adjusting battery power.

## Supported brands at a glance

| Brand | How Omnibattery connects | Start here |
|---|---|---|
| **Marstek Venus** | Modbus TCP, Modbus RTU or a LilyGo RS-485/ESPHome bridge | [Marstek setup](configuration/batteries/marstek.md) |
| **Zendure SolarFlow** | Local HTTP API | [Zendure setup](configuration/batteries/zendure.md) |
| **Anker SOLIX Solarbank** | Modbus TCP | [Anker SOLIX setup](configuration/batteries/anker.md) |
| **Huawei SUN2000 + LUNA2000** | Modbus TCP through the inverter | [Huawei setup](configuration/batteries/huawei.md) |
| **Sessy Home Battery** | Local HTTP API through the Sessy dongle | [Sessy setup](configuration/batteries/sessy.md) |
| **Hoymiles MS-A2 and HiBattery** | MQTT through Home Assistant | [Hoymiles MQTT setup](configuration/batteries/hoymiles.md) |

Omnibattery can coordinate supported brands in the same installation. Check the [installation requirements](installation.md#before-you-start) before changing any manufacturer settings.

## What it can do for you

<div class="grid cards" markdown>

-   :material-home-lightning-bolt: **Match battery power to home consumption**

    Reduce unwanted grid import or export as appliances turn on and off.

-   :material-white-balance-sunny: **Make better use of solar energy**

    Combine live production, battery capacity and forecasts to decide when energy should be stored.

-   :material-currency-eur: **Charge when grid energy makes sense**

    Use time windows or electricity prices to buy only the energy your home is expected to need.

-   :material-battery-sync: **Coordinate several batteries**

    Share demand across compatible batteries while respecting each battery's charge, discharge and state-of-charge limits.

</div>

## How to start

1. [Check the requirements and install Omnibattery](installation.md).
2. Add the integration and connect your grid sensor and battery.
3. Open the Omnibattery sidebar panel to confirm power and state of charge, then enable only the features that suit your home.

Already using **Marstek Venus Energy Manager**? Follow the [upgrade guide](upgrading-from-marstek-vem.md) so your settings, entity history and dashboards remain connected.

## Your control dashboard

The sidebar panel is installed with the integration; it does not require another Home Assistant Community Store (HACS) card or YAML dashboard configuration. Use **Overview** to follow live energy flow and daily history, **Batteries** to inspect each unit, and **Control** to enable and adjust optional features.

![Omnibattery dashboard showing home energy flow](assets/dashboard.gif)

??? "Advanced details"
    Omnibattery's proportional–derivative (PD) controller reacts when the grid sensor publishes a new value and adjusts battery power toward the configured grid target, including zero import or export. Tuning profiles from **Very smooth** to **Very aggressive** and the **PD Control Quality** sensor help identify a response that is stable, oscillating or slow. An optional direct-tracking mode follows the grid reading 1:1 in one control cycle, without integral, derivative, smoothing or rate-limit behavior.

    The integration can coordinate up to ten batteries. It uses state-of-charge priorities, energy hysteresis and efficiency-aware sharing while applying per-battery and system power limits. See [Multi-battery management](features/multi-battery.md).

    The dashboard's **Overview** tab includes an animated state-of-charge ring, a Grid↔Home↔Battery↔Solar flow diagram, diagnostics, history charts and a measured/projected daily timeline. **Batteries** shows each unit's power, state of charge, health, cells, daily energy, optional maximum power point tracking (MPPT) inputs, firmware and controls.

    Optional energy features include:

    - [Predictive charging](configuration/predictive-charging/index.md) using time slots, dynamic pricing or real-time prices, including Tibber; its demand estimate uses a seven-day rolling history of household consumption
    - [Time slots](configuration/time-slots.md) for independent charge and discharge windows, each with its own state-of-charge and power settings
    - [Capacity protection (peak shaving)](features/peak-shaving.md) to reserve energy for demand above a configured threshold
    - [Weekly full charge](features/weekly-full-charge.md), which can charge to 100% for balancing, and a [cell balance monitor](features/cell-balance-monitor.md) that records cell-voltage spread and protects the open-circuit rest period
    - [Solar charge delay](features/solar-charge-delay.md) when expected production can fill the battery later
    - [Hourly net balance](features/hourly-net-balance.md), which adjusts the PD target toward a configurable hourly grid-energy result and can use an external balance sensor
    - [Load exclusion](features/load-exclusion.md) for electric vehicle chargers and other large loads, with an individual exclusion setting from 0–100%
    - Proactive Home Assistant fault and alarm notifications when a battery driver provides that telemetry; **System Alarm Status** summarizes the fleet as `OK`, `Warning` or `Fault`

    State of charge (SOC) is the battery's remaining usable energy. The battery management system (BMS) applies the device's own cell and safety limits in addition to Omnibattery's software controls.

## Disclaimer

!!! danger "Liability disclaimer"
    This software is provided "as is", without warranty of any kind. Use is at your own risk. The developer assumes no responsibility for damage to batteries, inverters, electrical installations, financial losses or personal injury.

    **If you do not agree to these terms, do not install or use this integration.**

## Support

If you find this integration useful, you can support the project:

<a href="https://buymeacoffee.com/ffunes" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="40" width="145"></a>
