# Add a battery driver

This guide takes a new battery integration from vendor evidence to a reviewable Omnibattery pull request. A driver may use registers, a local application programming interface (API), Message Queuing Telemetry Transport (MQTT), or Home Assistant entities, but it must expose the same semantic battery behavior through `drivers/base.py::BatteryDriver`.

Start with evidence from real hardware. A protocol document alone cannot establish sign, scaling, command persistence, latency, or safe behavior after a partial write.

## Decide whether the device is suitable

Use these requirement levels while assessing the device:

| Code | Meaning |
|---|---|
| **B** | Blocking. Do not enable bidirectional automatic control without it. |
| **R** | Required for robust production support. Document any provisional mitigation. |
| **O** | Optional. Its absence removes a feature or entity, not core control. |

Record where each value comes from:

| Code | Source |
|---|---|
| **N** | Native device value or control. |
| **D** | Derived by the driver from validated native data. |
| **C** | User-configured or validated model constant. |
| **X** | Unsupported; omit or disable the dependent entity or feature. |

A device is:

- **SUITABLE** when every B and R requirement is covered.
- **SUITABLE WITH LIMITATIONS** when every B requirement is covered but an R or O item is missing, with affected features and risks documented.
- **NOT SUITABLE** when a B item is missing, command semantics cannot be confirmed, or control depends on an unstable or unauthorized interface.

### Automatic-control admission gate

Every item in this list is blocking:

- [ ] A programmable transport supports controlled connect, reconnect, and close.
- [ ] Fresh battery state of charge (SOC) is available as a percentage.
- [ ] Measured battery power is available directly or can be derived from simultaneous measurements.
- [ ] The device accepts power-limited charging and discharging commands.
- [ ] The device accepts and holds a safe idle command (`0 W`).
- [ ] Safe per-device charge and discharge maxima are known.
- [ ] The manufacturer’s battery management system (BMS) protections remain active under external control.
- [ ] The write cadence does not wear flash or violate API limits.
- [ ] Stale or lost communication can be detected without replaying old values indefinitely.

Without SOC, measured power, either control direction, or reliable idle, the device is not suitable for bidirectional automatic control. Monitoring-only support can be proposed separately, but it is not a full battery driver.

## Gather vendor and hardware evidence

Open an issue or engineering note with one assessment per manufacturer, model, and firmware family. Fill in this table before coding:

| Field | Evidence |
|---|---|
| Manufacturer and commercial model | `...` |
| Device-reported model | `...` |
| Firmware versions tested | `...` |
| Region or hardware variant | `...` |
| Rated capacity and charge/discharge power | `...` |
| AC-coupled, DC-coupled, or hybrid topology | `...` |
| Official document, revision, date, and link | `...` |
| Manufacturer authorization or support contact | `...` |
| Hardware used for validation | `...` |
| Test date | `...` |

Collect enough detail to answer all of these questions:

- Which models and firmware versions use the same protocol and field layout?
- Is access local, cloud-based, or both? What happens without internet access?
- How are authentication, token renewal, transport encryption, and certificate validation handled?
- What device, unit, endpoint, or topic identifies one physical battery?
- What are the timeout, retry, concurrency, connection, request-size, and rate limits?
- Does telemetry carry a timestamp, sequence number, or time-to-live?
- For every field, what are its type, byte order, unit, scale, sign, valid range, and sentinel values?
- For every write, what are its range, step, persistence, acknowledgement, and error response?
- Are multi-write commands atomic? If not, what order and rollback reach a safe state?
- Does a command survive device restart, network loss, Home Assistant reload, and Omnibattery shutdown?
- Does frequent writing update volatile memory or persistent flash?

Keep redacted request/response captures and hardware observations. Never include credentials, tokens, network addresses, or complete serial numbers in tests, diagnostics, documentation, or pull requests.

### Firmware compatibility matrix

