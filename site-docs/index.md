![Omnibattery](assets/logo-github.png){ width="420" }

**Omnibattery** is a custom Home Assistant integration for monitoring and controlling pluggable solar batteries from several brands. Current supported hardware includes:

- **Marstek** Venus E/C (v2/v3), Venus A and Venus D via Modbus TCP, Modbus RTU or a LilyGo RS485/ESPHome bridge.
- **Zendure** SolarFlow 4000 Mix Pro, 4000 Mix AC+, 2400 AC+, 2400 AC Pro, 1600 AC+, 800 Pro, 800 Plus and 800 via local HTTP.
- **Anker SOLIX** Solarbank Max AC and Solarbank 4 E5000 Pro via Modbus TCP.
- **Sessy** Home Battery via its local dongle API (testers welcome).
- **Hoymiles** MS-A2 via the MQTT integration configured in Home Assistant.

<div class="grid cards" markdown>

-   :material-battery-charging: **Dynamic power control**

    Event-driven PD controller that keeps grid exchange near its target, with one-click tuning profiles and a quality sensor to help find a stable response.

-   :material-calendar-clock: **Predictive charging**

    Charges from the grid only when solar and stored energy are not enough, with time-slot, dynamic-pricing and real-time-pricing modes.

-   :material-battery-sync: **Multi-battery**

    Coordinates up to 10 batteries with SOC priorities, energy hysteresis and efficiency-aware power sharing.

-   :material-brand_family: **Multi-brand**

    Combine Marstek, Zendure, Anker SOLIX, Sessy and Hoymiles batteries in one installation while sharing the same control loop, system entities and energy-management features.

-   :material-view-dashboard: **Integrated dashboard**

    Built-in Home Assistant sidebar panel with a power-flow diagram, history charts, battery health and all control settings in one place — no extra HACS card or YAML required.

-   :material-tune: **Highly configurable**

    Adjust time slots, SOC and power limits, peak shaving, weekly full charge, solar-charge delay and excluded loads from Home Assistant.

</div>

## Built-in control dashboard

The panel installs automatically as a Home Assistant sidebar panel — no extra HACS card or YAML configuration is required. It provides three tabs:

- **Overview** with animated SOC ring, Grid↔Home↔Battery↔Solar energy-flow diagram, diagnostics, 2×2 chart grid and a measured/projected daily operation timeline
- **Batteries** with per-battery SOC/power, health & cells, daily energy, optional MPPT, firmware info, controls
- **Control** with system-wide settings grouped by feature, each with its switch + config parameters

![Dashboard](/assets/MVEM%20-%20Dashboard.gif)

## Key features

- **PD Controller (Zero Export/Import)**: adjusts battery power in real time to keep grid exchange close to zero.
- **One-click PD profiles and control-quality sensor**: select a response from Very smooth to Very aggressive, then use the quality verdict to see whether regulation is stable, oscillating or sluggish.
- **No-PD direct-tracking mode** (opt-in): the battery follows the consumption sensor 1:1 in a single cycle — no integral, derivative, smoothing or rate limiter — for installations that prefer raw tracking over the PD control law.
- **Multi-brand support**: combine compatible Marstek, Zendure, Anker SOLIX, Sessy and Hoymiles batteries in the same installation.
- **Predictive charging**: three modes (time slot, dynamic pricing, real-time price — including Tibber) that charge from the grid only when the energy balance requires it. Uses a 7-day rolling average of real household consumption to decide whether grid charging is needed.
- **Multi-battery management**: smart selection with SOC priorities, energy hysteresis and efficiency zone operation.
- **Time slots**: independently control charge and discharge windows, with per-slot SOC and power parameters.
- **Peak shaving**: reserves battery capacity to cover demand spikes above a configurable power threshold.
- **Weekly full charge**: charges to 100% once a week for cell balancing.
- **Cell balance monitor**: measures the voltage spread between the strongest and weakest cell after each full charge; tracks imbalance trends over time, sends alerts for moderate or high imbalance, and blocks discharge during the open-circuit voltage rest period.
- **Solar charge delay**: postpones morning battery charging (both solar and grid) while expected solar production is enough to cover the remaining energy needed.
- **Hourly net balance**: adjusts the PD setpoint continuously to keep hourly net grid energy at a configurable target (default: net zero per hour). Supports external net balance sensors and composes cleanly with all other features via the setpoint registry.
- **Load exclusion**: exclude high-power devices (e.g. EV chargers) so the controller does not try to compensate their consumption. Each excluded device has an individual exclusion percentage slider (0–100%).
- **Proactive alarm notifications (Marstek v2 batteries only)**: monitors battery fault and alarm registers every 5 seconds and sends a Home Assistant notification the moment a new condition is detected, with the exact fault or alarm name. A system-level `System Alarm Status` sensor (`OK` / `Warning` / `Fault`) provides an at-a-glance view across all batteries.

## Disclaimer

!!! danger "Liability disclaimer"
    This software is provided "as is", without warranty of any kind. Use is at your own risk. The developer assumes no responsibility for damage to batteries, inverters, electrical installations, financial losses or personal injury.

    **If you do not agree to these terms, DO NOT install or use this integration.**

## Support

If you find this integration useful, you can support the project:

<a href="https://buymeacoffee.com/ffunes" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="40" width="145"></a>
