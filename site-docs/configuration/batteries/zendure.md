# Zendure SolarFlow

Omnibattery connects to Zendure SolarFlow devices through their local HTTP API.
The wizard probes the device and detects the model automatically; you do not
need to select a model manually.

!!! warning "Disable HEMS"
    Keep **HEMS disabled** in the Zendure app while Omnibattery is controlling the device. HEMS overrides manual power setpoints after a few seconds.

## Connection

Enter a descriptive name, the device's local IP address and its HTTP port. The
default port is `80`. The device must be reachable from Home Assistant on the
local network or through routing.

| Field | Description | Default |
|---|---|---|
| **Name** | Name used for the battery device | — |
| **Host IP** | Local IP address of the SolarFlow | — |
| **HTTP port** | Local API port | `80` |

The connection test reads `/properties/report`, verifies the device and seeds
the model-specific power limits.

## Supported models and power envelopes

| Model | Maximum AC charge | Maximum AC discharge |
|---|---:|---:|
| SolarFlow 800 / 800 Plus / 800 Pro | `1000 W` | `800 W` |
| SolarFlow 1600 AC+ | `1600 W` | `1600 W` |
| SolarFlow 2400 AC Pro / 2400 AC+ | `2400 W` | `2400 W` |
| SolarFlow 4000 Mix AC+ | `4000 W` | `4000 W` |
| SolarFlow 4000 Mix Pro | `4000 W` | `4000 W` |

The device report remains authoritative if it announces a lower limit. The
The 4000 Mix Pro exposes dual DC MPPT telemetry; the AC-coupled 1600 AC+,
2400 AC+ and 4000 Mix AC+ models do not expose DC MPPT telemetry through this
connection.

Existing 2400 AC+ entries are promoted automatically when the device reports
the 4000 Mix AC+ or 4000 Mix Pro product identifier. The saved user power
ceilings are retained; raise them in the battery options if you want to use the
larger envelope.

## Zendure-specific settings

The common limits page includes charge/discharge power, maximum SOC, minimum
SOC, charge hysteresis and the backup offgrid threshold. Zendure uses a minimum
SOC range of 5–50% and does not use Marstek's cell-voltage taper.

Nominal capacity is optional. Enter it when you want Omnibattery to calculate
stored energy and efficiency from SOC; Zendure does not provide a nominal
capacity counter in its report.

### Manual control

Zendure has no native force-mode or charge/discharge setpoint entities in this
API. Omnibattery therefore provides software `Force Mode`, `Set Charge Power`
and `Set Discharge Power` controls. Select the per-battery **Battery Manual
Control** switch before using them; the controller re-applies a non-idle
software setpoint on each cycle while that switch is enabled. Keep HEMS off or
the Zendure app may override the command.

For the common runtime controls and system limits, see [Battery configuration](index.md).
