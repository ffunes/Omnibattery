# Battery driver requirements template

Use this template to audit a battery manufacturer's official development
documentation before implementing an Omnibattery driver. **Marstek Venus E v3**
is the functional reference, but another brand does not need the same registers
or modes. It must satisfy the same semantic contract: report the battery's real
state and accept a safe signed net-power command.

Complete one assessment per manufacturer, model and firmware family.

## Assessment outcome

Requirement levels:

| Code | Meaning |
|---|---|
| **B** | Blocking. Automatic control must not be enabled without it. |
| **R** | Required for a robust production integration. A provisional exception needs a documented mitigation. |
| **O** | Optional. Its absence removes specific entities or features, not core control. |

Source classifications:

| Code | Source |
|---|---|
| **N** | Native device value or control. |
| **D** | Derived by the driver from native data. |
| **C** | User-configured or validated model constant. |
| **X** | Unsupported; the dependent entity or feature is omitted. |

Final verdict:

- **SUITABLE**: every B and R requirement is covered.
- **SUITABLE WITH LIMITATIONS**: every B requirement is covered, but an R or O
  item is missing. Disabled features and residual risks are listed.
- **NOT SUITABLE**: a B item is missing, command semantics cannot be confirmed,
  or control depends on an unstable or unauthorised interface.

## 1. Device and documentation evidence

| Field | Value |
|---|---|
| Manufacturer | `...` |
| Commercial model | `...` |
| Device-reported model | `...` |
| Verified firmware range | `...` |
| Region/hardware variant | `...` |
| Rated capacity and power | `...` |
| Coupling type | `AC / DC / hybrid` |
| Official document, revision and date | `...` |
| URL or archived file | `...` |
| Manufacturer support channel | `...` |
| Hardware used for validation | `...` |
| Test date | `...` |

- [ ] The interface is published or authorised by the manufacturer.
- [ ] Applicable models and firmware versions are explicitly known.
- [ ] Real request/response examples have been retained without secrets.
- [ ] Every field documents type, unit, scale, sign, range and sentinels.
- [ ] Every write documents range, step, persistence and error response.
- [ ] Rate, concurrency and request-size limits are known.
- [ ] Restart, disconnect and Omnibattery shutdown behaviour is known.

### Firmware compatibility matrix

| Model | Firmware | Transport | Read | Write | Known differences | Status |
|---|---|---|---|---|---|---|
| `...` | `...` | `...` | `yes/no` | `yes/no` | `...` | `tested/untested` |

### Transport and access worksheet

| Aspect | Value |
|---|---|
| Scope | `local / cloud / both` |
| Protocol and version | `...` |
| Address, port, endpoint or topic | `...` |
| Discovery | `manual / mDNS / broadcast / cloud / ...` |
| Authentication and renewal | `...` |
| Encryption/TLS and certificate validation | `...` |
| Unit/device identifier | `...` |
| Recommended timeout and retry policy | `...` |
| Maximum simultaneous connections | `...` |
| Read/write rate limit | `...` |
| Ordering/atomicity of multi-write commands | `...` |
| Telemetry timestamp, sequence or TTL | `...` |
| Volatile versus persistent commands | `...` |
| Behaviour without network/cloud | `...` |

A cloud-only API is not automatically rejected, but its latency, token expiry,
quotas and outage behaviour must allow safe idle and stable control cadence.
These constraints are part of the verdict, not merely implementation detail.

## 2. Admission gate for automatic control

Every item below is blocking:

- [ ] A programmable transport supports controlled connect, reconnect and close.
- [ ] Real, fresh battery SOC can be read as a percentage.
- [ ] Real battery power is available directly or can be derived from simultaneous measurements.
- [ ] The device accepts power-limited charging commands.
- [ ] The device accepts power-limited discharging commands.
- [ ] The device accepts and holds a safe idle command (`0 W`).
- [ ] Safe per-unit charge and discharge maxima are known.
- [ ] The manufacturer's independent BMS protections remain active under external control.
- [ ] The required write cadence neither wears flash nor violates API limits.
- [ ] Stale or lost communications can be detected without reusing old data forever.

Without SOC, measured power, either direction, or reliable idle, the driver is
**NOT SUITABLE** for bidirectional automatic control. A monitoring-only mode may
still be considered but is not full support.

## 3. Omnibattery canonical contract

The driver translates vendor protocol details into
`drivers/base.py::BatteryDriver`; registers, endpoints, topics and proprietary
names must not leak into the control layer.

Mandatory conventions:

- Signed net power: `+W` charges, `-W` discharges, `0 W` idles.
- `battery_power` follows the same convention and is a measurement, not merely
  the last requested setpoint.
