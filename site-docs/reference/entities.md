# Home Assistant entities

The integration automatically creates entities for each configured battery and aggregated sensors for the whole system.

The predictive-charging status binary sensor includes deadline-aware Dynamic Pricing diagnostics: `chronological_planning_active`, curve sources, `earliest_projected_depletion`, deadline/flexible kWh, shortfalls, cumulative `energy_deadlines`, and JSON-safe per-slot quota/deadline maps. These attributes describe planning intent; live battery and grid limits remain authoritative.

## Sensors (per battery)

| Entity | Description | Unit |
|---|---|---|
| `sensor.*_battery_soc` | State of charge | % |
| `sensor.*_battery_soh` | State of health (SoH); Anker Modbus input register **10015** | % |
| `sensor.*_battery_power` | Current power | W |
| `sensor.*_ac_power` | AC-side power; on Anker it is derived from battery power using the shared sign convention | W |
| `sensor.*_battery_voltage` | Voltage (not exposed on Anker) | V |
| `sensor.*_battery_current` | Current | A |
| `sensor.*_battery_temperature` | Temperature | °C |
| `sensor.*_internal_temperature` | Internal temperature used by thermal protection; on Anker it aliases `temperature` | °C |
| `sensor.*_total_charging_energy` | Total charging energy | kWh |
| `sensor.*_total_discharging_energy` | Total discharging energy | kWh |
| `sensor.*_total_daily_charging_energy` | Energy charged today (daily register on Marstek; derived from the cumulative counter on Anker; integrated on Zendure) | kWh |
| `sensor.*_total_daily_discharging_energy` | Energy discharged today (daily register on Marstek; derived from the cumulative counter on Anker; integrated on Zendure) | kWh |
| `sensor.*_battery_cycle_count` | Cycle count (register, v3/vA/vD) | — |
| `sensor.*_battery_cycle_count_calc` | Calculated cycle count (all versions) | — |
| `sensor.*_max_cell_voltage` | Max cell voltage (v3/vA/vD; not exposed on Anker) | V |
| `sensor.*_min_cell_voltage` | Min cell voltage (v3/vA/vD; not exposed on Anker) | V |
| `sensor.*_alarm_status` | Active alarm conditions (v2) — diagnostic | text |
| `sensor.*_fault_status` | Active fault conditions (v2) — diagnostic | text |

## Cell balance monitor sensors (per battery)

Only present when the [cell balance monitor](../features/cell-balance-monitor.md) is enabled in the weekly full charge configuration.

| Entity | Description | Unit |
|---|---|---|
| `sensor.*_cell_delta` | Voltage spread between max and min cell at last OCV reading | mV |
| `sensor.*_balance_status` | Balance result: `green` / `yellow` / `orange` / `red` | — |
| `sensor.*_delta_trend` | Trend over last formal readings: `rising` / `stable` / `falling` | — |
| `sensor.*_last_balance_read` | Timestamp of the last reading | timestamp |
| `sensor.*_delta_avg_4w` | Rolling average of the last 4 formal readings | mV |

## Device information sensors

| Entity | Description |
|---|---|
| `sensor.*_device_name` | Device name |
| `sensor.*_sn_code` | Serial number |
| `sensor.*_software_version` | Firmware version |
| `sensor.*_bms_version` | BMS version |
| `sensor.*_mac_address` | MAC address |
| `sensor.*_device_name` | Device name |
| `sensor.*_sn_code` | Serial number |
| `sensor.*_software_version` | Firmware version |
| `sensor.*_bms_version` | BMS version |
| `sensor.*_mac_address` | MAC address |

## Binary sensors

| Entity | Description |
|---|---|
| `binary_sensor.*_wifi_status` | WiFi status |
| `binary_sensor.*_cloud_status` | Cloud status |
| `binary_sensor.marstek_venus_system_predictive_charging_active` | Predictive charging active (system) |
| `binary_sensor.omnibattery_curtailment_status` | Smart pre-discharge / anti-curtailment status (Dynamic Pricing only) |
| `binary_sensor.*_wifi_status` | WiFi status |
| `binary_sensor.*_cloud_status` | Cloud status |
| `binary_sensor.marstek_venus_system_predictive_charging_active` | Predictive charging active (system) |

## Numbers (sliders)

