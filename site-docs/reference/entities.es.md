# Entidades de Home Assistant

Esta referencia enumera las familias de entidades que puede crear Omnibattery. Tu instalación solo muestra las entidades que admite su controlador de batería y las funciones que hayas configurado.

## Encuentra la entidad que necesitas

1. Empieza por la tabla de uso diario.
2. Busca en el catálogo completo el nombre visible o la clave.
3. Lee **Cuándo aparece** antes de usar una entidad en una automatización.

Home Assistant puede conservar un ID de entidad anterior o añadir un sufijo. Las entidades nuevas del sistema usan `omnibattery_`; las instalaciones actualizadas pueden conservar `marstek_venus_system_` para que el historial y las automatizaciones sigan funcionando. Usa el selector de entidades o el registro de entidades en lugar de asumir un ID exacto.

## Entidades de uso diario

| Nombre visible | Patrón habitual de una instalación nueva | Uso |
|---|---|---|
| **System SOC** | `sensor.omnibattery_system_soc` | Estado de carga (SOC) combinado |
| **System Charge Power** | `sensor.omnibattery_system_charge_power` | Potencia total de carga |
| **System Discharge Power** | `sensor.omnibattery_system_discharge_power` | Potencia total de descarga |
| **Home Consumption** | `sensor.omnibattery_home_consumption` | Demanda actual del hogar |
| **Integration Status** | `sensor.omnibattery_integration_status` | Modo de control actual y bloqueadores |
| **PD Control Quality** | `sensor.omnibattery_system_pd_control_quality` | Calidad del seguimiento de red |
| **Active Batteries** | `sensor.omnibattery_active_batteries` | Baterías que participan actualmente |
| **Non-Responsive Batteries** | `sensor.omnibattery_non_responsive_batteries` | Baterías retiradas temporalmente del control |
| **Predictive Charging Active** | `binary_sensor.omnibattery_predictive_charging_active` | Plan predictivo y estado de carga |
| **Allow Charge / Allow Discharge** | `switch.*_battery_allow_charge`, `switch.*_battery_allow_discharge` | Participación por batería |
| **Manual Battery Control** | `switch.*_battery_manual_mode` | Control manual de una batería |

La potencia positiva de batería significa carga y la negativa, descarga. **System Charge Power** y **System Discharge Power** son valores separados sin signo.

## Cómo se forman los ID

Las entidades por batería normalmente usan `<domain>.<battery-name>_<key>`, mostrado abajo como `<domain>.*_<key>`. Las entidades del sistema usan `<domain>.omnibattery_<key>` en instalaciones nuevas. Las entradas existentes en el registro conservan sus ID actuales, incluidos los ID traducidos y el prefijo heredado `marstek_venus_system_`.

## Dispositivo del sistema

Estas entidades pertenecen a **Omnibattery System**, no a una batería.

??? "Sensores de potencia, energía y aprendizaje"

    | Claves | Cuándo aparecen |
    |---|---|
    | `system_soc`, `system_charge_power`, `system_discharge_power`, `system_battery_cell_power`, `system_total_energy`, `system_stored_energy` | Mediciones básicas del sistema |
    | `home_consumption`, `system_daily_home_energy`, `system_daily_grid_import_energy`, `system_daily_grid_export_energy`, `system_daily_grid_at_min_soc_energy` | Medidor de red y controlador disponibles |
    | `system_daily_charging_energy`, `system_daily_discharging_energy` | Energía agregada básica |
    | `system_solar_power`, `system_daily_solar_energy` | Telemetría solar de batería admitida o fuente solar configurada, según corresponda |
    | `expected_home_consumption_profile`, `consumption_profile_capture` | Aprendizaje de consumo disponible |
    | `balance_neto` | Balance neto horario configurado |

??? "Sensores de control y diagnóstico"

    | Claves | Cuándo aparecen |
    |---|---|
    | `integration_status`, `active_batteries`, `non_responsive_batteries`, `discharge_window`, `three_phase_protection_status`, `system_pd_control_quality`, `daily_operation_timeline` | Controlador disponible |
    | `predictive_charging_active` | Se ha configurado carga predictiva |
    | `curtailment_status`, `surplus_price_hold_status`, `discharge_reserve_status`, `high_price_discharge_status` | Función de Precio dinámico pertinente disponible |
    | `capacity_protection_active` | Protección de capacidad configurada |
    | `weekly_full_charge` | Carga completa semanal activada |
    | `charge_delay_status` | Retraso de carga configurado |
    | `system_alarm_status` | Al menos una batería expone registros de alarma |

