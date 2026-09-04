# Entidades de Home Assistant

La integración crea automáticamente entidades para cada batería configurada y sensores agregados del sistema completo.

El sensor binario de estado de carga predictiva incluye diagnósticos de Precio Dinámico con plazos: `chronological_planning_active`, fuentes de curvas, `earliest_projected_depletion`, kWh con plazo/flexibles, *shortfalls*, `energy_deadlines` acumulados y mapas JSON-safe de cuota/plazo por slot. Estos atributos describen la intención del plan; los límites reales de baterías y red siguen siendo autoritativos.

## Sensores (por batería)

| Entidad | Descripción | Unidad |
|---|---|---|
| `sensor.*_battery_soc` | Estado de carga | % |
| `sensor.*_battery_power` | Potencia actual | W |
| `sensor.*_ac_power` | Potencia en el lado AC; en Anker se deriva de la potencia de batería con la convención de signo común | W |
| `sensor.*_battery_voltage` | Tensión | V |
| `sensor.*_battery_current` | Corriente | A |
| `sensor.*_battery_temperature` | Temperatura | °C |
| `sensor.*_internal_temperature` | Temperatura interna usada por la protección térmica; en Anker es un alias de `temperature` | °C |
| `sensor.*_total_charging_energy` | Energía total cargada | kWh |
| `sensor.*_total_discharging_energy` | Energía total descargada | kWh |
| `sensor.*_total_daily_charging_energy` | Energía cargada hoy (registro diario en Marstek; derivada del contador acumulado en Anker; integrada en Zendure) | kWh |
| `sensor.*_total_daily_discharging_energy` | Energía descargada hoy (registro diario en Marstek; derivada del contador acumulado en Anker; integrada en Zendure) | kWh |
| `sensor.*_battery_cycle_count` | Ciclos (registros, v3/vA/vD) | — |
| `sensor.*_battery_cycle_count_calc` | Ciclos calculados (todos) | — |
| `sensor.*_max_cell_voltage` | Tensión máx. de celda (v3/vA/vD) | V |
| `sensor.*_min_cell_voltage` | Tensión mín. de celda (v3/vA/vD) | V |
| `sensor.*_alarm_status` | Condiciones de alarma activas (v2) — diagnóstico | texto |
| `sensor.*_fault_status` | Condiciones de fallo activas (v2) — diagnóstico | texto |

## Sensores del monitor de equilibrio de celdas (por batería)

Solo presentes cuando el [monitor de equilibrio de celdas](../features/cell-balance-monitor.md) está activado en la configuración de carga semanal completa.

| Entidad | Descripción | Unidad |
|---|---|---|
| `sensor.*_cell_delta` | Diferencia de tensión entre la celda máxima y mínima en la última lectura OCV | mV |
| `sensor.*_balance_status` | Resultado del equilibrio: `green` / `yellow` / `orange` / `red` | — |
| `sensor.*_delta_trend` | Tendencia en las últimas lecturas formales: `rising` / `stable` / `falling` | — |
| `sensor.*_last_balance_read` | Marca de tiempo de la última lectura | timestamp |
| `sensor.*_delta_avg_4w` | Media de las últimas 4 lecturas formales | mV |

## Sensores de información de dispositivo

| Entidad | Descripción |
|---|---|
| `sensor.*_device_name` | Nombre del dispositivo |
| `sensor.*_sn_code` | Número de serie |
| `sensor.*_software_version` | Versión de firmware |
| `sensor.*_bms_version` | Versión BMS |
| `sensor.*_mac_address` | Dirección MAC |

## Sensores binarios

| Entidad | Descripción |
|---|---|
| `binary_sensor.*_wifi_status` | Estado WiFi |
| `binary_sensor.*_cloud_status` | Estado Cloud |
| `binary_sensor.marstek_venus_system_predictive_charging_active` | Carga predictiva activa (sistema) |
| `binary_sensor.omnibattery_curtailment_status` | Estado de predescarga inteligente / anti-vertido (solo Precio Dinámico) |

## Números (sliders)

| Entidad | Descripción | Rango |
|---|---|---|
| `number.*_max_soc` | SOC máximo | 0–100 % |
| `number.*_min_soc` | SOC mínimo | 0–100 % |
| `number.*_max_charge_power` | Potencia máx. de carga | W |
| `number.*_max_discharge_power` | Potencia máx. de descarga | W |
| `number.marstek_venus_system_system_max_charge_power` | Límite opcional de carga combinada para todo el sistema (`0 W` = desactivado). Solo se crea cuando los límites del sistema están activados. | Dinámico: suma de potencias de carga configuradas |
| `number.marstek_venus_system_system_max_discharge_power` | Límite opcional de descarga combinada para todo el sistema (`0 W` = desactivado). Solo se crea cuando los límites del sistema están activados. | Dinámico: suma de potencias de descarga configuradas |
| `number.omnibattery_predictive_safety_margin_kwh` | Margen de previsión solar usado por la carga predictiva y el anti-vertido en Precio Dinámico | 0–20 kWh |
| `number.omnibattery_negative_injection_threshold` | Umbral inclusivo de precio para franjas de riesgo de inyección negativa | -2–2 moneda/kWh |
| `number.omnibattery_predischarge_reserve_soc` | Suelo de SOC adicional para la predescarga inteligente | 0–100 % |
| `number.omnibattery_predischarge_max_export_power_w` | Exportación máxima durante la predescarga (`0 W` = solo autoconsumo) | 0–10000 W |