| Model | Firmware | Transport | Read | Write | Known differences | Hardware tested |
|---|---|---|---|---|---|---|
| `...` | `...` | `...` | `yes/no` | `yes/no` | `...` | `yes/no` |

A cloud-only API is not rejected automatically. Its latency, expiry, quotas, and outage behavior must still support safe idle and the required control cadence.

## Map the vendor protocol to Omnibattery

The driver translates protocol details into canonical logical keys and operations. Registers, endpoints, topics, service names, and proprietary modes must stay inside the driver or its transport client.

Use these conventions:

- Signed net power is positive while charging, negative while discharging, and zero while idle.
- `battery_power` is a physical measurement in the same sign convention, not the last command.
- Publish final units in W, kWh, %, V, and °C.
- Omit a failed or unavailable value. Never replace it with zero when zero is valid.
- Clamp commands to the declared device envelope.
- Return a coherent `SetpointResult` from every `apply_setpoint()` path, including failures and writes without immediate readback.
- Treat an intent cache as command history. It is not measured delivery.

### Required telemetry and controls

| Canonical key or operation | Level | Vendor requirement | Accepted substitute |
|---|---|---|---|
| `battery_soc` | B | Fresh real SOC as a percentage | A voltage estimate is not full support. |
| `battery_power` | B | Instantaneous power in both directions | D formula from validated simultaneous flows. |
| `apply_setpoint(+W)` | B | Power-limited charge | Mode plus limit, or one signed property. |
| `apply_setpoint(-W)` | B | Power-limited discharge | Mode plus limit, or one signed property. |
| `apply_setpoint(0)` and `standby()` | B | Held idle without autonomous import or export | Documented zero-limit and mode sequence. |
| Maximum power | B | Safe values per model or device | C values bounded by official maxima. |
| Availability and freshness | B | Error, timestamp, sequence, or equivalent | Driver cache-expiry timer. |
| Setpoint echo | R | Applied mode and limit | An intent cache may optimize writes but does not confirm delivery. |
| Actuator latency | R | Command-to-physical-response delay | Hardware measurement with a conservative margin. |
| Readback latency | R | Command-to-settled-telemetry delay | Reuse actuator latency only when testing proves they match. |
| Reliable minimum power | R | Sustainable non-zero minimum and command step | Validated per-model C constant. |

### Optional telemetry and feature degradation

| Canonical data | Enables | If absent |
|---|---|---|
| `battery_total_energy` | Stored energy, allocation, and predictive charging | Require configured nominal capacity. |
| Charge/discharge energy totals | Energy and efficiency entities | Integrate measured power and persist the result. |
| `max_cell_voltage`, `min_cell_voltage` | Top-of-charge behavior and balance monitoring | Disable voltage-dependent features. |
| `internal_temperature` | Thermal power limiting | Disable temperature limiting. |
| `inverter_state` | Standby and BMS-cutoff confirmation | Use measured power and omit dependent detection. |
| `ac_offgrid_power` | Backup-load exclusion | Disable automatic backup exclusion. |
| MPPT or aggregate solar power | DC production and solar calculations | Declare the applicable solar capabilities false. |
| Alarm or fault state | Alarm notifications | Omit the dependent sensor and notifier input. |
| Battery voltage | Diagnostics | Omit the entity. |
| Stable serial and firmware | Device identity and support | Use the best stable device key and omit unavailable entities. |
| Hardware SOC cutoff | Persistent autonomous limits | Let shared control enforce limits in software. |
| Writable hardware power cap | Persistent device configuration | Use a software cap without exposing a fake hardware control. |
| External-control gate | Enter and restore external control | Required only when setpoints depend on the gate. |
| Device AC-port delivery | Correct delivery checks with shared DC solar | Omit `ac_delivered_power`; shared code falls back only where valid. |

Unsupported features must be gated through capabilities, entity definitions, or configuration. Do not create entities or decisions from fabricated zeros.

## Implement the driver contract