??? "Números del sistema"

    Los controles de funciones aparecen solo cuando se configura esa función o modo de precio.

    - Control PD: `pd_controller_kp`, `pd_controller_kd`, `pd_controller_deadband`, `pd_controller_max_power_change`, `pd_controller_direction_hysteresis`, `pd_min_charge_power`, `pd_min_discharge_power`, `pd_relay_cooldown`, `pd_min_cycle_interval`, `pd_target_grid_power`, `no_pd_command_delay`
    - Límites de flota: `system_max_charge_power`, `system_max_discharge_power`, `max_contracted_power`
    - Protección de capacidad: `capacity_protection_limit`, `capacity_protection_soc_threshold`
    - Retraso de carga: `delay_safety_margin_min`, `charge_delay_balance_deadband_kwh`, `delay_soc_setpoint`
    - Temperatura: `temp_charge_limit_c`, `temp_charge_limit_band_c`, `temp_charge_limit_floor_pct`
    - Carga predictiva: `predictive_safety_margin_kwh`, `predictive_min_soc_floor`
    - Precio dinámico: `max_price_threshold`, `discharge_price_threshold`, `min_arbitrage_margin`, `round_trip_efficiency`, `negative_injection_threshold`, `predischarge_reserve_soc`, `surplus_hold_min_saving`, `discharge_reserve_min_saving`
    - Balance neto horario: `hourly_balance_target_net_wh`, `hourly_balance_max_offset_w`, `hourly_balance_deadband_wh`, `hourly_balance_hysteresis_w`
    - Carga excluida: `excluded_device_exclusion_pct`, uno por cada dispositivo compatible configurado

??? "Interruptores del sistema"

    - Opciones de controlador siempre disponibles: `manual_mode`, `no_pd_mode`, `three_phase_protection`, `weekly_full_charge_enabled`, `vacation_mode`
    - Funciones condicionales: `offgrid_mode`, `primary_feedforward`, `system_power_limits`, `predictive_charging`, `min_soc_floor_enabled`, `charge_delay`, `delay_soc_setpoint_enabled`, `weekly_full_charge_delay`, `temp_charge_limit`, `temp_charge_limit_discharge`, `capacity_protection`, `capacity_protection_excluded_devices`, `hourly_balance`
    - Controles de precios: `price_discharge_control`, `smart_predischarge`, `negative_price_charging`, `surplus_price_hold`, `discharge_reserve`
    - Controles generados: `time_slot` para cada franja horaria configurada; `excluded_device_enabled`, `excluded_device_solar_surplus`, `excluded_device_dynamic_power_control` y `excluded_device_cover_home` para cada dispositivo excluido aplicable
    - Compatibilidad con automatización externa: `automation_charging_active`

??? "Selectores y botones del sistema"

    | Clave | Cuándo aparece |
    |---|---|
    | `pd_tuning_profile` | Controlador disponible |
    | `weekly_full_charge_day` | Controlador disponible |
    | `battery_phase` | Por batería, para asignación de fase |
    | `primary_battery`, `charge_priority` | Hay más de una batería configurada |
    | `high_price_sale` | Modo predictivo de Precio dinámico |
    | `reevaluate_dynamic_pricing` | Modo predictivo de Precio dinámico o Franja horaria |

## Dispositivos de batería

Las entidades de batería dependen del controlador. Una entidad ausente normalmente significa que el controlador no puede suministrar o controlar con seguridad ese campo; no significa que la configuración haya fallado.

??? "Sensores de estado, potencia y energía de batería"

    | Finalidad | Claves |
    |---|---|
    | Carga y salud | `battery_soc`, `battery_soh`, `battery_total_energy`, `stored_energy`, `battery_runtime_estimate`, `battery_cycle_count`, `battery_cycle_count_calc` |
    | Potencia | `battery_power`, `ac_power`, `battery_cell_power`, `grid_power`, `inverter_ac_power`, `ac_offgrid_power`, `max_charge_power`, `max_discharge_power`, `inverter_max_power`, `inverter_rated_power`, `output_limit`, `input_limit`, `power_restriction` |
    | Energía | `total_charging_energy`, `total_discharging_energy`, `total_daily_charging_energy`, `total_daily_discharging_energy`, `pv_total_generation`, `round_trip_efficiency_total` |
    | Estado eléctrico | `battery_voltage`, `max_cell_voltage`, `min_cell_voltage`, `cell_voltage_delta`, `internal_temperature`, `internal_mos1_temperature`, `internal_mos2_temperature`, `max_cell_temperature`, `min_cell_temperature` |
    | Estado de funcionamiento | `inverter_state`, `battery_status`, `operating_mode`, `user_work_mode`, `ac_mode`, `remain_discharge_time`, `balancing_mode`, `backup_function` |

    La energía diaria puede proceder de un contador nativo, del incremento de un contador acumulado o de integrar potencia. La fuente depende de las capacidades del controlador.