- Final units are W, kWh, %, V and °C.
- Failed values are omitted or unknown; never invent zero when zero is valid.
- `apply_setpoint()` clamps to the device envelope and always returns a coherent
  `SetpointResult`, even without immediate readback.

### Minimum driver surface

| Surface | Requirement | Level |
|---|---|---|
| Identity/capabilities | `capabilities`, `model_label`, and stable `serial` when available | R |
| Lifecycle | `connected`, `connect()`, `close()`, `set_shutting_down()` | B |
| Read | `read_groups`, `read_telemetry(keys)`; cache push data | B |
| Net control | `apply_setpoint(+W/-W/0)` | B |
| Entity controls | `write_control(key, value)`; return `False` for unsupported keys | R |
| Command echo | `net_power_from_data(data)`; `None` is allowed when no echo exists | R |
| Dependencies | `control_dependency_keys` for data polled even if its entity is disabled | R |
| Configuration | `apply_config(...)`, explicitly skipping inapplicable settings | R |
| Shutdown | `standby()` leaves the device safe before close | B |
| Charge cutoff | `set_charge_cutoff()` or controlled `False` for software enforcement | O/conditional |
| External-control gate | `set_rs485_control()`/`get_rs485_control()` or equivalent when required | B/conditional |

The coordinator currently calls some semantic methods that are not yet abstract
on the base class; a new driver must still implement them.

### Declared capabilities

| `DriverCapabilities` | Value | Evidence/rationale |
|---|---:|---|
| `hardware_soc_cutoff` | `...` | `...` |
| `has_force_mode` | `...` | `...` |
| `push_telemetry` | `...` | `...` |
| `max_charge_power_w` | `...` | `...` |
| `max_discharge_power_w` | `...` | `...` |
| `min_charge_power_w` | `...` | `...` |
| `min_discharge_power_w` | `...` | `...` |
| `has_mppt_pv` | `...` | `...` |
| `has_alarm_registers` | `...` | `...` |
| `has_rs485_control` | `...` | `...` |
| `has_energy_counters` | `...` | `...` |
| `setpoint_confirm_reliable` | `...` | `...` |
| `actuator_latency_s` | `...` | Worst case, not average |

## 4. Minimum telemetry and control levers

### Blocking core

| Omnibattery key/operation | Level | Vendor requirement | Accepted substitute |
|---|---|---|---|
| `battery_soc` | B | Fresh real SOC, 0–100% | A simple voltage estimate is not full support |
| `battery_power` | B | Instantaneous power in both directions | D formula from simultaneous validated flows |
| `apply_setpoint(+W)` | B | Power-limited charge | Mode + limit or one signed property |
| `apply_setpoint(-W)` | B | Power-limited discharge | Mode + limit or one signed property |
| `apply_setpoint(0)` / `standby()` | B | Held idle without autonomous import/export | Documented zero-limit/mode sequence |
| Maximum power | B | Per-model values or device readings | C values bounded by official maxima |
| Availability/freshness | B | Error, timestamp or equivalent | Driver cache expiry timer |
| Setpoint echo | R | Applied/accepted mode and limit | Intent cache only optimises; it is not measured power |
| Actuator latency | R | Write-to-application-to-telemetry delay | Hardware measurement with conservative margin |
| Reliable minimum power | R | Sustainable non-zero minimum and command step | Validated per-model C constant |

### Optional feature inputs

| Canonical key | Level | Enables | If absent |
|---|---|---|---|
| `battery_total_energy` | R | Stored energy, allocation and predictive charging | User-configured C capacity |
| Charge/discharge energy totals | O | Energy and cumulative efficiency | Integrate `battery_power` with persistence |
| `max_cell_voltage` | O | 100% taper/pause, recalibration and balance | Disable voltage-dependent features |
| `min_cell_voltage` | O | Cell delta and balance monitor | Do not expose delta/balance |
| `internal_temperature` | O | Thermal derating | Do not enable temperature limiting |
| `inverter_state` | O | Extra standby/BMS-cut confirmation | Use measured power only; omit dependent detections |
| `ac_offgrid_power` | O | Backup-load exclusion from PD | Disable automatic backup exclusion |
| Backup mode/control | O | Observe/control backup behaviour | Omit entity and specific logic |
| MPPT/PV power | O | DC production and plane efficiency | `has_mppt_pv=False` |
| Alarm/fault state | O | System alarm notifications | Omit dependent sensor/notifier |
| Battery voltage | O | Diagnostics | Omit entity |
| Identity, firmware, RSSI | O | Diagnostics/support | Omit entities; prefer stable serial when available |
| Hardware SOC cutoff | O | Autonomous persistent SOC limits | Software enforcement |
| Writable power cap | O | Persistent device configuration | Software cap without a fake hardware entity |
| External-control gate | Conditional | Enables/restores external control | Required only when setpoints depend on it |

