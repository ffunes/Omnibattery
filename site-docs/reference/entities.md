# Home Assistant entities

This reference lists the entity families Omnibattery can create. Your installation shows only the entities supported by its battery driver and the features you configured.

## Find the entity you need

1. Start with the everyday table.
2. Search the complete catalogue for the visible name or key.
3. Read **When it appears** before using an entity in an automation.

Home Assistant may preserve an older entity ID or add a suffix. Fresh system entities use `omnibattery_`; upgraded installations can retain `marstek_venus_system_` so history and automations keep working. Use the entity picker or entity registry instead of assuming an exact ID.

## Everyday entities

| Visible name | Typical fresh-install pattern | Use |
|---|---|---|
| **System SOC** | `sensor.omnibattery_system_soc` | Combined state of charge (SOC) |
| **System Charge Power** | `sensor.omnibattery_system_charge_power` | Total charging power |
| **System Discharge Power** | `sensor.omnibattery_system_discharge_power` | Total discharging power |
| **Home Consumption** | `sensor.omnibattery_home_consumption` | Current household demand |
| **Integration Status** | `sensor.omnibattery_integration_status` | Current control mode and blockers |
| **PD Control Quality** | `sensor.omnibattery_system_pd_control_quality` | Grid-tracking quality |
| **Active Batteries** | `sensor.omnibattery_active_batteries` | Batteries currently participating |
| **Non-Responsive Batteries** | `sensor.omnibattery_non_responsive_batteries` | Batteries temporarily removed from control |
| **Predictive Charging Active** | `binary_sensor.omnibattery_predictive_charging_active` | Predictive plan and charging state |
| **Allow Charge / Allow Discharge** | `switch.*_battery_allow_charge`, `switch.*_battery_allow_discharge` | Per-battery participation |
| **Manual Battery Control** | `switch.*_battery_manual_mode` | Manual ownership of one battery |

Positive battery power means charging and negative battery power means discharging. **System Charge Power** and **System Discharge Power** are separate unsigned values.

## How IDs are formed

Per-battery entities normally use `<domain>.<battery-name>_<key>`, shown below as `<domain>.*_<key>`. System entities use `<domain>.omnibattery_<key>` on fresh installs. Existing registry entries keep their current IDs, including translated IDs and the legacy `marstek_venus_system_` prefix.

## System device

These entities belong to **Omnibattery System** rather than one battery.

??? "Power, energy, and learning sensors"

    | Keys | When they appear |
    |---|---|
    | `system_soc`, `system_charge_power`, `system_discharge_power`, `system_battery_cell_power`, `system_total_energy`, `system_stored_energy` | Core system measurements |
    | `home_consumption`, `system_daily_home_energy`, `system_daily_grid_import_energy`, `system_daily_grid_export_energy`, `system_daily_grid_at_min_soc_energy` | Grid meter and controller available |
    | `system_daily_charging_energy`, `system_daily_discharging_energy` | Core aggregate energy |
    | `system_solar_power`, `system_daily_solar_energy` | Supported battery solar telemetry or configured solar source, as applicable |
    | `expected_home_consumption_profile`, `consumption_profile_capture` | Consumption learning available |
    | `balance_neto` | Hourly net balance configured |

??? "Control and diagnostic sensors"

    | Keys | When they appear |
    |---|---|
    | `integration_status`, `active_batteries`, `non_responsive_batteries`, `discharge_window`, `three_phase_protection_status`, `system_pd_control_quality`, `daily_operation_timeline` | Controller available |
    | `predictive_charging_active` | Predictive charging has been configured |
    | `curtailment_status`, `surplus_price_hold_status`, `discharge_reserve_status`, `high_price_discharge_status` | Relevant Dynamic Pricing feature available |
    | `capacity_protection_active` | Capacity protection configured |
    | `weekly_full_charge` | Weekly full charge enabled |
    | `charge_delay_status` | Charge delay configured |
    | `system_alarm_status` | At least one battery exposes alarm registers |

??? "System numbers"

    Feature controls appear only when that feature or pricing mode is configured.

    - PD control: `pd_controller_kp`, `pd_controller_kd`, `pd_controller_deadband`, `pd_controller_max_power_change`, `pd_controller_direction_hysteresis`, `pd_min_charge_power`, `pd_min_discharge_power`, `pd_relay_cooldown`, `pd_min_cycle_interval`, `pd_target_grid_power`, `no_pd_command_delay`
    - Fleet limits: `system_max_charge_power`, `system_max_discharge_power`, `max_contracted_power`
    - Capacity protection: `capacity_protection_limit`, `capacity_protection_soc_threshold`
    - Charge delay: `delay_safety_margin_min`, `charge_delay_balance_deadband_kwh`, `delay_soc_setpoint`
    - Temperature: `temp_charge_limit_c`, `temp_charge_limit_band_c`, `temp_charge_limit_floor_pct`
    - Predictive charging: `predictive_safety_margin_kwh`, `predictive_min_soc_floor`
    - Dynamic Pricing: `max_price_threshold`, `discharge_price_threshold`, `min_arbitrage_margin`, `round_trip_efficiency`, `negative_injection_threshold`, `predischarge_reserve_soc`, `surplus_hold_min_saving`, `discharge_reserve_min_saving`
    - Hourly net balance: `hourly_balance_target_net_wh`, `hourly_balance_max_offset_w`, `hourly_balance_deadband_wh`, `hourly_balance_hysteresis_w`
    - Excluded load: `excluded_device_exclusion_pct`, one per compatible configured device

