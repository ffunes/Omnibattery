# Marstek Modbus register overview

This reference covers the Marstek Venus register-backed drivers used by Omnibattery. Other battery drivers use their own local API, MQTT, or Home Assistant entity contract and do not use this map.

!!! warning "Writing registers can interrupt normal control"
    Prefer Omnibattery entities and controls. Raw writes can conflict with automatic control, select an unsafe operating mode, or leave a stale setpoint active. Do not write an address unless you have verified the model, firmware family, data type, scale, and allowed value.

The [complete register table](registers.md) is an English-only technical reference.

## Firmware families

| Code in the complete table | Omnibattery hardware selection |
|---|---|
| `a` | Venus A |
| `d` | Venus D |
| `e_v12` | Venus E v2 |
| `e_v3` | Venus E v3 |

A blank cell means that Omnibattery does not define that key for the selected family. It does not prove that the physical address is unused by the firmware.

## How to read the table

- A Modbus register is two bytes. Multi-register values occupy consecutive addresses.
- Signed and unsigned types must be decoded differently.
- Apply the listed scale after decoding the raw value.
- Bit fields represent several independent flags.
- Calculated rows have no physical register.

| Type | Typical width | Meaning |
|---|---:|---|
| `uint16`, `int16` | one register | Unsigned or signed integer |
| `uint32`, `int32` | two registers | Unsigned or signed integer |
| `uint48` | three registers | Unsigned integer |
| `uint64` | four registers | Unsigned integer or bit field |
| `char` | variable | Text |
| `bit` | model-specific | Flags |

## Key registers used by control

| Purpose | Venus E v2 | Venus E v3 | Venus A | Venus D |
|---|---:|---:|---:|---:|
| Battery SOC | `32104` | `37005` | `32104` | `32104` |
| Battery power | `32102` | `30001` | `30001` | `30001` |
| RS-485 control | `42000` | `42000` | `42000` | `42000` |
| Force mode | `42010` | `42010` | `42010` | `42010` |
| Charge setpoint | `42020` | `42020` | `42020` | `42020` |
| Discharge setpoint | `42021` | `42021` | `42021` | `42021` |
| Hardware charging cutoff | `44000` | Not available | Not available | Not available |
| Hardware discharging cutoff | `44001` | Not available | Not available | Not available |
| Maximum charge power | `44002` | `44002` | `44002` | `44002` |
| Maximum discharge power | `44003` | `44003` | `44003` | `44003` |

Venus E v3, Venus A, and Venus D state-of-charge cutoffs are enforced in software because those firmware maps do not expose the v2 cutoff registers.

## Before diagnosing with raw registers

1. Confirm the hardware family selected in the config entry.
2. Compare the related Home Assistant entity with the row in the complete table.
3. Check whether the row is telemetry, configuration, or an immediate control command.
4. Download Omnibattery diagnostics before changing anything.
5. Use the [troubleshooting guide](../troubleshooting.md) and battery entities first.

??? "Transport timing"
    Omnibattery applies firmware-specific pacing and timeouts itself. Venus E v3, Venus A, and Venus D require a minimum of 150 ms between Modbus TCP messages; Venus E v2 requires 50 ms. External polling or writes on the same connection can delay replies and interfere with controller traffic. An RS-485 gateway uses a separate, shorter pacing profile from the battery's native Modbus TCP server.