Create `custom_components/omnibattery/drivers/<brand>.py` and subclass `BatteryDriver`. Use an existing driver with the closest transport as your starting point:

- `marstek.py`, `anker.py`, and `huawei.py` show polled Modbus designs.
- `zendure.py` and `sessy.py` show local HTTP designs.
- `esphome.py` and `hoymiles.py` show push-fed Home Assistant entity designs.

### Abstract members

Implement every abstract member in `drivers/base.py`:

| Member | Required behavior |
|---|---|
| `capabilities` | Return one immutable `DriverCapabilities` object for the connected model. |
| `connected` | Report whether the transport or upstream source is currently usable. |
| `connect()` | Establish or validate access; be safe to call again after a failure. |
| `close()` | Release sessions, subscriptions, clients, and single-connection resources. |
| `set_shutting_down(value)` | Suppress expected transport noise during unload. |
| `read_groups` | Group logical keys into schedulable units with valid cadence names. |
| `read_telemetry(keys)` | Return decoded logical values, honoring the optional key subset. |
| `apply_setpoint()` | Clamp, translate, write, optionally confirm, and return `SetpointResult`. |
| `write_control()` | Write a logical entity control or return `False` when unsupported. |
| `net_power_from_data()` | Reconstruct the echoed signed command or return `None` when incomplete. |
| `control_dependency_keys` | Name values that control needs even when entities are disabled. |

`BatteryDriver` also provides optional semantic hooks: `dc_coupled`, `model_label`, `serial`, `balance_dependency_keys`, `supplemental_discharge_dependency_keys`, `supplemental_discharge_power_w()`, and `dynamic_discharge_limit_w()`.

### Coordinator-called hooks

The coordinator also invokes these methods by convention. They are not currently abstract in `BatteryDriver`, so verify them explicitly during review:

| Hook | Behavior |
|---|---|
| `apply_config(max_soc_pct, min_soc_pct, max_charge_power_w, max_discharge_power_w)` | Apply supported setup values and deliberately skip inapplicable settings. |
| `standby()` | Leave the device at a safe idle state before transport close. |
| `set_charge_cutoff(soc_pct)` | Change a hardware cutoff when supported; otherwise return `False`. |
| `set_rs485_control(enable)` | Toggle the external-control gate when supported; otherwise return `False`. |
| `get_rs485_control()` | Confirm the gate state for drivers that declare `has_rs485_control=True`. |

Do not claim a capability when its corresponding hook cannot fulfill the contract.

### Declare every capability

Build `DriverCapabilities` with evidence for every field:

| Field | What to establish |
|---|---|
| `hardware_soc_cutoff` | Whether hardware enforces the whole user-visible SOC range. |
| `has_force_mode` | Whether a distinct forced mode is part of the command sequence. |
| `push_telemetry` | Whether `read_telemetry()` returns a push-fed cache. |
| `max_charge_power_w`, `max_discharge_power_w` | Safe inclusive envelope for this model. |
| `min_charge_power_w`, `min_discharge_power_w` | Lowest sustainable non-zero command, or zero when no floor exists. |
| `has_mppt_pv` | Whether separate maximum power point tracking channels exist. |
| `has_solar_telemetry` | Whether independent aggregate solar power exists. |
| `has_alarm_registers` | Whether native alarm or fault status is exposed. |
| `has_rs485_control` | Whether an external-control gate can be toggled and confirmed. |
| `has_energy_counters` | Whether cumulative energy counters are native. |
| `has_daily_energy_counters` | Whether native counters reset daily. |
| `has_nominal_capacity` | Whether nominal capacity is reported by hardware. |
| `cycles_from_discharge_only` | Whether cycle calculation should use discharged energy only. |
| `setpoint_confirm_reliable` | Whether immediate command readback is trustworthy. |
| `actuator_latency_s` | Conservative measured physical response time. |
| `readback_latency_s` | Worst-case time before telemetry settles, or `None` to reuse actuator latency. |
| `engage_grace_s` | Extra idle-to-active allowance, or `None` for the controller default. |
| `telemetry_liveness_checked` | Whether a cache read proves that fresh push data resumed. |
| `charge_cutoff_range`, `discharge_cutoff_range` | Values the hardware write path really accepts. |

