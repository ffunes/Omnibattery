"""Textos de notificació en català (mateixes claus i marcadors que ``en``)."""

# Dilluns primer, com ``datetime.weekday()``.
DAYS = ("dl.", "dt.", "dc.", "dj.", "dv.", "ds.", "dg.")

TEXTS: dict[str, str | dict[str, str]] = {
    # --- plan.py: _forecast_notification_context (horizon, labels, basis) ---
    "plan.horizon.until_tomorrow_midnight": "fins a mitjanit de demà",
    "plan.horizon.until_day_time": "fins {day} {time}",
    "plan.horizon.until_sunrise": "fins a la sortida del sol ({time})",
    "plan.horizon.until_midnight": "fins a mitjanit",
    "plan.phrase.tomorrow": "la resta d'avui i demà",
    "plan.phrase.tomorrow_until": "la resta d'avui i demà ({horizon})",
    "plan.phrase.tonight": "la resta d'avui i aquesta nit {horizon}",
    "plan.phrase.today": "la resta d'avui",
    "plan.basis.profile": "perfil après de 15 minuts",
    "plan.basis.profile_days": "perfil après de 15 minuts ({days} dies)",
    "plan.basis.temporary_curve": "corba diària temporal",
    "plan.basis.remaining_average": "estimació del que queda del dia a partir de la mitjana diària de {days} dies",
    "plan.basis.remaining_default": "estimació per defecte del que queda del dia",
    "plan.basis.average": "mitjana diària de {days} dies",
    "plan.basis.daily_default": "estimació diària per defecte",
    "plan.basis.adjusted": "{basis}, ajustat amb el consum d'avui",
    "plan.balance.today": "Avui",
    "plan.balance.tomorrow": "Demà",
    "plan.balance.night": "Nit",
    "plan.balance.total": "Total",
    "plan.balance.solar": "☀️ Solar previst",
    "plan.balance.solar_margin": "☀️ Amb marge ({pct:.0f} %)",
    "plan.balance.consumption": "📊 Consum casa",
    "plan.balance.battery": "🔋 Bateria ({soc:.0f} %)",
    "plan.balance.grid_charge": "🔌 Càrrega de xarxa",
    "plan.balance.available": "✅ Disponible (bateria + sol)",
    "plan.balance.deficit": "⚡ Dèficit d'energia",
    "plan.balance.grid_need": "⚡ Necessitat total de la xarxa",
    "plan.balance.grid_floor": "🔌 Càrrega de xarxa (SOC mínim garantit)",
    "plan.balance.solar_surplus": "☀️ Excedent solar després del consum",
    "plan.balance.import_left": "🔌 Es deixa a la xarxa",
    # --- plan.py: _build_forecast_lines and shared lines ---
    "plan.line.basis": "   🧮 Base: {basis}",
    "plan.battery": "🔋 Bateria: {soc:.0f}% ({usable:.2f} kWh utilitzables)",
    "plan.horizon_text.today": "avui",
    "plan.horizon_text.for": "per a {phrase}",
    "plan.sufficient": "✓ Energia suficient per a {horizon}",
    "plan.no_more_charging": "No cal més càrrega des de la xarxa.",
    "plan.no_charging": "No cal càrrega des de la xarxa.",
    # --- plan.py: _limits_table (the power limits the plan used) ---
    "plan.power.header": "⚡ Potència màxima",
    "plan.power.charge": "🔌 Càrrega",
    "plan.power.discharge": "🔋 Descàrrega",
    "plan.power.contracted": "{power:g}W (Màx. importació de xarxa)",
    "plan.power.batteries": "{power:g}W (Bateries)",
    "plan.power.carried": (
        "El consum per sobre de la potència màxima de descàrrega ve de la xarxa: {carried:.2f} kWh, exclòs de la càrrega"
    ),
    # --- plan.py: format_predictive_notification_message ---
    "plan.safe.title": "Càrrega predictiva: mode segur",
    "plan.safe.no_forecast": "⚠️ Sense previsió solar disponible — mode conservador",
    "plan.safe.not_activated": "Càrrega des de la xarxa NO activada.",
    "plan.deferred.title": "Càrrega predictiva: ajornada per calendari",
    "plan.deferred.no_quota": "🕒 Aquesta franja configurada no té quota d'energia.",
    "plan.deferred.shortfall": "⚠️ Dèficit abans del termini: {kwh:.2f} kWh",
    "plan.deferred.reserved": "L'energia està reservada per a una altra franja configurada.",
    "plan.not_required.title": "Càrrega predictiva: no necessària",
    "plan.expected_today.title": "Càrrega predictiva: prevista per a avui",
    "plan.started.title": "Càrrega predictiva: INICIADA",
    "plan.timing.when_cheap": "⏰ La càrrega s'activarà quan els preus siguin baixos",
    "plan.timing.until": "⏰ Carregant fins a: {time}",
    "plan.timing.now": "⏰ Carregant ara des de la xarxa",
    "plan.charge.needed": "⚡ Dèficit d'energia — cal carregar des de la xarxa",
    # --- plan.py: day prefix, clock and span ---
    "plan.day_prefix": "{day} {date} · ",
    # --- plan.py: _plan_order_lines ---
    # The one table's title, then its legend (under the table); plan.py
    # puts each row's icon in front.
    "plan.orders.title": "🗓️ Pla per franges:",
    "plan.orders.legend": "Llegenda:",
    "plan.orders.charge": "Càrrega de xarxa",
    "plan.orders.holds": "Bateria reservada per a més tard (la casa compra de la xarxa)",
    "plan.orders.empty": "Bateria buida (la casa compra de la xarxa)",
    "plan.orders.exports": "☀️ Excedent solar per exportar: {items}",
    "plan.orders.sales": "💶 Venda a la xarxa:",
    "plan.orders.room": "🔋 Fent lloc per al sol:",
    "plan.orders.solar_delay": "⏳ Càrrega solar retardada pel pla",
    # --- plan.py: _no_slots_explanation ---
    "plan.noslots.awaiting_prices.headline": (
        "Cal carregar més tard, però encara no s'han publicat els seus preus"
    ),
    "plan.noslots.awaiting_prices.hint": (
        "Els {kwh:.2f} kWh es necessiten després de l'últim preu publicat, així que "
        "encara no es pot valorar cap franja. Es planificarà de nou tan bon punt "
        "es publiquin els preus del dia següent."
    ),
    "plan.noslots.cheaper_to_import.headline": (
        "Cal carregar, però carregar ara des de la xarxa no compensa"
    ),
    "plan.noslots.cheaper_to_import.hint": (
        "Importar els {kwh:.2f} kWh quan es consumeixin surt més barat que "
        "emmagatzemar-los ara, tenint en compte les pèrdues del cicle i el marge "
        "d'arbitratge. Redueix el marge mínim d'arbitratge per carregar igualment."
    ),
    "plan.noslots.capacity.headline": (
        "Cal carregar, però les franges restants no poden aportar-ho a temps"
    ),
    "plan.noslots.capacity.hint": (
        "Les franges de preu que queden abans de necessitar l'energia són massa "
        "poques o massa curtes amb la potència de càrrega disponible. Revisa la "
        "potència màx. de càrrega i la potència contractada, o espera al pròxim "
        "dia de preus."
    ),
    "plan.noslots.no_power.headline": (
        "Cal carregar, però la potència de càrrega disponible és zero"
    ),
    "plan.noslots.no_power.hint": (
        "Revisa la potència màx. de càrrega, la potència contractada i el límit de reducció de pics."
    ),
    "plan.noslots.no_room.headline": "Cal carregar més tard, però ara no es pot emmagatzemar res",
    "plan.noslots.no_room.hint": (
        "La bateria no té espai per a l'energia abans que es consumeixi, així que "
        "no es reserva cap franja. El pla es reconstrueix a mesura que la bateria "
        "es descarrega i en la propera avaluació diària."
    ),
    "plan.noslots.no_eligible.headline": (
        "Cal carregar, però no queden franges de preu a l'horitzó de planificació"
    ),
    "plan.noslots.no_eligible.hint": (
        "Revisa el sensor de preu: potser encara no s'han publicat els preus del dia següent."
    ),
    "plan.noslots.default.headline": "Cal carregar, però no s'han trobat franges de preu vàlides",
    "plan.noslots.hint.arbitrage": (
        "El diferencial d'avui és massa petit per cobrir les pèrdues del cicle amb "
        "el marge d'arbitratge configurat."
    ),
    "plan.noslots.hint.price": "Revisa el sensor de preu o puja el llindar de preu màxim.",
    "plan.noslots.cheapest": "Franja més barata: {start} a {price:.4f} {unit}.\n{hint}",
    "plan.noslots.cheapest_ceiling": (
        "Franja més barata: {start} a {price:.4f} {unit} "
        "(sostre {ceiling:.4f} {unit}).\n{hint}"
    ),
    # --- plan.py: _plan_order_lines, its last line ---
    "plan.savings": (
        "💰 Estalvi estimat del pla: ~{saving:.2f} {cost_unit} (respecte a fer servir la bateria sense pla)"
    ),
    # --- plan.py: format_dynamic_pricing_notification ---
    "plan.price.charge": "càrrega ≤ {price:.4f} {unit}",
    "plan.price.discharge": "descàrrega ≥ {price:.4f} {unit}",
    "plan.price.arbitrage": "sostre d'arbitratge ≤ {price:.4f} {unit}",
    "plan.price.arbitrage_not_binding": (
        "sostre d'arbitratge ≤ {price:.4f} {unit} (no limitant)"
    ),
    "plan.price.thresholds": "⚙️ Llindars de preu: {parts}",
    "plan.pricing.not_needed.title": "Càrrega predictiva: optimització de preu - NO necessària",
    "plan.pricing.deferred.title": (
        "Càrrega predictiva: optimització de preu - encara res a carregar"
    ),
    "plan.pricing.deferred.headline": "✓ Les franges més barates per a l'energia necessària són demà",
    "plan.pricing.deferred.footer": (
        "Ara no hi ha càrrega des de la xarxa programada; es planificarà de nou en la propera avaluació diària."
    ),
    "plan.pricing.import.title": "Càrrega predictiva: optimització de preu - Importar de la xarxa surt més barat",
    "plan.pricing.awaiting.title": "Càrrega predictiva: optimització de preu - Esperant els preus",
    "plan.pricing.no_slots.title": "Càrrega predictiva: optimització de preu - Sense franges disponibles",
    "plan.pricing.import.headline": (
        "ℹ️ Sense càrrega de xarxa: comprar l'energia quan es consumeix surt més barat"
    ),
    "plan.pricing.import.storing": (
        "Emmagatzemar-la ara costaria més tenint en compte les pèrdues del cicle."
    ),
    "plan.pricing.import.storing_margin": (
        "Emmagatzemar-la ara costaria més tenint en compte les pèrdues del cicle i "
        "el marge d'arbitratge de {margin:.4f} {unit}."
    ),
    "plan.pricing.warning": "⚠️ {headline}",
    "plan.pricing.hours": "{hours:g}h",
    "plan.pricing.hours_slots": "{hours:g}h ({slots})",
    "plan.pricing.slots": {"one": "{count} franja", "other": "{count} franges"},
    "plan.pricing.selected.title": "Càrrega predictiva: optimització de preu - {hours} seleccionades",
    "plan.pricing.floor.title": (
        "Càrrega predictiva: optimització de preu + SOC mínim - {hours} seleccionades"
    ),
    "plan.pricing.negative.title": "Càrrega amb preu negatiu: {hours} seleccionades",
    "plan.pricing.negative.opportunistic": "⚡ Càrrega oportunista planificada: {kwh:.2f} kWh",
    "plan.pricing.negative.no_deficit": "No cal cap dèficit d'energia predictiu.",
    "plan.pricing.negative.header": "💰 Franges amb preu més negatiu que compleixen:",
    "plan.pricing.info.title": "Càrrega predictiva: informació de preu - {hours} més barates",
    "plan.pricing.info.no_charging": "✓ No cal càrrega des de la xarxa {horizon}",
    "plan.pricing.info.header": "💰 Hores més barates d'avui (informatiu):",
    "plan.pricing.info.footer": "No s'activarà cap càrrega.",
    "plan.pricing.opportunity": "⚡ Oportunitat de preu negatiu: {kwh:.2f} kWh",
    "plan.pricing.deadline": "🕒 Necessari abans dels terminis: {kwh:.2f} kWh",
    "plan.pricing.flexible": "💰 Flexible segons el preu: {kwh:.2f} kWh",
    "plan.pricing.tomorrow": (
        "📅 Planificat a les franges de demà (programat a les 00:05): {kwh:.2f} kWh"
    ),
    "plan.pricing.import_left": (
        "🔌 Deixat a la xarxa (importar quan es consumeix surt més barat): {kwh:.2f} kWh"
    ),
    "plan.pricing.cannot_deliver": (
        "⚠️ No es poden aportar {kwh:.2f} kWh abans del seu termini ({reason})."
    ),
    "plan.pricing.selected_deadline": "Franges seleccionades (les més barates que compleixen cada termini)",
    "plan.pricing.selected_hours": "Hores seleccionades (les més barates)",
    "plan.pricing.grid_charge_hours": (
        "🔌 Càrrega de xarxa planificada: {kwh:.2f} kWh → calen {hours:.1f}h de càrrega"
    ),
    "plan.pricing.slots_header": "💰 {label}:",
    "plan.pricing.average_price": "Preu mitjà: {price:.4f} {unit}",
    "plan.pricing.estimated_cost": "Cost estimat: ~{cost:.2f} {cost_unit}",
    # Pieces of one slot line (a price slot row); `extras` joins the optional ones.
    "plan.slot.line": "  • {span} → {price:.4f} {unit}{extras}",
    "plan.slot.kwh": ", {kwh:.2f} kWh",
    "plan.slot.before": " [abans de {time}]",
    "plan.slot.flexible": " [flexible]",
    "plan.slot.purpose": " [{purpose}]",
    "plan.slot.purpose_extra": " + {purpose}",
    "plan.table.header": "| Franja | Preu | Energia | Cost |",
    "plan.table.power": "Potència",
    "plan.table.price": "{price:.4f} {unit}",
    "plan.table.cost": "~{cost:.2f} {cost_unit}",
    "plan.table.total": "**Total**",
    "plan.table.kwh": "{kwh:.2f} kWh",
    # --- plan.py: format_slot_start_notification ---
    "slot_start.title": "Càrrega predictiva: INICIADA ({price:.4f} {unit})",
    "slot_start.charging": "⚡ Carregant a un màxim de {power:g}W",
    "slot_start.slot": "Franja: {span}",
    # English keeps the phase 3 text ("slot(s)"); other languages use their plural.
    "slot_start.next": {
        "one": "Propera franja: {time} · {count} franja restant",
        "other": "Propera franja: {time} · {count} franges restants",
    },
    "slot_start.last": "Última franja · No queden més franges avui",
    # --- plan.py: format_dp_pre_slot_reevaluation_notification ---
    "pre_slot.title": "Càrrega predictiva: franja {time} confirmada — cal carregar",
    "pre_slot.slot": "Franja: {span} @ {price:.4f} {unit}",
    "pre_slot.activate": "→ La càrrega s'activarà a les {time}",
    # --- switch.py: predictive charging override and manual mode ---
    "switch.override.title": "Càrrega predictiva: desactivada",
    "switch.override.paused": (
        "La càrrega predictiva des de la xarxa s'ha posat en pausa. Torna a "
        "activar l'interruptor per reprendre-la."
    ),
    "switch.override.disabled": (
        "La càrrega predictiva ara està desactivada. No s'activarà quan la franja "
        "horària estigui activa."
    ),
    "switch.manual.title": "Mode manual: actiu",
    "switch.manual.message": (
        "El control automàtic de càrrega/descàrrega està en pausa. "
        "Totes les bateries s'han posat en repòs (0W). "
        "Ara pots controlar manualment cada bateria amb els controls "
        "'Establir potència de càrrega/descàrrega forçada' o, "
        "per a les bateries la integració pròpia de les quals ofereix més "
        "modes de funcionament, triar-hi un altre mode "
        "sense que OmniBattery el reverteixi.\n\n"
        "Desactiva el mode manual per reprendre el control automàtic."
    ),
    # --- infra/alarm_notifier.py: AlarmNotifier._send (driver alarm labels stay English) ---
    "alarm.title.fault": "🚨 Fallada de bateria: {device}",
    "alarm.title.warning": "⚠️ Avís de bateria: {device}",
    "alarm.header.fault": "🚨 Fallada detectada a {device}",
    "alarm.header.warning": "⚠️ Avís detectat a {device}",
    "alarm.new_faults": "🆕 Noves fallades:",
    "alarm.new_alarms": "🆕 Noves alarmes:",
    "alarm.also_active": "⚡ També actives:",
    # --- tracking/balance_monitor.py: title and `issues` lines ---
    "balance.title": "{icon} Equilibri de cel·les: {name}",
    "balance.high": "Delta: {delta:.0f} mV — desequilibri alt de cel·les.",
    "balance.moderate": "Delta: {delta:.0f} mV — desequilibri moderat.",
    "balance.consecutive": (
        "Desequilibri alt de cel·les ({delta:.0f} mV) durant {count} càrregues "
        "completes consecutives. Considera executar un blueprint d'equilibratge "
        "actiu per a aquesta bateria per reequilibrar les cel·les."
    ),
    "balance.trend": (
        "Tendència creixent del desequilibri: +{slope:.1f} mV/lectura, "
        "mitjana de {avg:.0f} mV en les últimes lectures."
    ),
    # --- control/weekly_full_charge.py: solar outlook warning ---
    "weekly.title": "Càrrega completa setmanal: no hi ha prou sol",
    "weekly.not_enough_solar": (
        "El solar que queda avui ({net:.1f} kWh després del consum de la llar) "
        "no cobrirà els {needed:.1f} kWh que encara calen per arribar al 100%. "
        "Amb la càrrega predictiva desactivada, la càrrega completa setmanal no "
        "carrega des de la xarxa. Activa la càrrega predictiva, o deixa que una "
        "automatització actuï segons l'atribut solar_sufficient del sensor de "
        "càrrega completa setmanal."
    ),
    # --- __init__.py: legacy active-balance migration ---
    "migration.title": "Equilibratge actiu: migració en pausa ({name})",
    "migration.message": (
        "L'antiga execució d'equilibratge actiu integrada no s'ha pogut migrar "
        "de manera segura ({error}). La bateria es manté en mode manual a 0 W. "
        "Mantén-la disponible i recarrega Omnibattery per tornar-ho a provar."
    ),
    # --- driver notices: control paused, firmware, settings, pacing ---
    "battery_control.paused_title": "Control de bateria en pausa: {name}",
    "battery_control.realtime_mode_required": (
        "Omnibattery ha posat en pausa el control de **{name}** perquè la bateria "
        "no està en mode **Realtime**.\n\n"
        "Obre l'app de la bateria i posa el mode de treball en Realtime. Omnibattery "
        "reprèn el control automàticament."
    ),
    "battery_control.handover_failed": (
        "Omnibattery no ha pogut prendre el control de **{name}**: la bateria no "
        "ha confirmat el mode d'espera després d'una ordre d'aturada. Continua "
        "monitorant-la i ho reintenta automàticament."
    ),
    "battery_control.unknown_model": (
        "**{name}** indica el codi de model `{model}`, que Omnibattery encara no "
        "suporta. Es monitora, però no es controla. Comunica el codi de model "
        "perquè es pugui afegir."
    ),
    "battery_control.firmware_title": "Actualització de firmware de la bateria recomanada: {name}",
    "battery_control.firmware": (
        "**{name}** fa servir el firmware `{firmware}`. El fabricant recomana almenys "
        "`{minimum}` per a l'API local. La monitorització funciona; actualitza la "
        "bateria a la seva app si el control falla."
    ),
    "battery_control.setting_title": "Ajust de bateria no aplicat: {name}",
    "battery_control.setting_failed": (
        "Omnibattery no ha pogut ajustar «{setting}» de **{name}** a {value}. La "
        "bateria conserva el seu valor actual, i Omnibattery torna a mostrar i fer "
        "servir aquest valor. Torna-ho a provar més tard, o canvia'l a l'app "
        "de la bateria."
    ),
    "battery_control.min_soc": "SOC mínim",
    "battery_control.max_charge_power": "potència màxima de càrrega",
    "battery_control.max_discharge_power": "potència màxima de descàrrega",
    "battery_control.throttled_title": "Bateria lenta: {name}",
    "battery_control.throttled": (
        "**{name}** respon lentament o no respon, així que Omnibattery espera "
        "ara {interval:.0f}s entre peticions. Pot ser que una altra cosa estigui "
        "consultant també la bateria, com una altra integració de Home Assistant, "
        "o que la seva connexió de xarxa sigui feble."
    ),
    "battery_control.official": (
        "\n\nLa integració oficial del fabricant també està consultant aquesta "
        "bateria. Omnibattery està pensada per substituir-la: desactiva aquesta "
        "integració per a aquest dispositiu i alliberaràs el pressupost "
        "de peticions de la bateria. Els seus controls (mode d'energia, límit de "
        "descàrrega, límit d'injecció) a més escriuen a la memòria flash de la "
        "bateria cada vegada que es fan servir."
    ),
    "battery_control.slower": (
        "\n\nSi continua passant, baixa un esglaó la velocitat de peticions de "
        "**{name}**, a **{speed}**: a Paràmetres → Dispositius i serveis, tria "
        "Reconfigura a Omnibattery."
    ),
    "battery_control.speed_fast": "Ràpida (2s)",
    "battery_control.speed_normal": "Normal (3s)",
    "battery_control.speed_conservative": "Conservadora (5s)",
}