## Selectores

| Entidad | Opciones |
|---|---|
| `select.*_force_mode` | None / Charge / Discharge |
| `select.marstek_venus_system_pd_tuning_profile` | Muy suave / Suave / Equilibrado / Agresivo / Personalizado — presets de PD de un clic que fijan `Kp`, `Kd` y el límite de rampa a la vez (el deadband lo controla el usuario) |

## Switches

| Entidad | Descripción |
|---|---|
| `switch.*_rs485_control` | Modo control RS485 |
| `switch.*_allow_charge` | Control de software que permite que esta batería participe en la carga automática |
| `switch.*_allow_discharge` | Control de software que permite que esta batería participe en la descarga automática |
| `switch.*_battery_manual_mode` | Excluye esta batería del control automático de potencia, manteniendo su telemetría y potencia física en los agregados del sistema |
| `switch.*_backup_function` | Función de reserva — cuando está activo **y** la potencia AC offgrid ≠ 0 W, la batería queda excluida del control PD (no se envían comandos de escritura) |
| `switch.marstek_venus_system_override_predictive_charging` | Cancelar carga predictiva |
| `switch.omnibattery_smart_predischarge` | Activar predescarga inteligente / anti-vertido (solo Precio Dinámico) |
| `switch.omnibattery_negative_price_charging` | Activar carga oportunista con precios negativos de importación (solo Precio Dinámico) |

## Botones

| Entidad | Descripción |
|---|---|
| `button.*_reset` | Reset del dispositivo |
| `button.omnibattery_reevaluate_dynamic_pricing` | Reconstruye ahora el plan de Precio Dinámico; solo se crea en modo Precio Dinámico |

## Sensores del sistema

### Estado de la integración

`sensor.marstek_venus_system_integration_status` muestra de un vistazo qué está haciendo la integración en cada momento. Refleja el modo activo de mayor prioridad:

| Estado | Descripción |
|---|---|
| `Charging from Grid` | Carga predictiva desde la red activa |
| `Weekly Full Charge` | Cargando al 100 % para equilibrado de celdas |
| `Charge Delayed` | Carga bloqueada, esperando el momento óptimo según previsión solar |
| `Waiting for Solar` | Retraso de carga: esperando que comience la producción solar |
| `Charging to Setpoint` | Retraso de carga: cargando hasta el SOC mínimo configurado |
| `Capacity Protection` | Descarga limitada por SOC bajo (peak shaving activo) |
| `No-Discharge Window` | Dentro de una franja horaria sin descarga configurada |
| `Charging` | Cargando (excedente solar u otro) |
| `Discharging` | Descargando para cubrir el consumo del hogar |
| `Standby` | Sistema equilibrado dentro de la banda muerta, sin acción necesaria |
| `Manual Mode` | Modo manual activo — la integración no envía comandos automáticos |
| `Initializing` | Primer ciclo del controlador aún no completado |

El sensor también expone diagnósticos del registro de bloqueos como atributos:

| Atributo | Descripción |
|---|---|
| `charge_blocked` | `true` cuando la carga está bloqueada de forma efectiva en todo el sistema, por un bloqueo global o porque todas las baterías conocidas tienen la carga bloqueada |
| `discharge_blocked` | `true` cuando la descarga está bloqueada de forma efectiva en todo el sistema, por un bloqueo global o porque todas las baterías conocidas tienen la descarga bloqueada |
| `charge_blockers` | Bloqueos globales de carga activos con motivo, detalles y marca temporal |
| `discharge_blockers` | Bloqueos globales de descarga activos con motivo, detalles y marca temporal |
| `battery_charge_blockers` | Bloqueos de carga activos por batería, agrupados por batería, incluyendo permitir carga, SOC máximo e histéresis de carga |
| `battery_discharge_blockers` | Bloqueos de descarga activos por batería, agrupados por batería, incluyendo permitir descarga y SOC mínimo |

### Calidad de control PD