??? "System switches"

    - Always available controller choices: `manual_mode`, `no_pd_mode`, `three_phase_protection`, `weekly_full_charge_enabled`, `vacation_mode`
    - Conditional features: `offgrid_mode`, `primary_feedforward`, `system_power_limits`, `predictive_charging`, `min_soc_floor_enabled`, `charge_delay`, `delay_soc_setpoint_enabled`, `weekly_full_charge_delay`, `temp_charge_limit`, `temp_charge_limit_discharge`, `capacity_protection`, `capacity_protection_excluded_devices`, `hourly_balance`
    - Pricing controls: `price_discharge_control`, `high_price_discharge`, `smart_predischarge`, `negative_price_charging`, `surplus_price_hold`, `discharge_reserve`
    - Generated controls: `time_slot` for each configured time slot; `excluded_device_enabled`, `excluded_device_solar_surplus`, `excluded_device_dynamic_power_control`, and `excluded_device_cover_home` for each applicable excluded device
    - External automation compatibility: `automation_charging_active`

??? "System selects and buttons"

    | Key | When it appears |
    |---|---|
    | `pd_tuning_profile` | Controller available |
    | `weekly_full_charge_day` | Controller available |
    | `battery_phase` | Per battery, for phase assignment |
    | `primary_battery`, `charge_priority` | More than one battery configured |
    | `reevaluate_dynamic_pricing` | Dynamic Pricing or Time Slot predictive mode |

## Battery devices

Battery entities depend on the driver. A missing entity usually means that the driver cannot supply or safely control that field; it does not mean setup failed.

??? "Battery state, power, and energy sensors"

    | Purpose | Keys |
    |---|---|
    | Charge and health | `battery_soc`, `battery_soh`, `battery_total_energy`, `stored_energy`, `battery_runtime_estimate`, `battery_cycle_count`, `battery_cycle_count_calc` |
    | Power | `battery_power`, `ac_power`, `battery_cell_power`, `grid_power`, `inverter_ac_power`, `ac_offgrid_power`, `max_charge_power`, `max_discharge_power`, `inverter_max_power`, `inverter_rated_power`, `output_limit`, `input_limit`, `power_restriction` |
    | Energy | `total_charging_energy`, `total_discharging_energy`, `total_daily_charging_energy`, `total_daily_discharging_energy`, `pv_total_generation`, `round_trip_efficiency_total` |
    | Electrical state | `battery_voltage`, `max_cell_voltage`, `min_cell_voltage`, `cell_voltage_delta`, `internal_temperature`, `internal_mos1_temperature`, `internal_mos2_temperature`, `max_cell_temperature`, `min_cell_temperature` |
    | Operating state | `inverter_state`, `battery_status`, `operating_mode`, `user_work_mode`, `ac_mode`, `remain_discharge_time`, `balancing_mode`, `backup_function` |

    Daily energy can come from a native counter, a cumulative-counter delta, or power integration. The source depends on driver capabilities.

??? "Pack and solar sensors"

    Pack telemetry is created only for drivers and live pack counts that expose it.

    - Pack charge: `battery_soc_pack_1` through `battery_soc_pack_7`
    - Pack voltage extremes: `max_cell_voltage_pack_1` through `max_cell_voltage_pack_7`, and `min_cell_voltage_pack_1` through `min_cell_voltage_pack_7`
    - Pack metadata: `pack_count`, `pack1_firmware_version`, `pack2_firmware_version`, `pack3_firmware_version`, `pack1_serial_number`, `pack2_serial_number`, `pack3_serial_number`
    - Solar input: `solar_power`, `mppt1_power` through `mppt4_power`, and `pv1_voltage` through `pv4_voltage`

??? "Device and connection sensors"

    Driver-provided diagnostics include `device_name`, `sn_code`, `software_version`, `bms_version`, `ems_version`, `vms_version`, `inverter_software_version`, `comm_module_firmware`, `power_module_serial_number`, `power_module_firmware_version`, `inverter_serial_number`, `mac_address`, `wifi_signal_strength`, `esp_ip`, `esp_ssid`, `esp_version`, `esp_wifi_signal_strength`, `bt_status`, `fault_level`, `fault_status`, and `alarm_status`.

??? "Cell balance monitor sensors"

    When the cell balance monitor is enabled, each supported battery can expose `cell_delta`, `balance_status`, `delta_trend`, `last_balance_read`, and `delta_avg_4w`. See [cell balance monitor](../features/cell-balance-monitor.md).

