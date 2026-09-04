# Anker SOLIX

Omnibattery supports the Anker SOLIX Solarbank Max AC and Solarbank 4 E5000 Pro
over Modbus TCP. The connection test reads the device's live hardware limits
and the model identifier.

!!! warning "Enable Third-Party Control"
    Enable **Third-Party Control** and Modbus TCP in the Anker app before adding the battery. Only one Modbus client can connect to the Solarbank at a time.

## Connection

Enter the battery name, local IP address, Modbus port and slave ID.

| Field | Description | Default |
|---|---|---|
| **Name** | Name used for the battery device | — |
| **Host IP** | Local IP address of the Solarbank | — |
| **Modbus port** | TCP port | `502` |
| **Modbus slave ID** | Unit ID used by the device | `1` |

The connection test probes the Modbus map before continuing. Keep the Anker app
closed or disconnected if it is already using the only available Modbus client
session.

## Power and SOC limits

Anker reports its charge and discharge ceilings, so the wizard does not ask for
manual power sliders during setup. Omnibattery uses those live hardware limits
and clamps them to a 3500 W software envelope.

The common limits page also includes:

- maximum SOC: 80–100% (default `100%`);
- minimum SOC: 0–20% (default `10%`);
- mandatory charge hysteresis (minimum 2%);
- backup offgrid threshold.

Anker does not expose Marstek's cell-voltage taper. For runtime SOC controls,
system caps and backup thresholds, see [Battery configuration](index.md).

### Manual control

Anker does not expose Marstek-style force-mode and power-setpoint entities.
Omnibattery stores the software `Force Mode`, `Set Charge Power` and `Set
Discharge Power` values and re-applies non-idle setpoints through the local
driver while **Battery Manual Control** is enabled. Keep **Third-Party Control**
enabled in the Anker app; the driver and BMS remain responsible for their own
hardware safety limits.

## Diagnostics

| Reading | Entity | Source |
|---|---|---|
| **State of health (SoH)** | `sensor.<battery>_battery_soh` | Modbus input register **10015** |

SoH is exposed for all supported Anker Solarbank models that share the common
register map. It has only been field-verified on **Solarbank Max AC** (product
code **DMWH**); other models may report the same register but are not yet
confirmed in the field.

Anker does not expose pack voltage, per-cell voltages or cell-balance telemetry
over Modbus. Omnibattery therefore does not create `battery_voltage`,
`max_cell_voltage`, `min_cell_voltage` or balance-monitor sensors for Anker
batteries.

### Dashboard

The Omnibattery battery card **Health & cells** section shows only metrics that
exist for each device:

- **Anker**: internal temperature and **State of health (SoH)** when available.
- **Marstek / Zendure / others**: temperature, voltage, cell min/max, cell
  delta and any other sensors exposed by the driver.

Voltage and cell rows are omitted automatically when the integration has no
matching entities, so Anker cards no longer show empty placeholders.
