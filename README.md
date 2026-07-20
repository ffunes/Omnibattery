# Omnibattery for Home Assistant

![Logo](assets/logo-github.png)

**Omnibattery** is your one stop Home Assistant integration designed to monitor and control pluggable solar batteries, as of today, the following ones are supported:

- Marstek Venus E and C (v2 and v3), Venus D and Venus A via Modbus TCP
- Zendure Solarflow 2400 AC+, Solarflow 2400 Pro (Local API)
- Anker SOLIX Solarbank Max AC via Modbus TCP

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

**[https://ffunes.github.io/Omnibattery/](https://ffunes.github.io/Omnibattery/)**

## Key Features

- **Mix and match different battery brands**: Marstek, Zendure and more to come!
- **Zero Export/Import PD Controller**: Keeps grid exchange near zero using a Proportional-Derivative algorithm.
- **Integrated dashboard**: All the controls and adjustments from a single place. Graphs and power flow diagram included!
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
| Battery | Marstek Venus E v2/v3, Venus A, Venus D, Zendure Solarflow 2400 AC+, Solarflow 2400 Pro, Anker SOLIX Solarbank Max AC |
| Modbus bridge | Elfin-EW11 or compatible RS485-to-TCP converter. Venus E v3, Venus A and Venus D can also be connected via Ethernet with native Modbus TCP support. Anker Solarbank Max AC uses native Modbus TCP (enable in the Anker app under Third-Party Control; only one Modbus client at a time). |
| Wireless connection | Required for Zendure Solarflow 2400 AC+ and Solarflow 2400 Pro |
| Grid sensor | HA sensor measuring total grid consumption (e.g. Shelly EM3, Neurio, smart meter) |
| Network | Battery reachable by IP from Home Assistant |
| Home Assistant | Recent version (tested on 2024.x+) |
| Solar forecast *(optional)* | Sensor providing today's production in kWh (Solcast, Forecast.Solar, …) |

## Installation

**HACS (Recommended)**

[![Open your Home Assistant instance and add a custom repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ffunes&repository=Omnibattery&category=integration)

Search for "Omnibattery", install, and restart Home Assistant.

**Manual**

Download the release zip, extract the `omnibattery` folder, copy it to your Home Assistant `custom_components` directory, and restart.

### Upgrading from Marstek Venus Energy Manager

Everything is preserved: all configuration (PD tuning, time slots, thresholds), entity IDs (`marstek_venus_*`), recorder history, long-term statistics, dashboards, automations, and daily energy counters.

> [!IMPORTANT]
> You must pass through **v2.0.6** of the old *Marstek Venus Energy Manager* integration before switching to Omnibattery. That release writes a configuration backup to HA's `.storage` that survives the domain switch. Skipping it means you may have to reconfigure everything from scratch if HACS deletes the old entries during the switch.

> [!TIP]
> Take a full Home Assistant backup before starting (**Settings → System → Backups → Create backup**).

**Step 1 — Write the backup while still on the old domain**

In HACS, update **Marstek Venus Energy Manager** to **v2.0.6** and restart Home Assistant. The integration loads and silently writes a backup of your entire configuration.

**Step 2 — Switch to Omnibattery**

Add the Omnibattery repository in HACS and install it, then restart.

**Step 3 — Run the migration**

After the restart, the old integration will appear broken or missing — this is expected. Go to:

**Settings → Devices & Services → Add Integration → Omnibattery**

The setup flow detects your existing configuration and presents a confirmation screen. Confirm to run the migration. It will:
- Recreate each config entry under the new `omnibattery` domain
- Repoint the entity registry without changing any entity ID or unique ID
- Copy the integration's `.storage` files (daily energy totals, accumulators, balance history)

**Step 4 — Hard-refresh the browser**

Press **Ctrl+F5** so the renamed sidebar panel loads correctly.

---

**Recovery: if you accidentally deleted the integration entirely**

If the old config entries were deleted from *Settings → Devices & Services* before migrating, the backup written in Step 1 still survives. Go to **Add Integration → Omnibattery** — the flow will detect no live legacy entries but will find the backup and offer to restore it. Confirm to recreate everything from the backup.

---

**Optional: rename system entities to `omnibattery_*`**

After migration, system entities keep their old `marstek_venus_system_*` IDs. If you want to rename them, go to **Settings → Devices & Services → Omnibattery → ⋯ → Recreate entity IDs**. This renames them in-place with history preserved, but any automations, templates, or Energy dashboard entries that reference the old IDs must be updated manually.

## Testbed Configuration

- **Batteries**: 1× Marstek Venus E v2, 1x Marstek Venus E v3 and 1x Zendure Solarflow 2400 AC+.
- **Connectivity**: Elfin-EW11 Modbus to WiFi converter.
- **Metering**: Shelly Pro 3EM Energy Meter.

## Acknowledgements

Special thanks to [ViperRNMC/marstek_venus_modbus](https://github.com/ViperRNMC/marstek_venus_modbus) for the Modbus register documentation that made this integration possible.
