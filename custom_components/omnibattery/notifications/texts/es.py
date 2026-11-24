"""Textos de notificación en español (España).

Mismas claves y mismos marcadores ``{...}`` que ``en.py``.
"""

# Lunes primero, como ``datetime.weekday()``.
DAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")

TEXTS: dict[str, str | dict[str, str]] = {
    # --- plan.py: _forecast_notification_context (horizon, labels, basis) ---
    "plan.horizon.until_tomorrow_midnight": "hasta la medianoche de mañana",
    "plan.horizon.until_day_time": "hasta el {day} a las {time}",
    "plan.horizon.until_sunrise": "hasta el amanecer ({time})",
    "plan.horizon.until_midnight": "hasta la medianoche",
    "plan.phrase.tomorrow": "el resto de hoy y mañana",
    "plan.phrase.tomorrow_until": "el resto de hoy y mañana ({horizon})",
    "plan.phrase.tonight": "el resto de hoy y esta noche {horizon}",
    "plan.phrase.today": "el resto de hoy",
    "plan.basis.profile": "perfil aprendido de 15 minutos",
    "plan.basis.profile_days": "perfil aprendido de 15 minutos ({days} días)",
    "plan.basis.temporary_curve": "curva diaria provisional",
    "plan.basis.remaining_average": "estimación del resto del día según la media diaria de {days} días",
    "plan.basis.remaining_default": "estimación por defecto del resto del día",
    "plan.basis.average": "media diaria de {days} días",
    "plan.basis.daily_default": "estimación diaria por defecto",
    "plan.basis.adjusted": "{basis}, ajustado con el consumo de hoy",
    "plan.balance.today": "Hoy",
    "plan.balance.tomorrow": "Mañana",
    "plan.balance.night": "Noche",
    "plan.balance.total": "Total",
    "plan.balance.solar": "☀️ Solar previsto",
    "plan.balance.solar_margin": "☀️ Con margen ({pct:.0f} %)",
    "plan.balance.consumption": "📊 Consumo casa",
    "plan.balance.battery": "🔋 Batería ({soc:.0f} %)",
    "plan.balance.grid_charge": "🔌 Carga de red",
    "plan.balance.available": "✅ Disponible (batería + sol)",
    "plan.balance.deficit": "⚡ Déficit de energía",
    "plan.balance.grid_need": "⚡ Necesidad total de la red",
    "plan.balance.grid_floor": "🔌 Carga de red (SOC mínimo garantizado)",
    "plan.balance.solar_surplus": "☀️ Excedente solar tras el consumo",
    "plan.balance.import_left": "🔌 Se deja a la red",
    # --- plan.py: _build_forecast_lines and shared lines ---
    "plan.line.basis": "   🧮 Base: {basis}",
    "plan.battery": "🔋 Batería: {soc:.0f}% ({usable:.2f} kWh utilizables)",
    "plan.horizon_text.today": "hoy",
    "plan.horizon_text.for": "para {phrase}",
    "plan.sufficient": "✓ Energía suficiente para {horizon}",
    "plan.no_more_charging": "No hace falta cargar más de la red.",
    "plan.no_charging": "No hace falta cargar de la red.",
    # --- plan.py: _limits_table (the power limits the plan used) ---
    "plan.power.header": "⚡ Potencia máxima",
    "plan.power.charge": "🔌 Carga",
    "plan.power.discharge": "🔋 Descarga",
    "plan.power.contracted": "{power:g}W (Máx. importación de red)",
    "plan.power.batteries": "{power:g}W (Baterías)",
    "plan.power.carried": (
        "La carga por encima de la potencia máxima de descarga se toma de la red: {carried:.2f} kWh, fuera de la carga"
    ),
    # --- plan.py: format_predictive_notification_message ---
    "plan.safe.title": "Carga Predictiva: modo seguro",
    "plan.safe.no_forecast": "⚠️ Sin previsión solar disponible — modo conservador",
    "plan.safe.not_activated": "La carga desde la red NO se ha activado.",
    "plan.deferred.title": "Carga Predictiva: aplazada por el calendario",
    "plan.deferred.no_quota": "🕒 Esta ventana configurada no tiene cuota de energía.",
    "plan.deferred.shortfall": "⚠️ Falta antes del plazo: {kwh:.2f} kWh",
    "plan.deferred.reserved": "La energía está reservada para otra ventana configurada.",
    "plan.not_required.title": "Carga Predictiva: no necesaria",
    "plan.expected_today.title": "Carga Predictiva: prevista para hoy",
    "plan.started.title": "Carga Predictiva: INICIADA",
    "plan.timing.when_cheap": "⏰ La carga se activará cuando los precios sean bajos",
    "plan.timing.until": "⏰ Cargando hasta: {time}",
    "plan.timing.now": "⏰ Cargando ahora desde la red",
    "plan.charge.needed": "⚡ Déficit de energía — hace falta cargar de la red",
    # --- plan.py: day prefix, clock and span ---
    "plan.day_prefix": "{day} {date} · ",
    # --- plan.py: _plan_order_lines ---
    # The one table's title, then its legend (under the table); plan.py
    # puts each row's icon in front.
    "plan.orders.title": "🗓️ Plan por tramos:",
    "plan.orders.legend": "Leyenda:",
    "plan.orders.charge": "Carga de red",
    "plan.orders.holds": "Batería reservada para más tarde (la casa compra de la red)",
    "plan.orders.empty": "Batería vacía (la casa compra de la red)",
    "plan.orders.exports": "☀️ Excedente solar para exportar: {items}",
    "plan.orders.sales": "💶 Vendiendo a la red:",
    "plan.orders.room": "🔋 Haciendo hueco para el sol:",
    "plan.orders.solar_delay": "⏳ Carga solar retrasada por el plan",
    # --- plan.py: _no_slots_explanation ---
    "plan.noslots.awaiting_prices.headline": (
        "Hace falta cargar más tarde, pero sus precios aún no están publicados"
    ),
    "plan.noslots.awaiting_prices.hint": (
        "Los {kwh:.2f} kWh se necesitan después del último precio publicado, así que "
        "todavía no se puede valorar ningún tramo. Se volverá a planificar en cuanto "
        "se publiquen los precios del día siguiente."
    ),
    "plan.noslots.cheaper_to_import.headline": (
        "Hace falta cargar, pero no compensa cargar de la red ahora"
    ),
    "plan.noslots.cheaper_to_import.hint": (
        "Importar los {kwh:.2f} kWh cuando se usen sale más barato que almacenarlos "
        "ahora, tras las pérdidas de ida y vuelta y el margen de arbitraje. Baja el "
        "margen mínimo de arbitraje para cargar de todos modos."
    ),
    "plan.noslots.capacity.headline": (
        "Hace falta cargar, pero los tramos restantes no llegan a tiempo"
    ),
    "plan.noslots.capacity.hint": (
        "Los tramos de precio que quedan antes de necesitar la energía son pocos o "
        "demasiado cortos con la potencia de carga disponible. Revisa la potencia "
        "máxima de carga y la potencia contratada, o espera al siguiente día de precios."
    ),
    "plan.noslots.no_power.headline": (
        "Hace falta cargar, pero la potencia de carga disponible es cero"
    ),
    "plan.noslots.no_power.hint": (
        "Revisa la potencia máxima de carga, la potencia contratada y el límite de "
        "reducción de picos."
    ),
    "plan.noslots.no_room.headline": "Hace falta cargar más tarde, pero ahora no se puede almacenar nada",
    "plan.noslots.no_room.hint": (
        "La batería no tiene hueco para la energía antes de usarla, así que no se "
        "reserva ningún tramo. El plan se rehace a medida que la batería se descarga "
        "y en la siguiente evaluación diaria."
    ),
    "plan.noslots.no_eligible.headline": (
        "Hace falta cargar, pero no quedan tramos de precio en el horizonte de planificación"
    ),
    "plan.noslots.no_eligible.hint": (
        "Revisa el sensor de precios: puede que los precios del día siguiente aún no estén publicados."
    ),
    "plan.noslots.default.headline": "Hace falta cargar, pero no hay tramos de precio válidos",
    "plan.noslots.hint.arbitrage": (
        "El diferencial de hoy es demasiado pequeño para cubrir las pérdidas de ida y "
        "vuelta con el margen de arbitraje configurado."
    ),
    "plan.noslots.hint.price": "Revisa el sensor de precios o sube el umbral de precio máximo.",
    "plan.noslots.cheapest": "Tramo más barato: {start} a {price:.4f} {unit}.\n{hint}",
    "plan.noslots.cheapest_ceiling": (
        "Tramo más barato: {start} a {price:.4f} {unit} "
        "(techo {ceiling:.4f} {unit}).\n{hint}"
    ),
    # --- plan.py: _plan_order_lines, its last line ---
    "plan.savings": (
        "💰 Ahorro estimado del plan: ~{saving:.2f} {cost_unit} (frente a usar la batería sin plan)"
    ),
    # --- plan.py: format_dynamic_pricing_notification ---
    "plan.price.charge": "carga ≤ {price:.4f} {unit}",
    "plan.price.discharge": "descarga ≥ {price:.4f} {unit}",
    "plan.price.arbitrage": "techo de arbitraje ≤ {price:.4f} {unit}",
    "plan.price.arbitrage_not_binding": (
        "techo de arbitraje ≤ {price:.4f} {unit} (no limita)"
    ),
    "plan.price.thresholds": "⚙️ Umbrales de precio: {parts}",
    "plan.pricing.not_needed.title": "Carga Predictiva: optimización de precio - NO necesaria",
    "plan.pricing.deferred.title": (
        "Carga Predictiva: optimización de precio - aún no hay nada que cargar"
    ),
    "plan.pricing.deferred.headline": "✓ Los tramos más baratos para la energía necesaria son mañana",
    "plan.pricing.deferred.footer": (
        "Ahora no hay carga de red programada; se volverá a planificar en la siguiente evaluación diaria."
    ),
    "plan.pricing.import.title": "Carga Predictiva: optimización de precio - Importar de la red sale más barato",
    "plan.pricing.awaiting.title": "Carga Predictiva: optimización de precio - Esperando precios",
    "plan.pricing.no_slots.title": "Carga Predictiva: optimización de precio - Sin tramos disponibles",
    "plan.pricing.import.headline": (
        "ℹ️ Sin carga de red: comprar la energía cuando se use sale más barato"
    ),
    "plan.pricing.import.storing": (
        "Almacenarla ahora costaría más tras las pérdidas de ida y vuelta."
    ),
    "plan.pricing.import.storing_margin": (
        "Almacenarla ahora costaría más tras las pérdidas de ida y vuelta y el "
        "margen de arbitraje de {margin:.4f} {unit}."
    ),
    "plan.pricing.warning": "⚠️ {headline}",
    "plan.pricing.hours": "{hours:g}h",
    "plan.pricing.hours_slots": "{hours:g}h ({slots})",
    "plan.pricing.slots": {"one": "{count} tramo", "other": "{count} tramos"},
    "plan.pricing.selected.title": "Carga Predictiva: optimización de precio - {hours} seleccionadas",
    "plan.pricing.floor.title": (
        "Carga Predictiva: optimización de precio + SOC mínimo - {hours} seleccionadas"
    ),
    "plan.pricing.negative.title": "Carga con precio negativo: {hours} seleccionadas",
    "plan.pricing.negative.opportunistic": "⚡ Carga oportunista prevista: {kwh:.2f} kWh",
    "plan.pricing.negative.no_deficit": "No hay déficit de energía previsto.",
    "plan.pricing.negative.header": "💰 Tramos más negativos que cumplen los criterios:",
    "plan.pricing.info.title": "Carga Predictiva: información de precios - {hours} más baratas",
    "plan.pricing.info.no_charging": "✓ No hace falta cargar de la red {horizon}",
    "plan.pricing.info.header": "💰 Horas más baratas de hoy (informativo):",
    "plan.pricing.info.footer": "No se activará ninguna carga.",
    "plan.pricing.opportunity": "⚡ Oportunidad de precio negativo: {kwh:.2f} kWh",
    "plan.pricing.deadline": "🕒 Necesaria antes de los plazos: {kwh:.2f} kWh",
    "plan.pricing.flexible": "💰 Flexible según precio: {kwh:.2f} kWh",
    "plan.pricing.tomorrow": (
        "📅 Planificada en los tramos de mañana (programada a las 00:05): {kwh:.2f} kWh"
    ),
    "plan.pricing.import_left": (
        "🔌 Se deja a la red (sale más barato importar al usarla): {kwh:.2f} kWh"
    ),
    "plan.pricing.cannot_deliver": (
        "⚠️ No se pueden aportar {kwh:.2f} kWh antes de su plazo ({reason})."
    ),
    "plan.pricing.selected_deadline": "Tramos seleccionados (los más baratos que cumplen cada plazo)",
    "plan.pricing.selected_hours": "Horas seleccionadas (las más baratas)",
    "plan.pricing.grid_charge_hours": (
        "🔌 Carga de red prevista: {kwh:.2f} kWh → hacen falta {hours:.1f}h de carga"
    ),
    "plan.pricing.slots_header": "💰 {label}:",
    "plan.pricing.average_price": "Precio medio: {price:.4f} {unit}",
    "plan.pricing.estimated_cost": "Coste estimado: ~{cost:.2f} {cost_unit}",
    # Piezas de una línea de tramo; `extras` une las opcionales.
    "plan.slot.line": "  • {span} → {price:.4f} {unit}{extras}",
    "plan.slot.kwh": ", {kwh:.2f} kWh",
    "plan.slot.before": " [antes de {time}]",
    "plan.slot.flexible": " [flexible]",
    "plan.slot.purpose": " [{purpose}]",
    "plan.slot.purpose_extra": " + {purpose}",
    "plan.table.header": "| Tramo | Precio | Energía | Coste |",
    "plan.table.power": "Potencia",
    "plan.table.price": "{price:.4f} {unit}",
    "plan.table.cost": "~{cost:.2f} {cost_unit}",
    "plan.table.total": "**Total**",
    "plan.table.kwh": "{kwh:.2f} kWh",
    # --- plan.py: format_slot_start_notification ---
    "slot_start.title": "Carga Predictiva: INICIADA ({price:.4f} {unit})",
    "slot_start.charging": "⚡ Cargando a un máximo de {power:g}W",
    "slot_start.slot": "Tramo: {span}",
    "slot_start.next": {
        "one": "Siguiente tramo: {time} · queda {count} tramo",
        "other": "Siguiente tramo: {time} · quedan {count} tramos",
    },
    "slot_start.last": "Último tramo · No quedan más tramos hoy",
    # --- plan.py: format_dp_pre_slot_reevaluation_notification ---
    "pre_slot.title": "Carga Predictiva: tramo {time} confirmado — hace falta cargar",
    "pre_slot.slot": "Tramo: {span} a {price:.4f} {unit}",
    "pre_slot.activate": "→ La carga se activará a las {time}",
    # --- switch.py: predictive charging override and manual mode ---
    "switch.override.title": "Carga Predictiva: desactivada",
    "switch.override.paused": (
        "La carga predictiva desde la red se ha pausado. Vuelve a activar el "
        "interruptor para reanudarla."
    ),
    "switch.override.disabled": (
        "La carga predictiva está desactivada. No se activará cuando la franja "
        "horaria pase a estar activa."
    ),
    "switch.manual.title": "Modo Manual: activo",
    "switch.manual.message": (
        "El control automático de carga/descarga está en pausa. "
        "Todas las baterías se han puesto en reposo (0W). "
        "Ahora puedes controlar cada batería manualmente con los controles "
        "'Potencia de carga/descarga forzada' o, en el caso de baterías "
        "cuya propia integración ofrece más modos de "
        "funcionamiento, elegir allí otro modo sin que "
        "OmniBattery lo revierta.\n\n"
        "Desactiva el Modo Manual para reanudar el control automático."
    ),
    # --- infra/alarm_notifier.py: AlarmNotifier._send (los nombres de alarma del driver siguen en inglés) ---
    "alarm.title.fault": "🚨 Fallo de batería: {device}",
    "alarm.title.warning": "⚠️ Aviso de batería: {device}",
    "alarm.header.fault": "🚨 Fallo detectado en {device}",
    "alarm.header.warning": "⚠️ Aviso detectado en {device}",
    "alarm.new_faults": "🆕 Fallos nuevos:",
    "alarm.new_alarms": "🆕 Alarmas nuevas:",
    "alarm.also_active": "⚡ También activas:",
    # --- tracking/balance_monitor.py: título y líneas de `issues` ---
    "balance.title": "{icon} Balance de celdas: {name}",
    "balance.high": "Delta: {delta:.0f} mV — desequilibrio alto entre celdas.",
    "balance.moderate": "Delta: {delta:.0f} mV — desequilibrio moderado.",
    "balance.consecutive": (
        "Desequilibrio alto entre celdas ({delta:.0f} mV) durante {count} cargas "
        "completas seguidas. Considera usar un blueprint de balanceo activo para "
        "esta batería y reequilibrar las celdas."
    ),
    "balance.trend": (
        "Tendencia creciente del desequilibrio: +{slope:.1f} mV/lectura, "
        "media de {avg:.0f} mV en las últimas lecturas."
    ),
    # --- control/weekly_full_charge.py: aviso de previsión solar ---
    "weekly.title": "Carga Completa Semanal: solar insuficiente",
    "weekly.not_enough_solar": (
        "La solar que queda hoy ({net:.1f} kWh tras el consumo de la casa) "
        "no cubrirá los {needed:.1f} kWh que aún faltan para llegar al 100%. "
        "Con la Carga Predictiva desactivada, la carga completa semanal no carga "
        "desde la red. Activa la Carga Predictiva o haz que una automatización "
        "use el atributo solar_sufficient del sensor de Carga Completa Semanal."
    ),
    # --- __init__.py: migración del balanceo activo heredado ---
    "migration.title": "Balanceo activo: migración en pausa ({name})",
    "migration.message": (
        "No se ha podido migrar con seguridad el antiguo balanceo activo "
        "integrado ({error}). La batería se mantiene en modo manual a 0 W. "
        "Déjala disponible y recarga Omnibattery para reintentarlo."
    ),
    # --- driver notices: control paused, firmware, settings, pacing ---
    "battery_control.paused_title": "Control de batería en pausa: {name}",
    "battery_control.realtime_mode_required": (
        "Omnibattery ha pausado el control de **{name}** porque la batería no está "
        "en modo **Realtime**.\n\n"
        "Abre la app de la batería y pon el modo de trabajo en Realtime. Omnibattery "
        "retoma el control automáticamente."
    ),
    "battery_control.handover_failed": (
        "Omnibattery no ha podido tomar el control de **{name}**: la batería no "
        "confirmó el modo de espera tras una orden de parada. Sigue monitorizándola "
        "y reintenta automáticamente."
    ),
    "battery_control.unknown_model": (
        "**{name}** indica el código de modelo `{model}`, que Omnibattery aún no "
        "soporta. Se monitoriza, pero no se controla. Comunica el código de modelo "
        "para poder añadirlo."
    ),
    "battery_control.firmware_title": "Actualización de firmware de la batería recomendada: {name}",
    "battery_control.firmware": (
        "**{name}** usa el firmware `{firmware}`. El fabricante recomienda al menos "
        "`{minimum}` para la API local. La monitorización funciona; actualiza la "
        "batería en su app si el control falla."
    ),
    "battery_control.setting_title": "Ajuste de batería no aplicado: {name}",
    "battery_control.setting_failed": (
        "Omnibattery no ha podido ajustar «{setting}» de **{name}** a {value}. La "
        "batería conserva su valor actual, y Omnibattery vuelve a mostrar y usar "
        "ese valor. Inténtalo más tarde, o cámbialo en la app de la batería."
    ),
    "battery_control.min_soc": "SOC mínimo",
    "battery_control.max_charge_power": "potencia máxima de carga",
    "battery_control.max_discharge_power": "potencia máxima de descarga",
    "battery_control.throttled_title": "Batería lenta: {name}",
    "battery_control.throttled": (
        "**{name}** responde despacio o no responde, así que Omnibattery espera "
        "ahora {interval:.0f}s entre peticiones. Puede que otra cosa esté consultando "
        "también la batería, como otra integración de Home Assistant, o que su "
        "conexión de red sea débil."
    ),
    "battery_control.official": (
        "\n\nLa integración oficial del fabricante también está consultando esta "
        "batería. Omnibattery está pensada para sustituirla: desactiva esa integración "
        "para este dispositivo y liberarás el presupuesto de peticiones "
        "de la batería. Sus controles (modo de energía, límite de descarga, límite de "
        "inyección) además escriben en la memoria flash de la batería cada vez que "
        "se usan."
    ),
    "battery_control.slower": (
        "\n\nSi sigue ocurriendo, baja un escalón la velocidad de peticiones de "
        "**{name}**, a **{speed}**: en Ajustes → Dispositivos y servicios, elige "
        "Reconfigurar en Omnibattery."
    ),
    "battery_control.speed_fast": "Rápida (2s)",
    "battery_control.speed_normal": "Normal (3s)",
    "battery_control.speed_conservative": "Conservadora (5s)",
}