??? "Sensores de paquetes y solar"

    La telemetría de paquetes se crea solo para los controladores y los recuentos de paquetes activos que la exponen.

    - Carga de paquetes: `battery_soc_pack_1` hasta `battery_soc_pack_7`
    - Extremos de tensión de paquete: `max_cell_voltage_pack_1` hasta `max_cell_voltage_pack_7`, y `min_cell_voltage_pack_1` hasta `min_cell_voltage_pack_7`
    - Metadatos de paquetes: `pack_count`, `pack1_firmware_version`, `pack2_firmware_version`, `pack3_firmware_version`, `pack1_serial_number`, `pack2_serial_number`, `pack3_serial_number`
    - Entrada solar: `solar_power`, `mppt1_power` hasta `mppt4_power`, y `pv1_voltage` hasta `pv4_voltage`

??? "Sensores de dispositivo y conexión"

    Los diagnósticos proporcionados por el controlador incluyen `device_name`, `sn_code`, `software_version`, `bms_version`, `ems_version`, `vms_version`, `inverter_software_version`, `comm_module_firmware`, `power_module_serial_number`, `power_module_firmware_version`, `inverter_serial_number`, `mac_address`, `wifi_signal_strength`, `esp_ip`, `esp_ssid`, `esp_version`, `esp_wifi_signal_strength`, `bt_status`, `fault_level`, `fault_status` y `alarm_status`.

??? "Sensores del monitor de balance de celdas"

    Cuando se activa el monitor de balance de celdas, cada batería compatible puede exponer `cell_delta`, `balance_status`, `delta_trend`, `last_balance_read` y `delta_avg_4w`. Consulta el [monitor de balance de celdas](../features/cell-balance-monitor.md).

??? "Sensores binarios de batería"

    Los estados de conexión son `wifi_status`, `cloud_status` y `esp_wifi_status`. Las alarmas del controlador pueden incluir `pll_abnormal_restart`, `overtemperature_limit`, `low_temperature_limit`, `fan_abnormal_warning`, `low_battery_soc_warning`, `output_overcurrent_warning`, `abnormal_line_sequence_detection`, `wifi_abnormal`, `ble_abnormal`, `network_abnormal`, `ct_connection_abnormal`, `grid_overvoltage`, `grid_undervoltage`, `grid_overfrequency`, `grid_underfrequency`, `grid_peak_voltage_abnormal`, `current_dcover`, `voltage_dcover`, `bat_overvoltage`, `bat_undervoltage`, `bat_overcurrent`, `bat_low_soc`, `bat_communication_failure` y `bms_protect`.

    `balancing_mode` y `charge_hysteresis` aparecen solo cuando el controlador o la configuración los admiten.

??? "Números, interruptores, selectores y botones de batería"

    | Plataforma | Claves | Condición |
    |---|---|---|
    | Número | `set_charge_power`, `set_discharge_power` | Control por registro o control manual mediante software |
    | Número | `max_charge_power`, `max_discharge_power`, `inverse_max_power` | Límite de hardware o software admitido |
    | Número | `charging_cutoff_capacity`, `discharging_cutoff_capacity`, `charge_to_soc`, `soc_set`, `min_soc` | Corte de hardware o límite de software, según el controlador |
    | Número | `backup_offgrid_threshold`, `charge_hysteresis_percent`, `battery_capacity` | Controlador/configuración pertinente |
    | Interruptor | `battery_allow_charge`, `battery_allow_discharge`, `battery_manual_mode` | Cada batería controlada |
    | Interruptor | `full_charge_voltage_taper` | Telemetría de celdas compatible |
    | Interruptor | `backup_function`, `rs485_control_mode`, `lamp_switch` | El controlador expone el control |
    | Selector | `force_mode`, `grid_off_mode`, `user_work_mode` | El controlador expone el control |
    | Botón | `reset_device` | El controlador expone la orden |

