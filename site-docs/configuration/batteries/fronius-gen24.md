# Fronius GEN24 with BYD Battery-Box

Omnibattery can monitor and control a BYD Battery-Box through the storage
interface of a Fronius GEN24 inverter. The driver uses local Modbus TCP for
control and fast telemetry, plus the inverter's local Solar API for BYD model,
serial number, temperature, voltage, current and capacity.

## Requirements

- A Fronius GEN24 inverter with compatible BYD storage
- Modbus TCP enabled on the inverter
- Storage control enabled on the inverter
- Network access from Home Assistant to TCP port `502` and the inverter's HTTP API

Select **Fronius GEN24 / BYD**, then enter the inverter host, Modbus port and
SunSpec unit ID. The defaults are port `502` and unit ID `1`.

## SunSpec model detection

The driver supports both Fronius **`float`** and **`int+SF`** SunSpec model
settings and detects the active layout automatically from the Basic Storage
Control Model (124) header. No model-type setting is required in Omnibattery.

Fronius applies the selected representation to the preceding inverter model.
Models 160 and 124 still use integer values and scale factors in both layouts,
but their addresses move by ten registers:

| Block | `float` | `int+SF` |
|---|---:|---:|
| Multiple MPPT Model 160 data | `40265` | `40255` |
| Basic Storage Model 124 data | `40355` | `40345` |

These positions follow the
[official Fronius GEN24 Modbus documentation](https://manuals.fronius.com/html/4204102649/en-US.html#BasicStorageControlsRegister).

All reads, setpoint writes and readbacks use the detected layout. The detected
type is shown as **SunSpec model** in the battery information box.

## SOC limits

`min_soc` and `max_soc` are enforced by Omnibattery's software control loop.
In particular, `max_soc` is **not a guaranteed hardware cutoff**: the GEN24 or
another automation may continue charging from PV and the BYD can therefore
rise above the configured value. The inverter and BMS remain responsible for
their hardware safety limits.

## Identity and persistence

The physical BYD serial number is read from
`GetStorageRealtimeData.cgi`. Omnibattery uses that serial for synthetic-energy
backup, so deleting and re-adding the battery can restore its accumulated
energy even when the inverter's address changes. Until the Solar API returns a
serial, no host-derived substitute is used.

## Setpoint acknowledgement

On the GEN24/BYD installation used to validate the driver, the written storage
control registers were readable after a `0.2 s` settle. Physical battery-power
response can lag the register acknowledgement; Omnibattery advertises a
`1.5 s` readback latency to its controller and continues checking measured
power on normal telemetry updates.