Defaults in the dataclass are compatibility behavior for existing drivers. A new driver should set fields intentionally and explain model-dependent values in comments and tests.

### Define entities beside the decoder

Expose these driver properties even when a platform has no native definitions:

```python
sensor_definitions
number_definitions
select_definitions
switch_definitions
binary_sensor_definitions
button_definitions
all_definitions
```

Each definition uses canonical keys and includes the metadata consumed by its Home Assistant platform, such as unit, device class, state class, scale, precision, polling cadence, category, and default enablement. Seed definitions during `__init__`; `connect()` may refine them after model or pack discovery. A battery that starts unreachable still needs enough definitions for entities to subscribe and trigger later recovery.

Add visible names and descriptions to `custom_components/omnibattery/strings.json` and every file under `custom_components/omnibattery/translations/`. Reuse an existing canonical key and translation when the meaning and unit are identical.

## Wire the driver into setup

A driver file alone is not selectable support. Complete every integration point:

1. Export the class from `drivers/__init__.py` and add it to `__all__`.
2. Add the brand to both add-battery and edit-battery selectors in `config_flow.py`.
3. Add a brand-specific config-flow step that validates credentials or transport access on real hardware and stores only the fields needed at runtime.
4. Construct the driver in `MarstekVenusDataUpdateCoordinator.__init__()` and pass probed model limits or identity where needed.
5. Set software-control and software-limit flags from capabilities and actual hardware behavior; do not infer them only from the brand name.
6. Expose only supported entity definitions, then add all translation keys.
7. Add any user-configured C values, such as nominal capacity, with validation and clear labels.
8. Check setup while the battery is reachable and unreachable, then check reload, reconnect, and removal.
9. Verify the new device in a mixed-brand fleet so selection, allocation, manual ownership, and shutdown do not depend on homogeneous drivers.

If setup requires a new library, document why it is needed, pin it according to repository policy, and include license and maintenance information in the pull request.

## Test the driver

Create `tests/test_<brand>_driver.py`. Use a fake transport, fake Home Assistant states, or a fake service layer so tests never contact real hardware. Existing files such as `test_huawei_driver.py`, `test_zendure_driver.py`, and `test_esphome_driver.py` show the expected seams.

### Driver tests

Cover these behaviors:

- construction and complete capability values;
- connection success, authentication failure, repeated connect, close, and reconnect;
- read-group composition and polling cadence;
- decoding, scaling, sign conversion, sentinels, partial replies, missing keys, and key-filtered reads;
- push-cache expiry and liveness checks where applicable;
- positive, negative, and zero setpoints;
- command clamping, minimum reliable power, and model-specific limits;
- charge-to-discharge, discharge-to-charge, active-to-idle, and idle-to-active transitions;
- write ordering and the safe result of each partial failure;
- immediate, delayed, absent, stale, and mismatched readback;
- `SetpointResult` fields and `net_power_from_data()`;
- `apply_config()`, `standby()`, cutoffs, external-control gates, and unsupported controls;
- model detection, firmware variants, entity-definition filtering, and control dependencies;
- derived telemetry formulas at sign and range boundaries;
- synthetic energy persistence when native counters are absent.

### Integration tests

Add or extend tests for:

- coordinator construction and capability forwarding;
- config-flow serialization and editing;
- entity definitions and translations;
- setup with an unreachable battery and later recovery;
- mixed-brand selection and power distribution;
- software SOC or power limits when hardware does not enforce them;
- shutdown reaching idle and restoring vendor control when applicable;
- optional features disappearing cleanly when their telemetry is unsupported.