## Atributos de estado y diagnóstico

??? "Estados y bloqueadores de Integration Status"

    **Integration Status** informa de la condición activa con mayor prioridad. Sus estados cubren carga predictiva, carga completa semanal, retraso de carga, controles de precios, pausas de vehículo eléctrico, retención por balance de celdas, protección de capacidad, balance neto horario, modo de respaldo, control manual por franja horaria, ventanas cerradas de carga o descarga, carga o descarga normal, reposo, modo manual e inicialización.

    | Atributo | Significado |
    |---|---|
    | `charge_blocked`, `discharge_blocked` | Permiso efectivo del sistema |
    | `charge_blockers`, `discharge_blockers` | Motivos globales, detalles y marcas temporales |
    | `battery_charge_blockers`, `battery_discharge_blockers` | Motivos agrupados por batería |
    | `manual_batteries` | Baterías bajo control manual |
    | `non_responsive_batteries` | Baterías excluidas tras fallos de comunicación o entrega |
    | `balance_hold_batteries` | Baterías retenidas por protección de balance de celdas |
    | `backup_cooldown_batteries` | Baterías retenidas tras actividad de salida de respaldo |
    | `ev_chargers_active`, `ev_pause_until` | Exclusiones y pausas de cargadores activos |
    | `hourly_balance_status`, `hourly_balance_offset_w`, `hourly_balance_net_kwh` | Estado del controlador de balance neto |
    | `temperature_charge_limit` | Decisión actual de límite térmico |

??? "Atributos de Predictive Charging Active"

    Los atributos comunes incluyen `charging_needed`, `reason`, `price_data_status`, valores de previsión y consumo, franjas de precio seleccionadas, energía objetivo y progreso actual.

    Precio dinámico también puede exponer `chronological_planning_active`, `energy_horizon_end`, `overnight_consumption_kwh`, `earliest_projected_depletion`, `deadline_shortfall_kwh`, `energy_deadlines`, `slot_deadlines` y `chronological_plan_reason`. Describen la intención de planificación; los límites activos de batería, red y seguridad mantienen la autoridad.

??? "Atributos de Estado de venta a precio alto"

    Estos atributos aparecen cuando la política correspondiente de Precio dinámico tiene un plan. Consulta [Precio dinámico](../configuration/predictive-charging/dynamic-pricing.md) para las reglas de decisión.

    | Atributo | Significado |
    |---|---|
    | `trigger_1_budget_kwh` | Energía de batería que **Venta a precio alto** (*Solo excedente* o superior) puede vender tras reservar la necesidad prevista hasta el amanecer y el margen de seguridad |
    | `refill_price` | Precio de exportación de referencia para que la solar rellene la batería mañana |
    | `trigger_1_reason` | Por qué la exportación de excedente está activa o inactiva |
    | `surplus_export_enabled` | Si **Venta a precio alto** está en *Solo excedente* o superior |
    | `surplus_kwh` | Energía asignada a exportación de excedente en ese periodo de precio |

??? "Otros atributos de diagnóstico"

    **PD Control Quality** usa `stable`, `oscillating`, `sluggish`, `battery_limited`, `blocked` o `collecting_data`. Sus atributos exponen el error de seguimiento, la tasa de oscilación, la antigüedad de la métrica, los ajustes activos de control proporcional–derivativo (PD) y el perfil.

    **Daily Operation Timeline** expone datos de día local acotado en `series`, `operations` y `sources`. Consulta la [cronología diaria](../features/daily-operation-timeline.md).

    **Expected Home Consumption Profile** expone fuente, madurez, cobertura, recuentos de muestras y datos de previsión. **Vacation Mode** expone su línea base y periodos de aprendizaje excluidos.

## Seguridad de control

Los controles directos **Force Mode**, **Set Charge Power** y **Set Discharge Power** requieren el **Manual Mode** global o el **Manual Battery Control** por batería. El control automático rechaza escrituras que compiten con él. Los controles de configuración de batería siguen siendo editables cuando el controlador los admite.

Usa [varias baterías](../features/multi-battery.md) para el comportamiento de control y participación, y [solución de problemas](../troubleshooting.md) cuando un estado o bloqueador no explica el resultado.

!!! note "Actualmente no se crea ninguna entidad de hora"
    `automation_charging_end_time` permanece en los recursos de traducción, pero la integración no tiene plataforma `time` y no crea esa entidad.
