![Omnibattery](assets/logo-github.png){ width="420" }

**Omnibattery** is a custom Home Assistant integration to monitor and control pluggable solar batteries — Marstek Venus (E v2/v3, Venus A and Venus D series), Zendure SolarFlow (2400 AC+ / AC Pro), and Anker SOLIX Solarbank Max AC — via Modbus TCP or local HTTP.

<div class="grid cards" markdown>

-   :material-battery-charging: **Dynamic power control**

    PD controller that keeps grid flow near zero to maximise self-consumption.

-   :material-calendar-clock: **Predictive charging**

    Automatic grid charging when the solar forecast falls short of expected consumption.

-   :material-battery-sync: **Multi-battery**

    Intelligent management of up to 6 batteries with optimal power distribution.

-   :material-tune: **Highly configurable**

    Time slots, excluded devices, peak shaving, weekly full charge and more.

</div>

## Built-in control dashboard

Auto-installs as a HA sidebar panel — no HACS, no YAML.
Three tabs:
- **Overview** with animated SOC ring, Grid↔Home↔Battery↔Solar energy-flow diagram, diagnostics, 2×2 chart grid
- **Batteries** with per-battery SOC/power, health & cells, daily energy, optional MPPT, firmware info, controls
- **Control** with system-wide settings grouped by feature, each with its switch + config parameters

![Dashboard](/assets/MVEM%20-%20Dashboard.gif)

## Key features

- **PD Controller (Zero Export/Import)**: adjusts battery power in real time to keep grid exchange close to zero.
- **No-PD direct-tracking mode** (opt-in): the battery follows the consumption sensor 1:1 in a single cycle — no integral, derivative, smoothing or rate limiter — for installations that prefer raw tracking over the PD control law.
- **Predictive charging**: three modes (time slot, dynamic pricing, real-time price — including Tibber) that charge from the grid only when the energy balance requires it. Uses a 7-day rolling average of real household consumption to decide whether grid charging is needed.
- **Multi-battery management**: smart selection with SOC priorities, energy hysteresis and efficiency zone operation.
- **Discharge time slots**: define time windows and per-slot target grid power levels.
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