`sensor.marstek_venus_system_pd_control_quality` indica cómo de bien mantiene el controlador PD el objetivo de red, para que se vea el efecto de un [perfil de ajuste](../features/pd-controller.md#perfiles-de-ajuste) o un cambio de slider. El estado es un veredicto:

| Estado | Significado |
|---|---|
| `stable` | El PD sigue bien el objetivo |
| `oscillating` | Cabeceo — usa un perfil más suave o sube el deadband |
| `sluggish` | Demasiado lento — usa un perfil más agresivo |
| `battery_limited` | Batería llena/vacía o en su límite de potencia; el PD no puede actuar (no es problema de ajuste) |
| `blocked` | La dirección que exige el error de red no está permitida (retardo de carga, franja horaria, precio, pausa por VE); el PD está bloqueado, no mal ajustado |
| `collecting_data` | Calentando, o la métrica lleva más de 5 min sin avanzar |

Atributos: `rms_error_w` (error medio de seguimiento), `oscillation_per_min`, `metric_age_s` (segundos desde el último avance de la métrica), los `kp` / `kd` / `deadband_w` / `max_power_change_w` activos, y `active_profile`. La métrica es una media móvil de 60 s y se pausa brevemente tras un cambio de objetivo y mientras está limitada por batería o bloqueada, así que espera 1–2 min tras un cambio.

### Sensores agregados

Disponibles bajo el prefijo `sensor.marstek_venus_system_*`, suman los valores de todas las baterías:

- `system_battery_power` — Potencia total del sistema
- `system_battery_soc` — SOC promedio del sistema
- `system_total_charging_energy` — Energía total cargada (sistema)
- `system_total_discharging_energy` — Energía total descargada (sistema)
- `grid_at_min_soc` — Importación de red durante periodos en SOC mínimo (kWh)
- `system_alarm_status` — Estado de alarma agregado de todas las baterías (`OK` / `Warning` / `Fault`); los atributos listan las condiciones activas por batería
- `system_home_consumption` — Consumo instantáneo del hogar (W). Lee el sensor del hogar si está configurado, en caso contrario lo deriva de `red + AC de baterías + solar`.
- `system_daily_home_energy` — Consumo del hogar de hoy (kWh), integrado del valor de Consumo de la Casa anterior. Se reinicia a medianoche (hora local).

### Modo vacaciones

`switch.omnibattery_vacation_mode` pausa el aprendizaje de consumo sin pausar
la medición física ni el control de batería. Sus atributos muestran la carga
base constante activa, su origen, las noches válidas y los periodos excluidos
persistidos. Durante las vacaciones el sensor de perfil esperado usa
`source: vacation_baseline`.

### Perfil de consumo esperado del hogar

`sensor.omnibattery_expected_home_consumption_profile` es un sensor de
diagnóstico del perfil aprendido de 28 días. Su estado es la previsión de hoy en
kWh. Sus atributos incluyen `interval_profile_kwh`, `hourly_profile_kwh`,
`target_date`, `source`, `mature`, `coverage_ratio`, `weekday_samples`,
`day_type_samples`, `total_profile_days` y `newest_profile_date`. El resumen
acotado por día está disponible en los diagnósticos de la integración. El origen
es `profile` solo cuando se cumple el
contrato de madurez; `legacy_daily` identifica el fallback.

La carga predictiva también publica `solar_timeline_source`,
`solar_remaining_raw_kwh`, `solar_remaining_effective_kwh`,
`solar_timeline_fallback_reason`, `solar_profile_mature`,
`solar_profile_days`, `solar_profile_coverage_ratio` y
`solar_profile_generation`. Los diagnósticos contienen una sección acotada
`solar_profile` con origen de telemetría, contadores de calidad, generación,
estado de backfill y como máximo 24 valores resumidos de progreso.

### Línea temporal de operación diaria

`sensor.omnibattery_daily_operation_timeline` es un snapshot de diagnóstico del
día local que usa la tarjeta Resumen. Su estado es la fecha local y sus
atributos acotados contienen `schema_version`, `timezone`,
`interval_minutes` (15), `interval_count` (96), `current_index`,
`current_progress`, `mode`, frescura y los objetos `series`, `operations` y
`sources`. Las listas se excluyen de Recorder. Los valores `actual_*` son
medidos; los `planned_*` son proyecciones informativas y pueden ser `null` si
su fuente está obsoleta.

La línea conserva los intervalos cerrados aunque se reevalúe el plan y, tras un
reinicio, solo restaura el día local actual. Las máscaras `action_mask` usan
`solar_charge=1`, `grid_charge=2` y `discharge=4`; las máscaras de contexto
identifican setpoint, Retraso de Carga y el modo predictivo.
`grid_charge_decision` es independiente del flujo físico (`scheduled`,
`not_needed`, `unknown` o `not_applicable`).

Consulta la [guía de la línea temporal diaria](../features/daily-operation-timeline.es.md)
para las reglas visuales, DST e interacción móvil.