## 5. Reference behaviour: Marstek Venus E v3

The v3 is a behavioural reference, not a required protocol shape:

| Semantics | v3 reference implementation |
|---|---|
| SOC | `battery_soc`, register `37005`, `uint16`, `%` |
| Measured power | `battery_power`, register `30001`, `int16`; positive charge, negative discharge |
| Charge | Discharge setpoint 0, charge setpoint W, force mode charge |
| Discharge | Discharge setpoint W, charge setpoint 0, force mode discharge |
| Idle | Both setpoints 0 and force mode stop |
| Power envelope | Setpoints 0–2500 W, documented 50 W step; installation caps are separate |
| Declared minimum | 800 W in v3 power-limit definitions |
| External control | RS485 control gate with device-specific commands |
| SOC cutoff | No v3 cutoff registers; Omnibattery enforces min/max SOC in software |
| Confirmation | Mode, setpoint and measured-power readback with ramp tolerance |
| Transport | Polled Modbus with a single TCP slot and model-specific pacing |

`force_mode` is therefore not mandatory. A signed limit, two directional limits
or different vendor enum may implement the same contract.

## 6. Accepted substitutions: the Zendure pattern

| Vendor difference | Valid driver adaptation |
|---|---|
| No direct `battery_power` | Derive `outputPackPower - packInputPower` after validating sign and simultaneity |
| No kWh counters | Integrate `battery_power` and persist synthetic totals |
| No nominal capacity | Let the user configure `battery_total_energy` |
| No Marstek force mode | Map net power to `acMode` plus input/output limits |
| Read-only charge cap | Combine the device cap with a user software ceiling |
| Cells reported per pack | Derive global extremes and optionally expose per-pack keys |
| Multi-second readback lag | Set unreliable confirmation and conservative actuator latency capabilities |
| Frequent writes may touch flash | Use volatile setpoints and explicit persistent configuration writes |

Accept a substitute only when its sign, timing, range and persistence have been
tested. Configured values must be labelled as configuration, not device
telemetry. Do not fabricate SOC, alarms, temperature, cell voltages or delivered
power without a reliable physical source; a command cache records intent only.

## 7. Feature degradation matrix

| Feature | Minimum dependencies | Alternative | Model status |
|---|---|---|---|
| PD charge/discharge | SOC, measured power, ±W/0 control, limits | None for full support | `...` |
| Multi-battery | Core per unit; capacity improves energy allocation | C capacity | `...` |
| Min/max SOC | SOC + idle command | Hardware or software | `...` |
| Predictive/pricing charge | SOC + kWh capacity + charge control | C capacity | `...` |
| Energy/cycles/efficiency | Counters or timestamped power | D integration | `...` |
| 100% taper/protection | Max cell voltage, SOC and power | No equivalent | `...` |
| Balance monitoring | Max + min cell voltage | D extremes from cells/packs | `...` |
| Weekly full charge | SOC, power and control; cutoff if available | Software cutoff | `...` |
| Thermal limit | Internal temperature | None | `...` |
| Backup exclusion | Off-grid power and backup state/mode | None reliable | `...` |
| MPPT/DC production | Per-channel or total DC power | D sum | `...` |
| Alarms | Official fault bits/codes | None | `...` |
| Synthetic-energy identity | Power + stable device ID | Less-stable device key | `...` |

Unsupported features must be gated by capabilities, entity definitions or
configuration. They must not receive fabricated zero values.

## 8. Telemetry mapping worksheet

| Omnibattery key | B/R/O | Vendor field/register/topic | R/W | Type/endian | Scale/final unit | Range/sentinels | Cadence/TTL | N/D/C/X | Evidence | Tested |
|---|---|---|---|---|---|---|---|---|---|---|
| `battery_soc` | B | `...` | R | `...` | `... → %` | `...` | `...` | `...` | `...` | [ ] |
| `battery_power` | B | `...` | R | `...` | `... → W; +charge/-discharge` | `...` | `...` | `...` | `...` | [ ] |
| Setpoint state/echo | R | `...` | R | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
| `battery_total_energy` | R | `...` | R/C | `...` | `... → kWh` | `...` | `...` | `...` | `...` | [ ] |
| Charge/discharge totals | O | `...` | R | `...` | `... → kWh` | `...` | `...` | `...` | `...` | [ ] |
| Max/min cell voltage | O | `...` | R | `...` | `... → V` | `...` | `...` | `...` | `...` | [ ] |
| `internal_temperature` | O | `...` | R | `...` | `... → °C` | `...` | `...` | `...` | `...` | [ ] |
| `inverter_state` | O | `...` | R | enum | `map: ...` | `...` | `...` | `...` | `...` | [ ] |
| `ac_offgrid_power` | O | `...` | R | `...` | `... → W` | `...` | `...` | `...` | `...` | [ ] |
| Alarms/faults | O | `...` | R | bitmap/enum | `map: ...` | `...` | `...` | `...` | `...` | [ ] |
| MPPT/PV | O | `...` | R | `...` | `... → W` | `...` | `...` | `...` | `...` | [ ] |
| Identity/firmware | O | `...` | R | string | `...` | `...` | `...` | `...` | `...` | [ ] |