Run the focused test first, then the complete unit suite:

```bash
python -m pytest tests/test_<brand>_driver.py
python -m pytest
```

Tests that request Home Assistant’s `hass` fixture need the Home Assistant pytest plugin enabled. Follow the separate command pattern in `.github/workflows/tests.yml` and add the new test file there if the default suite skips it:

```bash
python -m pytest -o addopts="" tests/test_<integration_flow>.py
```

Before opening the pull request, also build the documentation exactly as continuous integration does:

```bash
python -m mkdocs build --strict
```

## Validate on hardware

Unit tests prove translation logic; they do not prove the vendor’s behavior. Record a hardware test for every supported model and firmware family:

- [ ] Connect, read identity and SOC, and close without leaked resources.
- [ ] Recover after a timeout, device restart, Home Assistant reload, and temporary network loss.
- [ ] Reject malformed replies, sentinels, and out-of-range values.
- [ ] Confirm physical power sign during charge, discharge, and idle.
- [ ] Confirm command range, step, clamps, and stable minimum power.
- [ ] Measure normal and worst-case actuator and readback latency.
- [ ] Confirm repeated commands are idempotent and do not write persistent flash unnecessarily.
- [ ] Interrupt each stage of a multi-write sequence and verify the documented safe state.
- [ ] Confirm failed writes do not enter the cache as confirmed state.
- [ ] Exercise minimum and maximum SOC behavior with the BMS protections still active.
- [ ] Stop Omnibattery and verify `standby()` plus any vendor-control restoration.
- [ ] Run in a mixed-brand pool and verify that measured delivery matches allocation.

Retain redacted logs or traces that show the requested command, acknowledgement, and measured physical response. State clearly which matrix entries remain untested.

## What to include in the pull request

A reviewer should be able to judge the protocol, safety behavior, product surface, and test evidence without reconstructing your research. Include:

- supported manufacturer, models, regions, and tested firmware;
- official protocol source and authorization status;
- transport, authentication, discovery, and offline behavior;
- the telemetry and control mapping, including sign, scaling, units, sentinels, and persistence;
- every `DriverCapabilities` value with its evidence or rationale;
- command sequence, clamping, partial-failure behavior, and safe idle path;
- measured actuator and readback latency;
- unsupported features and how they are gated;
- all files added or changed across driver export, coordinator, config flow, entities, translations, tests, and user documentation;
- focused and full test commands with results;
- redacted hardware-test evidence and the exact models and firmware tested;
- known limitations, remaining risks, and explicit follow-up work.

Do not claim support based only on mocked tests. The brand should be selectable, setup should complete, supported entities should populate, automatic control should reach real hardware safely, and the documented test matrix should identify what was physically verified.

