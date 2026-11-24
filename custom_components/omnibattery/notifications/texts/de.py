"""German notification texts (same keys and ``{...}`` markers as ``en``)."""

# Monday first, as ``datetime.weekday()``.
DAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")

TEXTS: dict[str, str | dict[str, str]] = {
    # --- plan.py: _forecast_notification_context (horizon, labels, basis) ---
    "plan.horizon.until_tomorrow_midnight": "bis morgen Mitternacht",
    "plan.horizon.until_day_time": "bis {day} {time}",
    "plan.horizon.until_sunrise": "bis Sonnenaufgang ({time})",
    "plan.horizon.until_midnight": "bis Mitternacht",
    "plan.phrase.tomorrow": "den Rest von heute und morgen",
    "plan.phrase.tomorrow_until": "den Rest von heute und morgen ({horizon})",
    "plan.phrase.tonight": "den Rest von heute und die Nacht {horizon}",
    "plan.phrase.today": "den Rest von heute",
    "plan.basis.profile": "gelerntes 15-Minuten-Profil",
    "plan.basis.profile_days": "gelerntes 15-Minuten-Profil ({days} Tage)",
    "plan.basis.temporary_curve": "vorläufige Tageskurve",
    "plan.basis.remaining_average": "Schätzung für den Resttag aus dem {days}-Tage-Tagesmittel",
    "plan.basis.remaining_default": "Standardschätzung für den Resttag",
    "plan.basis.average": "{days}-Tage-Tagesmittel",
    "plan.basis.daily_default": "Standard-Tagesschätzung",
    "plan.basis.adjusted": "{basis}, angepasst an den heutigen Verbrauch",
    "plan.balance.today": "Heute",
    "plan.balance.tomorrow": "Morgen",
    "plan.balance.night": "Nacht",
    "plan.balance.total": "Gesamt",
    "plan.balance.solar": "☀️ Solarprognose",
    "plan.balance.solar_margin": "☀️ Mit Marge ({pct:.0f} %)",
    "plan.balance.consumption": "📊 Hausverbrauch",
    "plan.balance.battery": "🔋 Batterie ({soc:.0f} %)",
    "plan.balance.grid_charge": "🔌 Netzladung",
    "plan.balance.available": "✅ Verfügbar (Batterie + Solar)",
    "plan.balance.deficit": "⚡ Energiedefizit",
    "plan.balance.grid_need": "⚡ Gesamter Netzbedarf",
    "plan.balance.grid_floor": "🔌 Netzladung (garantierter Mindest-SOC)",
    "plan.balance.solar_surplus": "☀️ Solarüberschuss nach Verbrauch",
    "plan.balance.import_left": "🔌 Dem Netz überlassen",
    # --- plan.py: _build_forecast_lines and shared lines ---
    "plan.line.basis": "   🧮 Grundlage: {basis}",
    "plan.battery": "🔋 Batterie: {soc:.0f}% ({usable:.2f} kWh nutzbar)",
    "plan.horizon_text.today": "heute",
    "plan.horizon_text.for": "für {phrase}",
    "plan.sufficient": "✓ Genug Energie für {horizon}",
    "plan.no_more_charging": "Kein weiteres Laden aus dem Netz nötig.",
    "plan.no_charging": "Kein Laden aus dem Netz nötig.",
    # --- plan.py: _limits_table (the power limits the plan used) ---
    "plan.power.header": "⚡ Maximale Leistung",
    "plan.power.charge": "🔌 Laden",
    "plan.power.discharge": "🔋 Entladen",
    "plan.power.contracted": "{power:g}W (Max. Netzbezug)",
    "plan.power.batteries": "{power:g}W (Batterien)",
    "plan.power.carried": (
        "Last über der maximalen Entladeleistung kommt aus dem Netz: {carried:.2f} kWh, nicht in der Ladung enthalten"
    ),
    # --- plan.py: format_predictive_notification_message ---
    "plan.safe.title": "Prädiktives Laden: Sicherer Modus",
    "plan.safe.no_forecast": "⚠️ Keine Solarprognose verfügbar — vorsichtiger Modus",
    "plan.safe.not_activated": "Laden aus dem Netz NICHT aktiviert.",
    "plan.deferred.title": "Prädiktives Laden: Vom Zeitplan zurückgestellt",
    "plan.deferred.no_quota": "🕒 Dieses konfigurierte Zeitfenster hat kein Energiekontingent.",
    "plan.deferred.shortfall": "⚠️ Fehlmenge bis zur Frist: {kwh:.2f} kWh",
    "plan.deferred.reserved": "Die Energie ist für ein anderes konfiguriertes Zeitfenster reserviert.",
    "plan.not_required.title": "Prädiktives Laden: Nicht nötig",
    "plan.expected_today.title": "Prädiktives Laden: Heute erwartet",
    "plan.started.title": "Prädiktives Laden: GESTARTET",
    "plan.timing.when_cheap": "⏰ Das Laden startet, wenn die Preise niedrig sind",
    "plan.timing.until": "⏰ Laden bis: {time}",
    "plan.timing.now": "⏰ Laden jetzt aus dem Netz",
    "plan.charge.needed": "⚡ Energiedefizit — Laden aus dem Netz nötig",
    # --- plan.py: day prefix, clock and span ---
    "plan.day_prefix": "{day} {date} · ",
    # --- plan.py: _plan_order_lines ---
    # The one table's title, then its legend (under the table); plan.py
    # puts each row's icon in front.
    "plan.orders.title": "🗓️ Plan nach Slots:",
    "plan.orders.legend": "Legende:",
    "plan.orders.charge": "Netzladung",
    "plan.orders.holds": "Batterie für später reserviert (das Haus bezieht aus dem Netz)",
    "plan.orders.empty": "Batterie leer (das Haus bezieht aus dem Netz)",
    "plan.orders.exports": "☀️ Solarüberschuss zum Einspeisen: {items}",
    "plan.orders.sales": "💶 Verkauf ins Netz:",
    "plan.orders.room": "🔋 Platz für die Sonne schaffen:",
    "plan.orders.solar_delay": "⏳ Solarladung vom Plan verzögert",
    # --- plan.py: _no_slots_explanation ---
    "plan.noslots.awaiting_prices.headline": (
        "Laden später nötig, aber die Preise sind noch nicht veröffentlicht"
    ),
    "plan.noslots.awaiting_prices.hint": (
        "Die {kwh:.2f} kWh werden nach dem letzten veröffentlichten Preis "
        "benötigt, daher kann noch kein Slot dagegen bewertet werden. Der Plan "
        "wird neu erstellt, sobald die Preise des nächsten Tages veröffentlicht "
        "sind."
    ),
    "plan.noslots.cheaper_to_import.headline": (
        "Laden nötig, aber Laden aus dem Netz lohnt sich jetzt nicht"
    ),
    "plan.noslots.cheaper_to_import.hint": (
        "Die {kwh:.2f} kWh zum Zeitpunkt des Verbrauchs zu beziehen ist günstiger, "
        "als sie jetzt nach Round-Trip-Verlusten und Arbitragemarge zu "
        "speichern. Senke die minimale Arbitragemarge, um trotzdem zu laden."
    ),
    "plan.noslots.capacity.headline": (
        "Laden nötig, aber die verbleibenden Slots schaffen es nicht rechtzeitig"
    ),
    "plan.noslots.capacity.hint": (
        "Die Preis-Slots bis zum Zeitpunkt des Energiebedarfs sind bei der "
        "verfügbaren Ladeleistung zu wenige oder zu kurz. Prüfe die max. "
        "Ladeleistung und die vertraglich vereinbarte Leistung oder warte auf "
        "den nächsten Preistag."
    ),
    "plan.noslots.no_power.headline": (
        "Laden nötig, aber die verfügbare Ladeleistung ist null"
    ),
    "plan.noslots.no_power.hint": (
        "Prüfe die max. Ladeleistung, die vertraglich vereinbarte Leistung und "
        "die Grenze der Spitzenlastkappung."
    ),
    "plan.noslots.no_room.headline": "Laden später nötig, aber jetzt kann nichts gespeichert werden",
    "plan.noslots.no_room.hint": (
        "Die Batterie hat vor dem Verbrauch keinen Platz für die Energie, daher "
        "wird kein Slot gebucht. Der Plan wird neu erstellt, wenn sich die "
        "Batterie entlädt und bei der nächsten täglichen Bewertung."
    ),
    "plan.noslots.no_eligible.headline": (
        "Laden nötig, aber im Planungszeitraum sind keine Preis-Slots mehr übrig"
    ),
    "plan.noslots.no_eligible.hint": (
        "Prüfe den Preissensor: Die Preise des nächsten Tages sind evtl. noch "
        "nicht veröffentlicht."
    ),
    "plan.noslots.default.headline": "Laden nötig, aber keine gültigen Preis-Slots gefunden",
    "plan.noslots.hint.arbitrage": (
        "Die heutige Preisspanne ist zu klein, um die Round-Trip-Verluste bei der "
        "konfigurierten Arbitragemarge zu decken."
    ),
    "plan.noslots.hint.price": "Prüfe den Preissensor oder erhöhe die Preisobergrenze.",
    "plan.noslots.cheapest": "Günstigster Slot: {start} zu {price:.4f} {unit}.\n{hint}",
    "plan.noslots.cheapest_ceiling": (
        "Günstigster Slot: {start} zu {price:.4f} {unit} "
        "(Obergrenze {ceiling:.4f} {unit}).\n{hint}"
    ),
    # --- plan.py: _plan_order_lines, its last line ---
    "plan.savings": (
        "💰 Geschätzte Ersparnis des Plans: ~{saving:.2f} {cost_unit} (gegenüber der Batterie ohne Plan)"
    ),
    # --- plan.py: format_dynamic_pricing_notification ---
    "plan.price.charge": "Laden ≤ {price:.4f} {unit}",
    "plan.price.discharge": "Entladen ≥ {price:.4f} {unit}",
    "plan.price.arbitrage": "Arbitrage-Obergrenze ≤ {price:.4f} {unit}",
    "plan.price.arbitrage_not_binding": (
        "Arbitrage-Obergrenze ≤ {price:.4f} {unit} (nicht bindend)"
    ),
    "plan.price.thresholds": "⚙️ Preisschwellen: {parts}",
    "plan.pricing.not_needed.title": "Prädiktives Laden: Preisoptimierung - NICHT nötig",
    "plan.pricing.deferred.title": (
        "Prädiktives Laden: Preisoptimierung - noch nichts zu laden"
    ),
    "plan.pricing.deferred.headline": "✓ Die günstigsten Slots für die benötigte Energie liegen morgen",
    "plan.pricing.deferred.footer": (
        "Jetzt ist kein Laden aus dem Netz geplant; es wird bei der nächsten täglichen Bewertung neu geplant."
    ),
    "plan.pricing.import.title": "Prädiktives Laden: Preisoptimierung - Netzbezug ist günstiger",
    "plan.pricing.awaiting.title": "Prädiktives Laden: Preisoptimierung - Warten auf Preise",
    "plan.pricing.no_slots.title": "Prädiktives Laden: Preisoptimierung - Keine Slots verfügbar",
    "plan.pricing.import.headline": (
        "ℹ️ Keine Netzladung: Die Energie zum Zeitpunkt des Verbrauchs zu kaufen ist günstiger"
    ),
    "plan.pricing.import.storing": (
        "Sie jetzt zu speichern wäre nach den Round-Trip-Verlusten teurer."
    ),
    "plan.pricing.import.storing_margin": (
        "Sie jetzt zu speichern wäre nach den Round-Trip-Verlusten und der "
        "Arbitragemarge von {margin:.4f} {unit} teurer."
    ),
    "plan.pricing.warning": "⚠️ {headline}",
    "plan.pricing.hours": "{hours:g}h",
    "plan.pricing.hours_slots": "{hours:g}h ({slots})",
    "plan.pricing.slots": {"one": "{count} Slot", "other": "{count} Slots"},
    "plan.pricing.selected.title": "Prädiktives Laden: Preisoptimierung - {hours} ausgewählt",
    "plan.pricing.floor.title": (
        "Prädiktives Laden: Preisoptimierung + Mindest-SOC - {hours} ausgewählt"
    ),
    "plan.pricing.negative.title": "Laden bei negativen Preisen: {hours} ausgewählt",
    "plan.pricing.negative.opportunistic": "⚡ Geplante opportunistische Ladung: {kwh:.2f} kWh",
    "plan.pricing.negative.no_deficit": "Es besteht kein prädiktives Energiedefizit.",
    "plan.pricing.negative.header": "💰 Slots mit den negativsten Preisen:",
    "plan.pricing.info.title": "Prädiktives Laden: Preisinfo - {hours} günstigste",
    "plan.pricing.info.no_charging": "✓ Kein Laden aus dem Netz nötig {horizon}",
    "plan.pricing.info.header": "💰 Günstigste Stunden heute (nur zur Information):",
    "plan.pricing.info.footer": "Es startet kein Laden.",
    "plan.pricing.opportunity": "⚡ Chance durch negative Preise: {kwh:.2f} kWh",
    "plan.pricing.deadline": "🕒 Vor den Fristen nötig: {kwh:.2f} kWh",
    "plan.pricing.flexible": "💰 Preislich flexibel: {kwh:.2f} kWh",
    "plan.pricing.tomorrow": (
        "📅 In den Slots von morgen geplant (Planung um 00:05): {kwh:.2f} kWh"
    ),
    "plan.pricing.import_left": (
        "🔌 Dem Netz überlassen (Bezug bei Verbrauch günstiger): {kwh:.2f} kWh"
    ),
    "plan.pricing.cannot_deliver": (
        "⚠️ {kwh:.2f} kWh sind vor der Frist nicht lieferbar ({reason})."
    ),
    "plan.pricing.selected_deadline": "Ausgewählte Slots (die günstigsten, die jede Frist einhalten)",
    "plan.pricing.selected_hours": "Ausgewählte Stunden (günstigste)",
    "plan.pricing.grid_charge_hours": (
        "🔌 Geplante Netzladung: {kwh:.2f} kWh → {hours:.1f}h Ladezeit nötig"
    ),
    "plan.pricing.slots_header": "💰 {label}:",
    "plan.pricing.average_price": "Durchschnittspreis: {price:.4f} {unit}",
    "plan.pricing.estimated_cost": "Geschätzte Kosten: ~{cost:.2f} {cost_unit}",
    # Pieces of one slot line (a price slot row); `extras` joins the optional ones.
    "plan.slot.line": "  • {span} → {price:.4f} {unit}{extras}",
    "plan.slot.kwh": ", {kwh:.2f} kWh",
    "plan.slot.before": " [vor {time}]",
    "plan.slot.flexible": " [flexibel]",
    "plan.slot.purpose": " [{purpose}]",
    "plan.slot.purpose_extra": " + {purpose}",
    "plan.table.header": "| Slot | Preis | Energie | Kosten |",
    "plan.table.power": "Leistung",
    "plan.table.price": "{price:.4f} {unit}",
    "plan.table.cost": "~{cost:.2f} {cost_unit}",
    "plan.table.total": "**Gesamt**",
    "plan.table.kwh": "{kwh:.2f} kWh",
    # --- plan.py: format_slot_start_notification ---
    "slot_start.title": "Prädiktives Laden: GESTARTET ({price:.4f} {unit})",
    "slot_start.charging": "⚡ Laden mit max. {power:g}W",
    "slot_start.slot": "Slot: {span}",
    # English keeps the phase 3 text ("slot(s)"); other languages use their plural.
    "slot_start.next": {
        "one": "Nächster Slot: {time} · noch {count} Slot",
        "other": "Nächster Slot: {time} · noch {count} Slots",
    },
    "slot_start.last": "Letzter Slot · Heute keine weiteren Slots",
    # --- plan.py: format_dp_pre_slot_reevaluation_notification ---
    "pre_slot.title": "Prädiktives Laden: Slot {time} bestätigt — Laden nötig",
    "pre_slot.slot": "Slot: {span} @ {price:.4f} {unit}",
    "pre_slot.activate": "→ Das Laden startet um {time}",
    # --- switch.py: predictive charging override and manual mode ---
    "switch.override.title": "Prädiktives Laden: deaktiviert",
    "switch.override.paused": (
        "Das prädiktive Laden aus dem Netz wurde pausiert. Schalte den Schalter wieder ein, um fortzufahren."
    ),
    "switch.override.disabled": (
        "Das prädiktive Laden ist jetzt deaktiviert. Es startet nicht, wenn das "
        "Zeitfenster aktiv wird."
    ),
    "switch.manual.title": "Manueller Modus: aktiv",
    "switch.manual.message": (
        "Die automatische Lade-/Entladeregelung ist pausiert. "
        "Alle Batterien wurden auf Bereit (0W) gesetzt. "
        "Du kannst jetzt jede Batterie manuell mit den Steuerelementen "
        "'Erzwungene Lade-/Entladeleistung einstellen' steuern, oder — "
        "bei Batterien, deren eigene Integration weitere "
        "Betriebsmodi anbietet — dort einen anderen Modus wählen, "
        "ohne dass OmniBattery ihn zurücksetzt.\n\n"
        "Schalte den manuellen Modus aus, um die automatische Regelung fortzusetzen."
    ),
    # --- infra/alarm_notifier.py: AlarmNotifier._send (driver alarm labels stay English) ---
    "alarm.title.fault": "🚨 Batteriefehler: {device}",
    "alarm.title.warning": "⚠️ Batteriewarnung: {device}",
    "alarm.header.fault": "🚨 Fehler erkannt bei {device}",
    "alarm.header.warning": "⚠️ Warnung erkannt bei {device}",
    "alarm.new_faults": "🆕 Neue Fehler:",
    "alarm.new_alarms": "🆕 Neue Alarme:",
    "alarm.also_active": "⚡ Außerdem aktiv:",
    # --- tracking/balance_monitor.py: title and `issues` lines ---
    "balance.title": "{icon} Zellabgleich: {name}",
    "balance.high": "Delta: {delta:.0f} mV — hohe Zellabweichung.",
    "balance.moderate": "Delta: {delta:.0f} mV — mäßige Zellabweichung.",
    "balance.consecutive": (
        "Hohe Zellabweichung ({delta:.0f} mV) bei {count} Vollladungen in Folge. "
        "Erwäge, für diese Batterie ein Blueprint für aktiven Zellabgleich "
        "auszuführen, um die Zellen anzugleichen."
    ),
    "balance.trend": (
        "Steigender Abweichungstrend: +{slope:.1f} mV/Messung, "
        "Ø {avg:.0f} mV über die letzten Messungen."
    ),
    # --- control/weekly_full_charge.py: solar outlook warning ---
    "weekly.title": "Wöchentliche Vollladung: nicht genug Solar",
    "weekly.not_enough_solar": (
        "Die heute verbleibende Solarenergie ({net:.1f} kWh nach Hausverbrauch) "
        "deckt die noch benötigten {needed:.1f} kWh bis 100% nicht. "
        "Bei ausgeschaltetem Prädiktivem Laden lädt die wöchentliche Vollladung "
        "nicht aus dem Netz. Aktiviere das Prädiktive Laden oder lass eine "
        "Automation auf das Attribut solar_sufficient des Sensors für die "
        "wöchentliche Vollladung reagieren."
    ),
    # --- __init__.py: legacy active-balance migration ---
    "migration.title": "Aktiver Zellabgleich: Migration pausiert ({name})",
    "migration.message": (
        "Der alte integrierte Lauf des aktiven Zellabgleichs konnte nicht sicher "
        "migriert werden ({error}). Die Batterie bleibt im manuellen Modus bei 0 W. "
        "Halte sie verfügbar und lade Omnibattery neu, um es erneut zu versuchen."
    ),
    # --- driver notices: control paused, firmware, settings, pacing ---
    "battery_control.paused_title": "Batteriesteuerung pausiert: {name}",
    "battery_control.realtime_mode_required": (
        "Omnibattery hat die Steuerung von **{name}** pausiert, weil sich die "
        "Batterie nicht im Modus **Realtime** befindet.\n\n"
        "Öffne die App der Batterie und stelle den Betriebsmodus auf Realtime. "
        "Omnibattery übernimmt die Steuerung dann automatisch wieder."
    ),
    "battery_control.handover_failed": (
        "Omnibattery konnte die Steuerung von **{name}** nicht übernehmen: Die "
        "Batterie hat nach einem Stoppbefehl keinen Standby bestätigt. Die "
        "Überwachung läuft weiter, und es wird automatisch erneut versucht."
    ),
    "battery_control.unknown_model": (
        "**{name}** meldet den Modellcode `{model}`, den Omnibattery noch nicht "
        "unterstützt. Die Batterie wird überwacht, aber nicht gesteuert. Bitte "
        "melde den Modellcode, damit er ergänzt werden kann."
    ),
    "battery_control.firmware_title": "Batterie-Firmware-Update empfohlen: {name}",
    "battery_control.firmware": (
        "**{name}** läuft mit der Firmware `{firmware}`. Der Hersteller empfiehlt für die "
        "lokale API mindestens `{minimum}`. Die Überwachung funktioniert; "
        "aktualisiere die Batterie in ihrer App, falls die Steuerung "
        "Probleme macht."
    ),
    "battery_control.setting_title": "Batterieeinstellung nicht übernommen: {name}",
    "battery_control.setting_failed": (
        "Omnibattery konnte „{setting}“ von **{name}** nicht auf {value} setzen. Die "
        "Batterie behält ihren aktuellen Wert, und Omnibattery zeigt und verwendet "
        "diesen Wert wieder. Versuche es später erneut oder ändere ihn in der "
        "App der Batterie."
    ),
    "battery_control.min_soc": "Minimaler SOC",
    "battery_control.max_charge_power": "Maximale Ladeleistung",
    "battery_control.max_discharge_power": "Maximale Entladeleistung",
    "battery_control.throttled_title": "Batterie langsam: {name}",
    "battery_control.throttled": (
        "**{name}** antwortet langsam oder gar nicht, deshalb wartet Omnibattery "
        "jetzt {interval:.0f}s zwischen den Anfragen. Möglicherweise fragt noch "
        "etwas anderes die Batterie ab, etwa eine andere Home-Assistant-Integration, "
        "oder ihre Netzwerkverbindung ist schwach."
    ),
    "battery_control.official": (
        "\n\nDie offizielle Integration des Herstellers fragt diese Batterie ebenfalls ab. "
        "Omnibattery ist als Ersatz gedacht: Deaktiviere diese Integration "
        "für dieses Gerät, damit sich die Batterie ihr Anfragebudget nicht teilen "
        "muss. Deren Bedienelemente (Energiemodus, Entladelimit, Einspeiselimit) "
        "schreiben außerdem bei jeder Nutzung in den Flash-Speicher der Batterie."
    ),
    "battery_control.slower": (
        "\n\nWenn das öfter passiert, senke die Anfragegeschwindigkeit von "
        "**{name}** um eine Stufe auf **{speed}**: Wähle unter Einstellungen → "
        "Geräte & Dienste bei Omnibattery „Neu konfigurieren“."
    ),
    "battery_control.speed_fast": "Schnell (2s)",
    "battery_control.speed_normal": "Normal (3s)",
    "battery_control.speed_conservative": "Konservativ (5s)",
}