| Entity | Description | Range |
|---|---|---|
| `number.*_max_soc` | Maximum SOC | 0–100 % |
| `number.*_min_soc` | Minimum SOC | 0–100 % |
| `number.*_max_charge_power` | Max charge power | W |
| `number.*_max_discharge_power` | Max discharge power | W |
| `number.marstek_venus_system_system_max_charge_power` | Optional combined charge cap for the whole system (`0 W` = disabled). Only created when system power limits are enabled. | Dynamic: configured charge-power sum |
| `number.marstek_venus_system_system_max_discharge_power` | Optional combined discharge cap for the whole system (`0 W` = disabled). Only created when system power limits are enabled. | Dynamic: configured discharge-power sum |
| `number.omnibattery_predictive_safety_margin_kwh` | Solar forecast buffer used by predictive charging and Dynamic Pricing anti-curtailment | 0–20 kWh |
| `number.omnibattery_negative_injection_threshold` | Inclusive price threshold for forecast negative-injection risk slots | -2–2 currency/kWh |
| `number.omnibattery_predischarge_reserve_soc` | Additional SOC floor for smart pre-discharge | 0–100 % |
| `number.omnibattery_predischarge_max_export_power_w` | Maximum grid export during smart pre-discharge (`0 W` = self-consumption only) | 0–10000 W |
| `number.*_max_soc` | Maximum SOC | 0–100 % |
| `number.*_min_soc` | Minimum SOC | 0–100 % |
| `number.*_max_charge_power` | Max charge power | W |
| `number.*_max_discharge_power` | Max discharge power | W |

## Selects

| Entity | Options |
|---|---|
| `select.*_force_mode` | None / Charge / Discharge |
| `select.marstek_venus_system_pd_tuning_profile` | Very smooth / Smooth / Balanced / Aggressive / Very aggressive / Custom — one-click PD presets that set `Kp`, `Kd` and the rate limit together (deadband stays user-owned) |

## Switches

| Entity | Description |
|---|---|
| `switch.*_rs485_control` | RS485 control mode |
| `switch.*_allow_charge` | Software control that allows this battery to participate in automatic charging |
| `switch.*_allow_discharge` | Software control that allows this battery to participate in automatic discharging |
| `switch.*_battery_manual_mode` | Excludes this battery from automatic power control while keeping its telemetry and physical power in system aggregates |
| `switch.*_backup_function` | Backup function — when enabled **and** AC offgrid power ≠ 0 W, the battery is excluded from PD control (no write commands sent) |
| `switch.marstek_venus_system_override_predictive_charging` | Override predictive charging |
| `switch.omnibattery_smart_predischarge` | Opt-in smart pre-discharge / anti-curtailment (Dynamic Pricing only) |
| `switch.omnibattery_negative_price_charging` | Opt-in opportunistic charging at negative import prices (Dynamic Pricing only) |

## Buttons

| Entity | Description |
|---|---|
| `button.*_reset` | Device reset |
| `button.omnibattery_reevaluate_dynamic_pricing` | Rebuild the Dynamic Pricing schedule now; only created in Dynamic Pricing mode |

## System sensors

### Daily operation timeline

`sensor.omnibattery_daily_operation_timeline` is a diagnostic-only, local-day
snapshot for the Overview card. Its state is the local date and its bounded
attributes contain `schema_version`, `timezone`, `interval_minutes` (15),
`interval_count` (96), `current_index`, `current_progress`, `mode`, freshness
and the `series`, `operations` and `sources` objects. The arrays are excluded
from Recorder. `actual_*` values are measured, while `planned_*` values are
informational projections and may be `null` when their source is stale.

The timeline preserves closed intervals across plan reevaluations and, after a
restart, restores only the current local day. `action_mask` values are
`solar_charge=1`, `grid_charge=2` and `discharge=4`; context masks identify
setpoint, Charge Delay and the predictive mode. `grid_charge_decision` is
independent of physical flow (`scheduled`, `not_needed`, `unknown` or
`not_applicable`).

See the [daily operation timeline guide](../features/daily-operation-timeline.md)
for the visual rules, DST handling and mobile interaction.

### Integration Status

`sensor.marstek_venus_system_integration_status` shows at a glance what the integration is currently doing. It reflects the highest-priority active mode:

| State | Description |
|---|---|
| `Charging from Grid` | Predictive grid charging is active |
| `Weekly Full Charge` | Charging to 100 % for cell balancing |
| `Charge Delayed` | Charging blocked, waiting for optimal time based on solar forecast |
| `Waiting for Solar` | Charge delay: waiting for solar production to start |
| `Charging to Setpoint` | Charge delay: charging to the configured minimum SOC |
| `Capacity Protection` | Discharge limited due to low SOC (peak shaving active) |
| `No-Discharge Window` | Inside a configured no-discharge time slot |
| `Charging` | Charging (solar surplus or other) |
| `Discharging` | Discharging to cover home consumption |
| `Standby` | System balanced within deadband, no action needed |
| `Manual Mode` | Manual mode active — integration sends no automatic commands |
| `Initializing` | First controller cycle not yet completed |

