"""Dutch notification texts. Same keys and ``{...}`` markers as ``en``."""

# Monday first, as ``datetime.weekday()``.
DAYS = ("ma", "di", "wo", "do", "vr", "za", "zo")

TEXTS: dict[str, str | dict[str, str]] = {
    # --- plan.py: _forecast_notification_context (horizon, labels, basis) ---
    "plan.horizon.until_tomorrow_midnight": "tot morgenmiddernacht",
    "plan.horizon.until_day_time": "tot {day} {time}",
    "plan.horizon.until_sunrise": "tot zonsopgang ({time})",
    "plan.horizon.until_midnight": "tot middernacht",
    "plan.overnight.tomorrow": "Waarvan morgen (00:00-24:00)",
    "plan.overnight.after_midnight": "Waarvan na middernacht ({horizon})",
    "plan.overnight.until_sunrise": "Nacht tot zonsopgang",
    "plan.phrase.tomorrow": "de rest van vandaag en morgen",
    "plan.phrase.tomorrow_until": "de rest van vandaag en morgen ({horizon})",
    "plan.phrase.tonight": "de rest van vandaag en vannacht {horizon}",
    "plan.phrase.today": "de rest van vandaag",
    "plan.consumption.remaining": "Resterend huisverbruik {horizon}",
    "plan.consumption.expected_horizon": "Verwacht huisverbruik {horizon}",
    "plan.consumption.expected_today": "Verwacht huisverbruik vandaag",
    "plan.basis.profile": "geleerd profiel per kwartier",
    "plan.basis.profile_days": "geleerd profiel per kwartier ({days} dagen)",
    "plan.basis.temporary_curve": "tijdelijke dagcurve",
    "plan.basis.remaining_average": "schatting voor de rest van de dag op basis van het daggemiddelde over {days} dagen",
    "plan.basis.remaining_default": "standaardschatting voor de rest van de dag",
    "plan.basis.average": "daggemiddelde over {days} dagen",
    "plan.basis.daily_default": "standaard dagschatting",
    "plan.basis.adjusted": "{basis}, aangepast met het verbruik van vandaag",
    "plan.solar.remaining": "Resterende zonne-energie vandaag",
    "plan.solar.forecast": "Zonneprognose vandaag",
    "plan.solar.tomorrow": "Zonne-energie morgen",
    "plan.solar.after_midnight": "Zonne-energie na middernacht ({horizon})",
    # --- plan.py: _build_forecast_lines and shared lines ---
    "plan.line.solar": "☀️ {label}: {value}",
    "plan.line.solar_kwh": "☀️ {label}: {kwh:.2f} kWh",
    "plan.line.consumption": "📊 {label}: {kwh:.2f} kWh",
    "plan.line.basis": "   🧮 Basis: {basis}",
    "plan.line.overnight": "🌙 {label}: {kwh:.2f} kWh",
    "plan.battery": "🔋 Batterij: {soc:.0f}% ({usable:.2f} kWh bruikbaar)",
    "plan.solar_unavailable": "niet beschikbaar",
    "plan.solar_na": "n.v.t.",
    "plan.horizon_text.today": "vandaag",
    "plan.horizon_text.for": "voor {phrase}",
    "plan.energy_deficit": "⚡ Energietekort: {kwh:.2f} kWh",
    "plan.available": (
        "✅ Beschikbaar voor deze periode: {available:.2f} kWh ≥ {needed:.2f} kWh nodig"
    ),
    "plan.sufficient": "✓ Voldoende energie voor {horizon}",
    "plan.no_more_charging": "Geen extra laden vanaf het net nodig.",
    "plan.no_charging": "Geen laden vanaf het net nodig.",
    "plan.max_charge_power": "Maximaal laadvermogen: {power}",
    # --- plan.py: _max_charge_power_str / _max_discharge_power_line ---
    "plan.power.contracted": "contractvermogen: {power:g}W",
    "plan.power.icp": "ICP: {power:g}W",
    "plan.power.batteries": "batterijen: {power:g}W",
    "plan.power.summary": "{limit:g}W ({parts})",
    "plan.max_discharge": "Maximaal ontlaadvermogen: {limit:g}W",
    "plan.max_discharge_carried": (
        "Maximaal ontlaadvermogen: {limit:g}W (belasting daarboven komt van het net: "
        "{carried:.2f} kWh, niet meegerekend in het laden)"
    ),
    # --- plan.py: format_predictive_notification_message ---
    "plan.safe.title": "Voorspellend laden: veilige modus",
    "plan.safe.no_forecast": "⚠️ Geen zonneprognose beschikbaar — voorzichtige modus",
    "plan.safe.not_activated": "Laden vanaf het net is NIET geactiveerd.",
    "plan.deferred.title": "Voorspellend laden: uitgesteld door tijdschema",
    "plan.deferred.no_quota": "🕒 Dit ingestelde venster heeft geen energiekwota.",
    "plan.deferred.aggregate_need": "⚡ Totale behoefte vanaf het net: {kwh:.2f} kWh",
    "plan.deferred.shortfall": "⚠️ Tekort voor de deadline: {kwh:.2f} kWh",
    "plan.deferred.reserved": "De energie is gereserveerd voor een ander ingesteld venster.",
    "plan.not_required.title": "Voorspellend laden: niet nodig",
    "plan.expected_today.title": "Voorspellend laden: verwacht vandaag",
    "plan.started.title": "Voorspellend laden: GESTART",
    "plan.timing.when_cheap": "⏰ Het laden start zodra de prijzen laag zijn",
    "plan.timing.until": "⏰ Laden tot: {time}",
    "plan.timing.now": "⏰ Nu laden vanaf het net",
    "plan.charge.floor": "🔌 Net: {kwh:.2f} kWh om de gegarandeerde minimale SOC te bereiken",
    "plan.charge.planned": "🔌 Geplande lading vanaf het net: {kwh:.2f} kWh",
    "plan.charge.surplus_remaining": (
        "☀️ Verwacht resterend zonne-overschot: {kwh:.2f} kWh (na het verwachte verbruik)"
    ),
    "plan.charge.surplus": (
        "☀️ Verwacht zonne-overschot: {kwh:.2f} kWh (na het verwachte verbruik)"
    ),
    "plan.charge.deficit": "⚡ Tekort: {kwh:.2f} kWh",
    "plan.charge.needed": "⚡ Energietekort — laden vanaf het net nodig",
    # --- plan.py: day prefix, clock and span ---
    "plan.day_prefix": "{day} {date} · ",
    # --- plan.py: _plan_order_lines ---
    "plan.orders.holds_one": "🔒 Batterij in reserve voor later: {item}",
    "plan.orders.holds_many": "🔒 Batterij in reserve voor later:",
    "plan.orders.exports": "☀️ Zonne-overschot om terug te leveren: {items}",
    "plan.orders.sales_one": "💶 Verkoop aan het net: {item}",
    "plan.orders.sales_many": "💶 Verkoop aan het net:",
    "plan.orders.at_kw": "{span} met {kw:.1f} kW",
    "plan.orders.bullet": "  • {item}",
    "plan.orders.room": "🔋 Ruimte maken voor de zon: {items}",
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
    # --- plan.py: _savings_line ---
    "plan.savings": (
        "Deze energie inkopen op het moment van gebruik: ~{alternative:.2f} {cost_unit}. "
        "Geschatte besparing: ~{saving:.2f} {cost_unit}"
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
    "plan.pricing.deferred.needed": "⚡ Benodigde energie: {kwh:.2f} kWh",
    "plan.pricing.deferred.footer": (
        "Er is nu geen laden vanaf het net gepland; bij de volgende dagelijkse beoordeling wordt opnieuw gepland."
    ),
    "plan.pricing.import.title": "Voorspellend laden: prijsoptimalisatie - Inkoop van het net is goedkoper",
    "plan.pricing.awaiting.title": "Voorspellend laden: prijsoptimalisatie - Wachten op prijzen",
    "plan.pricing.no_slots.title": "Voorspellend laden: prijsoptimalisatie - Geen slots beschikbaar",
    "plan.pricing.import.headline": (
        "ℹ️ Niet laden vanaf het net: de energie inkopen op het moment van gebruik is goedkoper"
    ),
    "plan.pricing.import.left": "🔌 Aan het net overgelaten: {kwh:.2f} kWh",
    "plan.pricing.import.storing": (
        "Nu opslaan zou na de rendementsverliezen meer kosten."
    ),
    "plan.pricing.import.storing_margin": (
        "Nu opslaan zou na de rendementsverliezen en de arbitragemarge van "
        "{margin:.4f} {unit} meer kosten."
    ),
    "plan.pricing.warning": "⚠️ {headline}",
    "plan.pricing.hours": "{hours:.1f}u",
    "plan.pricing.hours_slots": "{hours:.1f}u ({slots})",
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
    "plan.pricing.selected_slots": "Laadslots",
    "plan.pricing.grid_charge_hours": (
        "🔌 Geplande lading vanaf het net: {kwh:.2f} kWh → {hours:.1f}u laden nodig"
    ),
    "plan.pricing.grid_charge": "🔌 Geplande lading vanaf het net: {kwh:.2f} kWh",
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
    "plan.table.header": "| Slot | Prijs | Energie |",
    "plan.table.row": "| {span} | {price:.4f} {unit} | {energy} |",
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
}
