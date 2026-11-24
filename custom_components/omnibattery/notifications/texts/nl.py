"""Dutch notification texts. Same keys and ``{...}`` markers as ``en``."""

# Monday first, as ``datetime.weekday()``.
DAYS = ("ma", "di", "wo", "do", "vr", "za", "zo")

TEXTS: dict[str, str | dict[str, str]] = {
    # --- plan.py: _forecast_notification_context (horizon, labels, basis) ---
    "plan.horizon.until_tomorrow_midnight": "tot morgenmiddernacht",
    "plan.horizon.until_day_time": "tot {day} {time}",
    "plan.horizon.until_sunrise": "tot zonsopgang ({time})",
    "plan.horizon.until_midnight": "tot middernacht",
    "plan.phrase.tomorrow": "de rest van vandaag en morgen",
    "plan.phrase.tomorrow_until": "de rest van vandaag en morgen ({horizon})",
    "plan.phrase.tonight": "de rest van vandaag en vannacht {horizon}",
    "plan.phrase.today": "de rest van vandaag",
    "plan.basis.profile": "geleerd profiel per kwartier",
    "plan.basis.profile_days": "geleerd profiel per kwartier ({days} dagen)",
    "plan.basis.temporary_curve": "tijdelijke dagcurve",
    "plan.basis.remaining_average": "schatting voor de rest van de dag op basis van het daggemiddelde over {days} dagen",
    "plan.basis.remaining_default": "standaardschatting voor de rest van de dag",
    "plan.basis.average": "daggemiddelde over {days} dagen",
    "plan.basis.daily_default": "standaard dagschatting",
    "plan.basis.adjusted": "{basis}, aangepast met het verbruik van vandaag",
    "plan.balance.today": "Vandaag",
    "plan.balance.tomorrow": "Morgen",
    "plan.balance.night": "Nacht",
    "plan.balance.total": "Totaal",
    "plan.balance.solar": "☀️ Zonneprognose",
    "plan.balance.solar_margin": "☀️ Met marge ({pct:.0f} %)",
    "plan.balance.consumption": "📊 Huisverbruik",
    "plan.balance.battery": "🔋 Batterij ({soc:.0f} %)",
    "plan.balance.grid_charge": "🔌 Netladen",
    "plan.balance.available": "✅ Beschikbaar (batterij + zon)",
    "plan.balance.deficit": "⚡ Energietekort",
    "plan.balance.grid_need": "⚡ Totale netbehoefte",
    "plan.balance.grid_floor": "🔌 Netladen (gegarandeerde minimum-SOC)",
    "plan.balance.solar_surplus": "☀️ Zonne-overschot na verbruik",
    "plan.balance.import_left": "🔌 Aan het net overgelaten",
    # --- plan.py: _build_forecast_lines and shared lines ---
    "plan.line.basis": "   🧮 Basis: {basis}",
    "plan.battery": "🔋 Batterij: {soc:.0f}% ({usable:.2f} kWh bruikbaar)",
    "plan.horizon_text.today": "vandaag",
    "plan.horizon_text.for": "voor {phrase}",
    "plan.sufficient": "✓ Voldoende energie voor {horizon}",
    "plan.no_more_charging": "Geen extra laden vanaf het net nodig.",
    "plan.no_charging": "Geen laden vanaf het net nodig.",
    # --- plan.py: _limits_table (the power limits the plan used) ---
    "plan.power.header": "⚡ Maximaal vermogen",
    "plan.power.charge": "🔌 Laden",
    "plan.power.discharge": "🔋 Ontladen",
    "plan.power.contracted": "{power:g}W (Max. netafname)",
    "plan.power.batteries": "{power:g}W (batterijen)",
    "plan.power.carried": (
        "Belasting boven het maximale ontlaadvermogen komt van het net: {carried:.2f} kWh, niet meegerekend in het laden"
    ),
    # --- plan.py: format_predictive_notification_message ---
    "plan.safe.title": "Voorspellend laden: veilige modus",
    "plan.safe.no_forecast": "⚠️ Geen zonneprognose beschikbaar — voorzichtige modus",
    "plan.safe.not_activated": "Laden vanaf het net is NIET geactiveerd.",
    "plan.deferred.title": "Voorspellend laden: uitgesteld door tijdschema",
    "plan.deferred.no_quota": "🕒 Dit ingestelde venster heeft geen energiekwota.",
    "plan.deferred.shortfall": "⚠️ Tekort voor de deadline: {kwh:.2f} kWh",
    "plan.deferred.reserved": "De energie is gereserveerd voor een ander ingesteld venster.",
    "plan.not_required.title": "Voorspellend laden: niet nodig",
    "plan.expected_today.title": "Voorspellend laden: verwacht vandaag",
    "plan.started.title": "Voorspellend laden: GESTART",
    "plan.timing.when_cheap": "⏰ Het laden start zodra de prijzen laag zijn",
    "plan.timing.until": "⏰ Laden tot: {time}",
    "plan.timing.now": "⏰ Nu laden vanaf het net",
    "plan.charge.needed": "⚡ Energietekort — laden vanaf het net nodig",
    # --- plan.py: day prefix, clock and span ---
    "plan.day_prefix": "{day} {date} · ",
    # --- plan.py: _plan_order_lines ---
    # The one table's title, then its legend (under the table); plan.py
    # puts each row's icon in front.
    "plan.orders.title": "🗓️ Plan per slot:",
    "plan.orders.legend": "Legenda:",
    "plan.orders.charge": "Laden van het net",
    "plan.orders.holds": "Batterij in reserve voor later (het huis koopt van het net)",
    "plan.orders.empty": "Batterij leeg (het huis koopt van het net)",
    "plan.orders.exports": "☀️ Zonne-overschot om terug te leveren: {items}",
    "plan.orders.sales": "💶 Verkoop aan het net:",
    "plan.orders.room": "🔋 Ruimte maken voor de zon:",
    "plan.orders.solar_delay": "⏳ Laden met zonne-energie vertraagd door het plan",
    # --- plan.py: _no_slots_explanation ---
    "plan.noslots.awaiting_prices.headline": (
        "Later laden nodig, maar de prijzen zijn nog niet gepubliceerd"
    ),
    "plan.noslots.awaiting_prices.hint": (
        "De {kwh:.2f} kWh is nodig na de laatste gepubliceerde prijs, dus er kan nog "
        "geen slot tegen worden afgewogen. Zodra de prijzen van de volgende dag "
        "gepubliceerd zijn, wordt opnieuw gepland."
    ),
    "plan.noslots.cheaper_to_import.headline": (
        "Laden nodig, maar nu laden vanaf het net loont niet"
    ),
    "plan.noslots.cheaper_to_import.hint": (
        "De {kwh:.2f} kWh inkopen op het moment van gebruik is goedkoper dan hem nu "
        "opslaan, na rendementsverliezen en de arbitragemarge. Verlaag de minimale "
        "arbitragemarge om toch te laden."
    ),
    "plan.noslots.capacity.headline": (
        "Laden nodig, maar de resterende slots halen het niet op tijd"
    ),
    "plan.noslots.capacity.hint": (
        "De prijsslots die vóór het moment van gebruik overblijven zijn te weinig of "
        "te kort bij het beschikbare laadvermogen. Controleer het maximale "
        "laadvermogen en het contractvermogen, of wacht op de volgende prijsdag."
    ),
    "plan.noslots.no_power.headline": (
        "Laden nodig, maar het beschikbare laadvermogen is nul"
    ),
    "plan.noslots.no_power.hint": (
        "Controleer het maximale laadvermogen, het contractvermogen en de piekbegrenzing."
    ),
    "plan.noslots.no_room.headline": "Later laden nodig, maar nu kan niets worden opgeslagen",
    "plan.noslots.no_room.hint": (
        "De batterij heeft geen ruimte voor de energie voordat die wordt gebruikt, dus "
        "er is geen slot geboekt. Het plan wordt opnieuw opgebouwd naarmate de batterij "
        "leegloopt en bij de volgende dagelijkse beoordeling."
    ),
    "plan.noslots.no_eligible.headline": (
        "Laden nodig, maar er zijn geen prijsslots meer in de planperiode"
    ),
    "plan.noslots.no_eligible.hint": (
        "Controleer de prijssensor: de prijzen van de volgende dag zijn mogelijk nog "
        "niet gepubliceerd."
    ),
    "plan.noslots.default.headline": "Laden nodig, maar geen geldige prijsslots gevonden",
    "plan.noslots.hint.arbitrage": (
        "Het prijsverschil van vandaag is te klein om de rendementsverliezen te dekken "
        "bij de ingestelde arbitragemarge."
    ),
    "plan.noslots.hint.price": "Controleer de prijssensor of verhoog de maximale prijsgrens.",
    "plan.noslots.cheapest": "Goedkoopste slot: {start} voor {price:.4f} {unit}.\n{hint}",
    "plan.noslots.cheapest_ceiling": (
        "Goedkoopste slot: {start} voor {price:.4f} {unit} "
        "(grens {ceiling:.4f} {unit}).\n{hint}"
    ),
    # --- plan.py: _plan_order_lines, its last line ---
    "plan.savings": (
        "💰 Geschatte besparing van het plan: ~{saving:.2f} {cost_unit} (ten opzichte van de batterij zonder plan)"
    ),
    # --- plan.py: format_dynamic_pricing_notification ---
    "plan.price.charge": "laden ≤ {price:.4f} {unit}",
    "plan.price.discharge": "ontladen ≥ {price:.4f} {unit}",
    "plan.price.arbitrage": "arbitragegrens ≤ {price:.4f} {unit}",
    "plan.price.arbitrage_not_binding": (
        "arbitragegrens ≤ {price:.4f} {unit} (niet bindend)"
    ),
    "plan.price.thresholds": "⚙️ Prijsgrenzen: {parts}",
    "plan.pricing.not_needed.title": "Voorspellend laden: prijsoptimalisatie - NIET nodig",
    "plan.pricing.deferred.title": (
        "Voorspellend laden: prijsoptimalisatie - nog niets te laden"
    ),
    "plan.pricing.deferred.headline": "✓ De goedkoopste slots voor de benodigde energie liggen morgen",
    "plan.pricing.deferred.footer": (
        "Er is nu geen laden vanaf het net gepland; bij de volgende dagelijkse beoordeling wordt opnieuw gepland."
    ),
    "plan.pricing.import.title": "Voorspellend laden: prijsoptimalisatie - Inkoop van het net is goedkoper",
    "plan.pricing.awaiting.title": "Voorspellend laden: prijsoptimalisatie - Wachten op prijzen",
    "plan.pricing.no_slots.title": "Voorspellend laden: prijsoptimalisatie - Geen slots beschikbaar",
    "plan.pricing.import.headline": (
        "ℹ️ Niet laden vanaf het net: de energie inkopen op het moment van gebruik is goedkoper"
    ),
    "plan.pricing.import.storing": (
        "Nu opslaan zou na de rendementsverliezen meer kosten."
    ),
    "plan.pricing.import.storing_margin": (
        "Nu opslaan zou na de rendementsverliezen en de arbitragemarge van "
        "{margin:.4f} {unit} meer kosten."
    ),
    "plan.pricing.warning": "⚠️ {headline}",
    "plan.pricing.hours": "{hours:g}u",
    "plan.pricing.hours_slots": "{hours:g}u ({slots})",
    "plan.pricing.slots": {"one": "{count} slot", "other": "{count} slots"},
    "plan.pricing.selected.title": "Voorspellend laden: prijsoptimalisatie - {hours} geselecteerd",
    "plan.pricing.floor.title": (
        "Voorspellend laden: prijsoptimalisatie + minimale SOC - {hours} geselecteerd"
    ),
    "plan.pricing.negative.title": "Laden bij negatieve prijs: {hours} geselecteerd",
    "plan.pricing.negative.opportunistic": "⚡ Geplande opportunistische lading: {kwh:.2f} kWh",
    "plan.pricing.negative.no_deficit": "Er is geen voorspeld energietekort.",
    "plan.pricing.negative.header": "💰 Meest negatieve slots die in aanmerking komen:",
    "plan.pricing.info.title": "Voorspellend laden: prijsinfo - {hours} goedkoopste",
    "plan.pricing.info.no_charging": "✓ Geen laden vanaf het net nodig {horizon}",
    "plan.pricing.info.header": "💰 Goedkoopste uren vandaag (ter info):",
    "plan.pricing.info.footer": "Er wordt niet geladen.",
    "plan.pricing.opportunity": "⚡ Kans bij negatieve prijs: {kwh:.2f} kWh",
    "plan.pricing.deadline": "🕒 Nodig vóór de deadlines: {kwh:.2f} kWh",
    "plan.pricing.flexible": "💰 Flexibel op prijs: {kwh:.2f} kWh",
    "plan.pricing.tomorrow": (
        "📅 Gepland in de slots van morgen (ingepland om 00:05): {kwh:.2f} kWh"
    ),
    "plan.pricing.import_left": (
        "🔌 Aan het net overgelaten (inkopen op het moment van gebruik is goedkoper): {kwh:.2f} kWh"
    ),
    "plan.pricing.cannot_deliver": (
        "⚠️ Kan {kwh:.2f} kWh niet leveren vóór de deadline ({reason})."
    ),
    "plan.pricing.selected_deadline": "Geselecteerde slots (goedkoopste die elke deadline halen)",
    "plan.pricing.selected_hours": "Geselecteerde uren (goedkoopste)",
    "plan.pricing.grid_charge_hours": (
        "🔌 Geplande lading vanaf het net: {kwh:.2f} kWh → {hours:.1f}u laden nodig"
    ),
    "plan.pricing.slots_header": "💰 {label}:",
    "plan.pricing.average_price": "Gemiddelde prijs: {price:.4f} {unit}",
    "plan.pricing.estimated_cost": "Geschatte kosten: ~{cost:.2f} {cost_unit}",
    # Pieces of one slot line (a price slot row); `extras` joins the optional ones.
    "plan.slot.line": "  • {span} → {price:.4f} {unit}{extras}",
    "plan.slot.kwh": ", {kwh:.2f} kWh",
    "plan.slot.before": " [vóór {time}]",
    "plan.slot.flexible": " [flexibel]",
    "plan.slot.purpose": " [{purpose}]",
    "plan.slot.purpose_extra": " + {purpose}",
    "plan.table.header": "| Slot | Prijs | Energie | Kosten |",
    "plan.table.power": "Vermogen",
    "plan.table.price": "{price:.4f} {unit}",
    "plan.table.cost": "~{cost:.2f} {cost_unit}",
    "plan.table.total": "**Totaal**",
    "plan.table.kwh": "{kwh:.2f} kWh",
    # --- plan.py: format_slot_start_notification ---
    "slot_start.title": "Voorspellend laden: GESTART ({price:.4f} {unit})",
    "slot_start.charging": "⚡ Laden met maximaal {power:g}W",
    "slot_start.slot": "Slot: {span}",
    # English keeps the phase 3 text ("slot(s)"); other languages use their plural.
    "slot_start.next": {
        "one": "Volgend slot: {time} · nog {count} slot over",
        "other": "Volgend slot: {time} · nog {count} slots over",
    },
    "slot_start.last": "Laatste slot · Vandaag geen slots meer",
    # --- plan.py: format_dp_pre_slot_reevaluation_notification ---
    "pre_slot.title": "Voorspellend laden: slot {time} bevestigd — laden nodig",
    "pre_slot.slot": "Slot: {span} @ {price:.4f} {unit}",
    "pre_slot.activate": "→ Het laden start om {time}",
    # --- switch.py: predictive charging override and manual mode ---
    "switch.override.title": "Voorspellend laden: uitgeschakeld",
    "switch.override.paused": (
        "Voorspellend laden vanaf het net is gepauzeerd. Zet de schakelaar weer aan om te hervatten."
    ),
    "switch.override.disabled": (
        "Voorspellend laden is nu uitgeschakeld. Het start niet wanneer het "
        "tijdslot actief wordt."
    ),
    "switch.manual.title": "Handmatige modus: actief",
    "switch.manual.message": (
        "De automatische laad-/ontlaadregeling is gepauzeerd. "
        "Alle batterijen zijn op rust (0W) gezet. "
        "Je kunt nu elke batterij handmatig bedienen met de "
        "regelaars 'Geforceerd laad-/ontlaadvermogen instellen', of — "
        "bij batterijen waarvan de eigen integratie meer "
        "bedrijfsmodi biedt — daar een andere modus kiezen "
        "zonder dat OmniBattery die terugzet.\n\n"
        "Schakel de handmatige modus uit om de automatische regeling te hervatten."
    ),
    # --- infra/alarm_notifier.py: AlarmNotifier._send (driver alarm labels stay English) ---
    "alarm.title.fault": "🚨 Batterijstoring: {device}",
    "alarm.title.warning": "⚠️ Batterijwaarschuwing: {device}",
    "alarm.header.fault": "🚨 Storing gedetecteerd op {device}",
    "alarm.header.warning": "⚠️ Waarschuwing gedetecteerd op {device}",
    "alarm.new_faults": "🆕 Nieuwe storingen:",
    "alarm.new_alarms": "🆕 Nieuwe alarmen:",
    "alarm.also_active": "⚡ Ook actief:",
    # --- tracking/balance_monitor.py: title and `issues` lines ---
    "balance.title": "{icon} Celbalans: {name}",
    "balance.high": "Delta: {delta:.0f} mV — grote onbalans tussen de cellen.",
    "balance.moderate": "Delta: {delta:.0f} mV — matige onbalans.",
    "balance.consecutive": (
        "Grote celonbalans ({delta:.0f} mV) bij {count} opeenvolgende volledige ladingen. "
        "Overweeg een actieve-balancering-blueprint voor deze batterij te "
        "gebruiken om de cellen weer in balans te brengen."
    ),
    "balance.trend": (
        "Stijgende onbalanstrend: +{slope:.1f} mV/meting, "
        "gem. {avg:.0f} mV over de laatste metingen."
    ),
    # --- control/weekly_full_charge.py: solar outlook warning ---
    "weekly.title": "Wekelijkse volledige lading: te weinig zon",
    "weekly.not_enough_solar": (
        "De resterende zonne-energie van vandaag ({net:.1f} kWh na huisverbruik) "
        "dekt de {needed:.1f} kWh niet die nog nodig is om 100% te bereiken. "
        "Als Voorspellend laden uit staat, laadt de wekelijkse volledige lading "
        "niet vanaf het net. Schakel Voorspellend laden in, of laat een "
        "automatisering reageren op het attribuut solar_sufficient van de "
        "sensor Wekelijkse volledige lading."
    ),
    # --- __init__.py: legacy active-balance migration ---
    "migration.title": "Actieve balancering: migratie gepauzeerd ({name})",
    "migration.message": (
        "De oude geïntegreerde actieve-balanceringsrun kon niet veilig worden "
        "gemigreerd ({error}). De batterij blijft in handmatige modus op 0 W. "
        "Houd hem beschikbaar en laad Omnibattery opnieuw om het nog eens te proberen."
    ),
    # --- driver notices: control paused, firmware, settings, pacing ---
    "battery_control.paused_title": "Batterijaansturing gepauzeerd: {name}",
    "battery_control.realtime_mode_required": (
        "Omnibattery heeft de aansturing van **{name}** gepauzeerd, omdat de "
        "batterij niet in de modus **Realtime** staat.\n\n"
        "Open de app van de batterij en zet de werkmodus op Realtime. Omnibattery neemt "
        "de aansturing dan vanzelf weer over."
    ),
    "battery_control.handover_failed": (
        "Omnibattery kon de aansturing van **{name}** niet overnemen: de batterij "
        "bevestigde geen stand-by na een stopopdracht. Omnibattery blijft de "
        "batterij volgen en probeert het vanzelf opnieuw."
    ),
    "battery_control.unknown_model": (
        "**{name}** meldt modelcode `{model}`, die Omnibattery nog niet ondersteunt. "
        "De batterij wordt gevolgd, maar niet aangestuurd. Meld de modelcode graag, "
        "zodat die kan worden toegevoegd."
    ),
    "battery_control.firmware_title": "Batterijfirmware-update aanbevolen: {name}",
    "battery_control.firmware": (
        "**{name}** draait firmware `{firmware}`. De fabrikant raadt voor de lokale API "
        "minimaal `{minimum}` aan. Uitlezen werkt; werk de batterij bij in de "
        "bijbehorende app als de aansturing niet goed werkt."
    ),
    "battery_control.setting_title": "Batterij-instelling niet toegepast: {name}",
    "battery_control.setting_failed": (
        "Omnibattery kon het {setting} van **{name}** niet op {value} zetten. De "
        "batterij houdt haar huidige waarde, en Omnibattery toont en gebruikt die "
        "waarde weer. Probeer het later opnieuw, of wijzig het in de app van de batterij."
    ),
    "battery_control.min_soc": "minimale SOC",
    "battery_control.max_charge_power": "maximale laadvermogen",
    "battery_control.max_discharge_power": "maximale ontlaadvermogen",
    "battery_control.throttled_title": "Batterij traag: {name}",
    "battery_control.throttled": (
        "**{name}** reageert traag of niet, dus Omnibattery wacht nu {interval:.0f}s "
        "tussen verzoeken. Mogelijk vraagt iets anders de batterij ook uit, zoals een "
        "andere Home Assistant-integratie, of is de netwerkverbinding zwak."
    ),
    "battery_control.official": (
        "\n\nDe officiële integratie van de fabrikant vraagt deze batterij ook uit. "
        "Omnibattery is bedoeld als vervanging: schakel die integratie voor "
        "dit apparaat uit, zodat de batterij haar verzoeken niet hoeft te delen. De "
        "bediening van die integratie (werkmodus, ontlaadlimiet, terugleverlimiet) "
        "schrijft bovendien naar het flashgeheugen van de batterij zodra je die gebruikt."
    ),
    "battery_control.slower": (
        "\n\nGebeurt dit vaker, zet de verzoeksnelheid van **{name}** dan een stap "
        "lager, op **{speed}**: kies in Instellingen → Apparaten & diensten bij "
        "Omnibattery voor Opnieuw configureren."
    ),
    "battery_control.speed_fast": "Snel (2s)",
    "battery_control.speed_normal": "Normaal (3s)",
    "battery_control.speed_conservative": "Conservatief (5s)",
}