The sensor also exposes blocker diagnostics as attributes:

| Attribute | Description |
|---|---|
| `charge_blocked` | `true` when charge is effectively blocked system-wide, either by a global blocker or because every known battery is charge-blocked |
| `discharge_blocked` | `true` when discharge is effectively blocked system-wide, either by a global blocker or because every known battery is discharge-blocked |
| `charge_blockers` | Active system-wide charge blockers with reason, details, and timestamp |
| `discharge_blockers` | Active system-wide discharge blockers with reason, details, and timestamp |
| `battery_charge_blockers` | Active per-battery charge blockers grouped by battery, including manual allow-charge, maximum SOC, and charge hysteresis |
| `battery_discharge_blockers` | Active per-battery discharge blockers grouped by battery, including manual allow-discharge and minimum SOC |

### PD Control Quality

`sensor.marstek_venus_system_pd_control_quality` reports how well the PD controller holds the grid target, so the effect of a [tuning profile](../features/pd-controller.md#tuning-profiles) or slider change is visible. The state is a verdict:

| State | Meaning |
|---|---|
| `stable` | PD tracks the target well |
| `oscillating` | Hunting — use a smoother profile or raise the deadband |
| `sluggish` | Too slow — use a more aggressive profile |
| `battery_limited` | Battery full/empty or at its power rail; the PD cannot act (not a tuning issue) |
| `blocked` | The direction the grid error demands is not allowed (charge delay, time slot, price, EV pause); the PD is muzzled, not mistuned |
| `collecting_data` | Warming up, or the metric has not advanced for more than 5 min |

Attributes: `rms_error_w` (average grid-tracking error), `oscillation_per_min`, `metric_age_s` (seconds since the metric last advanced), the active `kp` / `kd` / `deadband_w` / `max_power_change_w`, and `active_profile`. The metric is a 60 s rolling average and is paused briefly after a target change and while battery-limited or blocked, so allow 1–2 min after a change.

### Aggregate sensors

Available under the `sensor.marstek_venus_system_*` prefix, summing values across all batteries:

- `system_battery_power` — Total system power
- `system_battery_soc` — System average SOC
- `system_total_charging_energy` — Total system charging energy
- `system_total_discharging_energy` — Total system discharging energy
- `grid_at_min_soc` — Grid import during min SOC periods (kWh)
- `system_alarm_status` — Aggregated alarm state across all batteries (`OK` / `Warning` / `Fault`); attributes list active conditions per battery
- `system_home_consumption` — Instantaneous home consumption (W). Reads the household sensor when configured, otherwise derives it from `grid + battery AC + solar`.
- `system_daily_home_energy` — Today's home consumption (kWh), integrated from the Home Consumption value above. Resets at midnight (local time).

### Expected home consumption profile

`sensor.omnibattery_expected_home_consumption_profile` is a diagnostic
sensor for the learned 28-day profile. Its state is today's forecast in kWh.
Attributes include `interval_profile_kwh`, `hourly_profile_kwh`, `target_date`,
`source`, `mature`, `coverage_ratio`, `weekday_samples`, `day_type_samples`,
`total_profile_days` and `newest_profile_date`. The bounded day-level summary is
available through the integration diagnostics endpoint.
The source is `profile` only when the maturity contract is satisfied;
`legacy_daily` identifies the fallback.

### Vacation Mode

`switch.omnibattery_vacation_mode` pauses consumption learning without pausing
physical metering or battery control. Its attributes report the active constant
baseline, its source, valid overnight samples and the persisted excluded
periods. During vacation the expected-profile sensor reports
`source: vacation_baseline`.

Predictive charging also reports `solar_timeline_source`,
`solar_remaining_raw_kwh`, `solar_remaining_effective_kwh`,
`solar_timeline_fallback_reason`, `solar_profile_mature`,
`solar_profile_days`, `solar_profile_coverage_ratio` and
`solar_profile_generation`. Integration diagnostics contain a bounded
`solar_profile` section with telemetry source, quality counters, generation,
backfill status and at most 24 summarized progress values.
- `system_battery_power` — Total system power
- `system_battery_soc` — System average SOC
- `system_total_charging_energy` — Total system charging energy
- `system_total_discharging_energy` — Total system discharging energy
- `grid_at_min_soc` — Grid import during min SOC periods (kWh)
- `system_alarm_status` — Aggregated alarm state across all batteries (`OK` / `Warning` / `Fault`); attributes list active conditions per battery
- `system_home_consumption` — Instantaneous home consumption (W). Reads the household sensor when configured, otherwise derives it from `grid + battery AC + solar`.
- `system_daily_home_energy` — Today's home consumption (kWh), integrated from the Home Consumption value above. Resets at midnight (local time).