## 9. Control mapping worksheet

| Semantic operation | B/R/O | Vendor fields/commands | Sequence | Range/step | Volatile/persistent | ACK/readback | Timeout/latency | Safe failure state | Evidence | Tested |
|---|---|---|---|---|---|---|---|---|---|---|
| Connect/authenticate | B | `...` | `...` | — | — | `...` | `...` | no control | `...` | [ ] |
| Charge at W | B | `...` | `...` | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
| Discharge at W | B | `...` | `...` | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
| Idle at 0 W | B | `...` | `...` | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
| Max charge/discharge | R | `...` | `...` | `...` | `...` | `...` | `...` | C limit | `...` | [ ] |
| Max/min SOC cutoff | O | `...` | `...` | `...` | `...` | `...` | `...` | software | `...` | [ ] |
| Enable external control | Cond. | `...` | `...` | `...` | `...` | `...` | `...` | restore control | `...` | [ ] |
| Restore vendor control | Cond. | `...` | `...` | — | `...` | `...` | `...` | `...` | `...` | [ ] |
| Other UI controls | O | `...` | `...` | `...` | `...` | `...` | `...` | omit entity | `...` | [ ] |

## 10. Minimum acceptance tests

Transport and data:

- [ ] Connect, read identity/SOC and close without leaking resources.
- [ ] Reconnect after timeout, device restart and temporary network loss.
- [ ] Reject partial replies, sentinels and out-of-range values.
- [ ] Expire push caches or last-known data when updates stop.
- [ ] Preserve units and sign in charge, discharge and idle.
- [ ] Respect mutual exclusion when the device allows one connection only.

Control:

- [ ] Positive, negative and zero setpoints work and clamp safely.
- [ ] Charge↔discharge and movement→idle transitions work.
- [ ] Repeated commands are idempotent and do not wear flash.
- [ ] A partially failed multi-write command converges to idle or another defined
  safe state, and required write ordering has been verified.
- [ ] Readback distinguishes accepted intent from delivered power.
- [ ] Normal and worst-case latency are measured; the conservative value is declared.
- [ ] Failed writes return a reason and do not update cache as confirmed.
- [ ] Max/min SOC cases remain safe.
- [ ] Shutdown calls `standby()` and restores vendor control when applicable.

Substitution and degradation:

- [ ] Every D formula has boundary and sign unit tests.
- [ ] Synthetic energy survives restart and skips telemetry gaps.
- [ ] C capacity is range-validated and labelled as configured.
- [ ] X features create no entities or decisions with fake values.
- [ ] The driver works in a mixed-brand battery pool.

Expected code coverage includes lifecycle, read groups, scaling, missing keys,
all setpoint paths, clamping, failures, delayed/no readback,
`net_power_from_data`, dependency keys, configuration, standby, model detection,
and the supported firmware matrix.

## 11. Copyable decision report

```text
Manufacturer/model:
Firmware tested:
Official documentation (revision/date/link):

Verdict: SUITABLE / SUITABLE WITH LIMITATIONS / NOT SUITABLE

Blocking items:
- Real SOC: N/D/C/X — evidence:
- Real power: N/D/C/X — evidence/formula:
- Adjustable charge: yes/no — range/step:
- Adjustable discharge: yes/no — range/step:
- Safe idle: yes/no — sequence:
- Safe limits: source/values:
- Freshness and connection loss: mechanism:

Omnibattery adaptations:
- Derived data:
- User-configured data:
- Software-enforced limits:

Disabled features:
-

Open risks:
-

Pending hardware tests:
-

Approval owner and date:
```

## 12. Post-approval implementation checklist

- [ ] Create the driver without leaking vendor details outside `drivers/`.
- [ ] Add brand/model selection and detection to the config flow.
- [ ] Instantiate the driver in the coordinator and declare capabilities.
- [ ] Define only supported entities and translations.
- [ ] Add configuration fields for C values such as nominal capacity.
- [ ] Capability-gate every feature that depends on an X key.
- [ ] Add driver unit tests and mixed-brand integration tests.
- [ ] Document device prerequisites, firmware and limitations.
- [ ] Redact credentials, tokens and sensitive serials from diagnostics.

Document approval allows implementation to start; it does not replace hardware
validation. Sign, scale, latency, internal clamps and firmware-specific behaviour
must all be verified before declaring stable support.
