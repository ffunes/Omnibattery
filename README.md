# Omnibattery for Home Assistant

**Omnibattery** a comprehensive Home Assistant integration designed to monitor and control pluggable solar batteries:

 - Marstek Venus E and C (v2 and v3), Venus D and Venus A via Modbus TCP
 - Zendure Solarflow 2400 AC+, Solarflow 2400 Pro (Local API) 

 It provides advanced energy management features including predictive grid charging, customizable time slots for discharge control, and device load exclusion logic.

> [!CAUTION]
> **LIABILITY DISCLAIMER:**
> This software is provided "as is", without warranty of any kind, express or implied. By using this integration, you acknowledge and agree that:
> 1.  **Use is at your own risk.** The developer(s) assume **NO RESPONSIBILITY** or **LIABILITY** for any damage, loss, or harm resulting from the use of this software.
> 2.  This includes, but is not limited to: damage to your batteries, inverters, home appliances, electrical system, fire, financial loss, or personal injury.
> 3.  You are solely responsible for ensuring that your hardware is compatible and safely configured.
> 4.  Interacting with high-voltage battery systems and Modbus registers always carries inherent risks. Incorrect settings or commands could potentially damage hardware.
>
> **If you do not agree to these terms, DO NOT install or use this integration.**


![Dashboard](assets/MVEM%20-%20Dashboard.gif)

## Support

If you find this integration useful, you can support my work:

<a href="https://buymeacoffee.com/ffunes" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="40" width="145" ></a>
## Documentation

Full documentation (configuration, features, entities, troubleshooting) is available at:

**[https://ffunes.github.io/Marstek-Venus-Energy-Manager/](https://ffunes.github.io/Marstek-Venus-Energy-Manager/)**

## Key Features

- **Mix and match different battery brands**: Marstek, Zendure and more to come!
- **Zero Export/Import PD Controller**: Keeps grid exchange near zero using a Proportional-Derivative algorithm.
- **One-Click PD Profiles + Quality Sensor**: Pick a tuning profile (Very smooth → Very aggressive) instead of tuning gains by hand; a control-quality sensor reports whether the result is stable, oscillating or sluggish.
- **Multi-Battery Support**: Manage up to 6 batteries with intelligent load sharing and SOC-based priority.
- **Predictive Grid Charging**: Automatically charges from the grid when solar forecast + battery won't cover tomorrow's consumption. Supports fixed time slots, dynamic pricing, and real-time pricing modes. An optional grid-charge margin (%) tops up the grid amount to hedge optimistic solar forecasts.
- **Time Slots**: Per-battery windows with independent charge/discharge ticks, optional SOC and power overrides, and a manual mode that forces a fixed charge or discharge power. Up to 8 slots per integration.
- **Weekly Full Charge**: Forces 100% SOC once a week for LFP cell balancing.
- **Solar-Aware Charge Delay**: Holds back grid charging while solar can still cover the required energy.
- **Peak Shaving**: Reserves battery capacity to cover demand spikes above a configurable power threshold, keeping energy in reserve rather than covering all consumption.
- **Load Exclusion**: Mask high-power devices (e.g. EV chargers) so the battery doesn't try to cover them.

## Requirements

| Requirement | Details |
|---|---|
| Battery | Marstek Venus E v2/v3, Venus A, Venus D, Zendure Solarflow 2400 AC+, Solarflow 2400 Pro |
| Modbus bridge | Elfin-EW11 or compatible RS485-to-TCP converter. Venus E v3, Venus A and Venus D can also be connected via Ethernet with native Modbus TCP support. |
| Wireless connection | Required for Zendure Solarflow 2400 AC+ and Solarflow 2400 Pro |
| Grid sensor | HA sensor measuring total grid consumption (e.g. Shelly EM3, Neurio, smart meter) |
| Network | Battery reachable by IP from Home Assistant |
| Home Assistant | Recent version (tested on 2024.x+) |
| Solar forecast *(optional)* | Sensor providing tomorrow's production in kWh (Solcast, Forecast.Solar, …) |

## Installation

**HACS (Recommended)**

[![Open your Home Assistant instance and add a custom repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ffunes&repository=Omnibattery&category=integration)

Search for "Omnibattery", install, and restart Home Assistant.

**Manual**

Download the release zip, extract the `omnibattery` folder, copy it to your Home Assistant `custom_components` directory, and restart.

## Testbed Configuration

- **Batteries**: 1× Marstek Venus E v2, 1x Marstek Venus E v3 and 1x Zendure Solarflow 2400 AC+.
- **Connectivity**: Elfin-EW11 Modbus to WiFi converter.
- **Metering**: Shelly Pro 3EM Energy Meter.

## Acknowledgements

Special thanks to [ViperRNMC/marstek_venus_modbus](https://github.com/ViperRNMC/marstek_venus_modbus) for the Modbus register documentation that made this integration possible.