??? "Battery binary sensors"

    Connection states are `wifi_status`, `cloud_status`, and `esp_wifi_status`. Driver alarms can include `pll_abnormal_restart`, `overtemperature_limit`, `low_temperature_limit`, `fan_abnormal_warning`, `low_battery_soc_warning`, `output_overcurrent_warning`, `abnormal_line_sequence_detection`, `wifi_abnormal`, `ble_abnormal`, `network_abnormal`, `ct_connection_abnormal`, `grid_overvoltage`, `grid_undervoltage`, `grid_overfrequency`, `grid_underfrequency`, `grid_peak_voltage_abnormal`, `current_dcover`, `voltage_dcover`, `bat_overvoltage`, `bat_undervoltage`, `bat_overcurrent`, `bat_low_soc`, `bat_communication_failure`, and `bms_protect`.

    `balancing_mode` and `charge_hysteresis` appear only when the driver or configuration supports them.

??? "Battery numbers, switches, selects, and buttons"

    | Platform | Keys | Condition |
    |---|---|---|
    | Number | `set_charge_power`, `set_discharge_power` | Register control or software manual control |
    | Number | `max_charge_power`, `max_discharge_power`, `inverse_max_power` | Supported hardware or software limit |
    | Number | `charging_cutoff_capacity`, `discharging_cutoff_capacity`, `charge_to_soc`, `soc_set`, `min_soc` | Hardware cutoff or software limit, depending on driver |
    | Number | `backup_offgrid_threshold`, `charge_hysteresis_percent`, `battery_capacity` | Relevant driver/configuration |
    | Switch | `battery_allow_charge`, `battery_allow_discharge`, `battery_manual_mode` | Every controlled battery |
    | Switch | `full_charge_voltage_taper` | Compatible cell telemetry |
    | Switch | `backup_function`, `rs485_control_mode`, `lamp_switch` | Driver exposes the control |
    | Select | `force_mode`, `grid_off_mode`, `user_work_mode` | Driver exposes the control |
    | Button | `reset_device` | Driver exposes the command |

## Status and diagnostic attributes

??? "Integration Status states and blockers"

    **Integration Status** reports the highest-priority active condition. Its states cover predictive charging, weekly full charge, charge delay, price controls, electric-vehicle pauses, cell-balance hold, capacity protection, hourly net balance, backup mode, manual time-slot control, closed charging or discharging windows, normal charging or discharging, standby, manual mode, and initialization.

    | Attribute | Meaning |
    |---|---|
    | `charge_blocked`, `discharge_blocked` | Effective system permission |
    | `charge_blockers`, `discharge_blockers` | Global reasons, details, and timestamps |
    | `battery_charge_blockers`, `battery_discharge_blockers` | Reasons grouped by battery |
    | `manual_batteries` | Batteries under manual ownership |
    | `non_responsive_batteries` | Batteries excluded after communication or delivery failures |
    | `balance_hold_batteries` | Batteries held by cell-balance protection |
    | `backup_cooldown_batteries` | Batteries held after backup output activity |
    | `ev_chargers_active`, `ev_pause_until` | Active charger exclusions and pauses |
    | `hourly_balance_status`, `hourly_balance_offset_w`, `hourly_balance_net_kwh` | Net-balance controller state |
    | `temperature_charge_limit` | Current thermal-limit decision |

??? "Predictive Charging Active attributes"

    Common attributes include `charging_needed`, `reason`, `price_data_status`, forecast and consumption values, selected price slots, target energy, and current progress.

    Dynamic Pricing can also expose `chronological_planning_active`, `energy_horizon_end`, `overnight_consumption_kwh`, `earliest_projected_depletion`, `deadline_shortfall_kwh`, `energy_deadlines`, `slot_deadlines`, and `chronological_plan_reason`. They describe planning intent; live battery, grid, and safety limits remain authoritative.

??? "Other diagnostic attributes"

    **PD Control Quality** uses `stable`, `oscillating`, `sluggish`, `battery_limited`, `blocked`, or `collecting_data`. Its attributes expose tracking error, oscillation rate, metric age, active proportional–derivative (PD) settings, and profile.

    **Daily Operation Timeline** exposes bounded local-day `series`, `operations`, and `sources` data. See the [daily operation timeline](../features/daily-operation-timeline.md).

    **Expected Home Consumption Profile** exposes source, maturity, coverage, sample counts, and forecast data. **Vacation Mode** exposes its baseline and excluded learning periods.

## Control safety

Raw **Force Mode**, **Set Charge Power**, and **Set Discharge Power** require global **Manual Mode** or per-battery **Manual Battery Control**. Automatic control rejects competing writes. Battery configuration controls remain writable when the driver supports them.

Use [multiple batteries](../features/multi-battery.md) for ownership and participation behavior, and [troubleshooting](../troubleshooting.md) when a status or blocker does not explain the result.

!!! note "No time entity is currently created"
    `automation_charging_end_time` remains in translation resources, but the integration has no `time` platform and does not create that entity.