??? "Protocol assessment worksheets"
    Use these tables in the issue or pull request when the mapping is too large for the summary.

    **Transport and access**

    | Aspect | Value |
    |---|---|
    | Local, cloud, or both | `...` |
    | Protocol and version | `...` |
    | Address, endpoint, unit, or topic | `...` |
    | Discovery method | `...` |
    | Authentication and renewal | `...` |
    | Encryption and certificate validation | `...` |
    | Timeout and retry policy | `...` |
    | Simultaneous connection limit | `...` |
    | Read/write rate limit | `...` |
    | Multi-write ordering or atomicity | `...` |
    | Telemetry timestamp, sequence, or TTL | `...` |
    | Volatile versus persistent commands | `...` |
    | Offline behavior | `...` |

    **Telemetry mapping**

    | Omnibattery key | B/R/O | Vendor field | R/W | Type/order | Scale and unit | Range/sentinels | Cadence/TTL | N/D/C/X | Evidence | Tested |
    |---|---|---|---|---|---|---|---|---|---|---|
    | `battery_soc` | B | `...` | R | `...` | `... → %` | `...` | `...` | `...` | `...` | [ ] |
    | `battery_power` | B | `...` | R | `...` | `... → W` | `...` | `...` | `...` | `...` | [ ] |
    | Setpoint echo | R | `...` | R | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
    | `battery_total_energy` | R | `...` | R/C | `...` | `... → kWh` | `...` | `...` | `...` | `...` | [ ] |
    | Energy totals | O | `...` | R | `...` | `... → kWh` | `...` | `...` | `...` | `...` | [ ] |
    | Cell voltages | O | `...` | R | `...` | `... → V` | `...` | `...` | `...` | `...` | [ ] |
    | `internal_temperature` | O | `...` | R | `...` | `... → °C` | `...` | `...` | `...` | `...` | [ ] |
    | `inverter_state` | O | `...` | R | enum | `map: ...` | `...` | `...` | `...` | `...` | [ ] |
    | `ac_offgrid_power` | O | `...` | R | `...` | `... → W` | `...` | `...` | `...` | `...` | [ ] |
    | Alarms or faults | O | `...` | R | bitmap/enum | `map: ...` | `...` | `...` | `...` | `...` | [ ] |
    | Solar or MPPT | O | `...` | R | `...` | `... → W` | `...` | `...` | `...` | `...` | [ ] |
    | Identity and firmware | O | `...` | R | string | `...` | `...` | `...` | `...` | `...` | [ ] |

    **Control mapping**

    | Operation | B/R/O | Vendor command | Sequence | Range/step | Volatile/persistent | ACK/readback | Latency | Safe failure state | Evidence | Tested |
    |---|---|---|---|---|---|---|---|---|---|---|
    | Connect/authenticate | B | `...` | `...` | — | — | `...` | `...` | no control | `...` | [ ] |
    | Charge | B | `...` | `...` | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
    | Discharge | B | `...` | `...` | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
    | Idle | B | `...` | `...` | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
    | Power maxima/minima | R | `...` | `...` | `...` | `...` | `...` | `...` | C limit | `...` | [ ] |
    | SOC cutoff | O | `...` | `...` | `...` | `...` | `...` | `...` | software limit | `...` | [ ] |
    | Enable external control | Conditional | `...` | `...` | `...` | `...` | `...` | `...` | restore control | `...` | [ ] |
    | Restore vendor control | Conditional | `...` | `...` | — | `...` | `...` | `...` | `...` | `...` | [ ] |
    | Other entity controls | O | `...` | `...` | `...` | `...` | `...` | `...` | omit entity | `...` | [ ] |

??? "Existing adaptation examples"
    Marstek Venus E v3 demonstrates a register-backed implementation. It reads `battery_soc` from register `37005` and signed `battery_power` from register `30001`. Charging writes a charge limit and forced-charge mode; discharging writes a discharge limit and forced-discharge mode; idle writes both directional setpoints to zero and selects no forced direction. The directional setpoints expose a `0–2,500 W` range and `50 W` step. The driver declares a `0 W` minimum reliable operating power because the command registers accept values below the separate hardware power-cap choices.

    Zendure demonstrates valid substitutions for a property-based device:

    | Vendor difference | Driver adaptation |
    |---|---|
    | No direct `battery_power` | Derive output-pack power minus pack-input power after validating sign and simultaneity. |
    | No native energy counters | Integrate measured power and persist synthetic totals. |
    | No nominal capacity | Require user-configured `battery_total_energy`. |
    | No Marstek force mode | Translate net power into the vendor mode and input/output limits. |
    | Read-only hardware charge cap | Combine the device cap with a user software ceiling. |
    | Cells reported by pack | Derive global extremes and expose pack-specific keys only when useful. |
    | Delayed readback | Declare unreliable immediate confirmation and conservative timing. |
    | Persistent-write concern | Use volatile setpoints and reserve persistent writes for explicit configuration changes. |

    Accept a substitute only after validating its sign, timing, range, persistence, and failure behavior on hardware. Configured values must remain visibly configured values; do not present them as device telemetry.
