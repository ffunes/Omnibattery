"""English notification texts: the reference catalogue and the fallback.

Every other language file has the same keys and the same ``{...}`` markers.
Placeholders keep their format specifiers (``{kwh:.2f}``), and each text is a
whole line (or title) so a language can reorder it. A dict value holds plural
forms (``one`` / ``other``) selected by ``count``.
"""

# Monday first, as ``datetime.weekday()``.
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

TEXTS: dict[str, str | dict[str, str]] = {
    # --- plan.py: _forecast_notification_context (horizon, labels, basis) ---
    "plan.horizon.until_tomorrow_midnight": "until tomorrow midnight",
    "plan.horizon.until_day_time": "until {day} {time}",
    "plan.horizon.until_sunrise": "until sunrise ({time})",
    "plan.horizon.until_midnight": "until midnight",
    "plan.phrase.tomorrow": "the rest of today and tomorrow",
    "plan.phrase.tomorrow_until": "the rest of today and tomorrow ({horizon})",
    "plan.phrase.tonight": "the rest of today and tonight {horizon}",
    "plan.phrase.today": "the rest of today",
    "plan.basis.profile": "learned 15-minute profile",
    "plan.basis.profile_days": "learned 15-minute profile ({days} days)",
    "plan.basis.temporary_curve": "temporary daily curve",
    "plan.basis.remaining_average": "remaining-day estimate from {days}-day daily average",
    "plan.basis.remaining_default": "default remaining-day estimate",
    "plan.basis.average": "{days}-day daily average",
    "plan.basis.daily_default": "default daily estimate",
    "plan.basis.adjusted": "{basis}, adjusted with today's consumption",
    "plan.balance.today": "Today",
    "plan.balance.tomorrow": "Tomorrow",
    "plan.balance.night": "Night",
    "plan.balance.total": "Total",
    "plan.balance.solar": "☀️ Solar forecast",
    "plan.balance.solar_margin": "☀️ With margin ({pct:.0f} %)",
    "plan.balance.consumption": "📊 House consumption",
    "plan.balance.battery": "🔋 Battery ({soc:.0f} %)",
    "plan.balance.grid_charge": "🔌 Grid charge",
    "plan.balance.available": "✅ Available (battery + solar)",
    "plan.balance.deficit": "⚡ Energy deficit",
    "plan.balance.grid_need": "⚡ Total grid need",
    "plan.balance.grid_floor": "🔌 Grid charge (guaranteed minimum SOC)",
    "plan.balance.solar_surplus": "☀️ Solar surplus after consumption",
    "plan.balance.import_left": "🔌 Left to the grid",
    # --- plan.py: _build_forecast_lines and shared lines ---
    "plan.line.basis": "   🧮 Basis: {basis}",
    "plan.battery": "🔋 Battery: {soc:.0f}% ({usable:.2f} kWh usable)",
    "plan.horizon_text.today": "today",
    "plan.horizon_text.for": "for {phrase}",
    "plan.sufficient": "✓ Sufficient energy for {horizon}",
    "plan.no_more_charging": "No additional grid charging required.",
    "plan.no_charging": "No grid charging required.",
    # --- plan.py: _limits_table (the power limits the plan used) ---
    "plan.power.header": "⚡ Maximum power",
    "plan.power.charge": "🔌 Charge",
    "plan.power.discharge": "🔋 Discharge",
    "plan.power.contracted": "{power:g}W (Max. grid import)",
    "plan.power.batteries": "{power:g}W (batteries)",
    "plan.power.carried": (
        "Load above the maximum discharge power comes from the grid: {carried:.2f} kWh, left out of the charge"
    ),
    # --- plan.py: format_predictive_notification_message ---
    "plan.safe.title": "Predictive Charging: Safe mode",
    "plan.safe.no_forecast": "⚠️ No solar forecast available — conservative mode",
    "plan.safe.not_activated": "Grid charging NOT activated.",
    "plan.deferred.title": "Predictive Charging: Deferred by timeline",
    "plan.deferred.no_quota": "🕒 This configured window has no energy quota.",
    "plan.deferred.shortfall": "⚠️ Deadline shortfall: {kwh:.2f} kWh",
    "plan.deferred.reserved": "The energy is reserved for another configured window.",
    "plan.not_required.title": "Predictive Charging: Not required",
    "plan.expected_today.title": "Predictive Charging: Expected today",
    "plan.started.title": "Predictive Charging: STARTED",
    "plan.timing.when_cheap": "⏰ Charging will activate when prices are low",
    "plan.timing.until": "⏰ Charging until: {time}",
    "plan.timing.now": "⏰ Charging now from grid",
    "plan.charge.needed": "⚡ Energy deficit — grid charging needed",
    # --- plan.py: day prefix, clock and span ---
    "plan.day_prefix": "{day} {date} · ",
    # --- plan.py: _plan_order_lines ---
    # The one table's title, then its legend (under the table); plan.py
    # puts each row's icon in front.
    "plan.orders.title": "🗓️ Plan by slot:",
    "plan.orders.legend": "Legend:",
    "plan.orders.charge": "Grid charge",
    "plan.orders.holds": "Battery kept for later (the house buys from the grid)",
    "plan.orders.empty": "Battery empty (the house buys from the grid)",
    "plan.orders.exports": "☀️ Solar surplus to export: {items}",
    "plan.orders.sales": "💶 Selling to the grid:",
    "plan.orders.room": "🔋 Making room for the sun:",
    "plan.orders.solar_delay": "⏳ Solar charge delayed by the plan",
    # --- plan.py: _no_slots_explanation ---
    "plan.noslots.awaiting_prices.headline": (
        "Charging needed later, but its prices are not published yet"
    ),
    "plan.noslots.awaiting_prices.hint": (
        "The {kwh:.2f} kWh is needed after the last published price, so no slot "
        "can be valued against it yet. It is planned again as soon as the next "
        "day's prices are published."
    ),
    "plan.noslots.cheaper_to_import.headline": (
        "Charging needed, but charging from the grid now is not worth it"
    ),
    "plan.noslots.cheaper_to_import.hint": (
        "Importing the {kwh:.2f} kWh when it is used is cheaper than storing it "
        "now after round-trip losses and the arbitrage margin. Lower the minimum "
        "arbitrage margin to charge anyway."
    ),
    "plan.noslots.capacity.headline": (
        "Charging needed but the remaining slots cannot deliver it in time"
    ),
    "plan.noslots.capacity.hint": (
        "The price slots left before the energy is needed are too few or too "
        "short at the available charge power. Check the max charge power and "
        "contracted power, or wait for the next price day."
    ),
    "plan.noslots.no_power.headline": (
        "Charging needed but the available charge power is zero"
    ),
    "plan.noslots.no_power.hint": (
        "Check the max charge power, contracted power and peak shaving limit."
    ),
    "plan.noslots.no_room.headline": "Charging needed later, but nothing can be stored now",
    "plan.noslots.no_room.hint": (
        "The battery has no room for the energy before it is used, so no slot is "
        "booked. The plan is rebuilt as the battery drains and at the next daily "
        "evaluation."
    ),
    "plan.noslots.no_eligible.headline": (
        "Charging needed but no price slots remain in the planning horizon"
    ),
    "plan.noslots.no_eligible.hint": (
        "Check the price sensor: the next day's prices may not be published yet."
    ),
    "plan.noslots.default.headline": "Charging needed but no valid price slots found",
    "plan.noslots.hint.arbitrage": (
        "Today's spread is too small to cover round-trip losses at the configured "
        "arbitrage margin."
    ),
    "plan.noslots.hint.price": "Check price sensor or raise max price threshold.",
    "plan.noslots.cheapest": "Cheapest slot: {start} at {price:.4f} {unit}.\n{hint}",
    "plan.noslots.cheapest_ceiling": (
        "Cheapest slot: {start} at {price:.4f} {unit} "
        "(ceiling {ceiling:.4f} {unit}).\n{hint}"
    ),
    # --- plan.py: _plan_order_lines, its last line ---
    "plan.savings": (
        "💰 Estimated saving of the plan: ~{saving:.2f} {cost_unit} (against using the battery with no plan)"
    ),
    # --- plan.py: format_dynamic_pricing_notification ---
    "plan.price.charge": "charge ≤ {price:.4f} {unit}",
    "plan.price.discharge": "discharge ≥ {price:.4f} {unit}",
    "plan.price.arbitrage": "arbitrage ceiling ≤ {price:.4f} {unit}",
    "plan.price.arbitrage_not_binding": (
        "arbitrage ceiling ≤ {price:.4f} {unit} (not binding)"
    ),
    "plan.price.thresholds": "⚙️ Price thresholds: {parts}",
    "plan.pricing.not_needed.title": "Predictive Charging: Price Optimization - NOT needed",
    "plan.pricing.deferred.title": (
        "Predictive Charging: Price Optimization - nothing to charge yet"
    ),
    "plan.pricing.deferred.headline": "✓ The cheapest slots for the energy needed are tomorrow",
    "plan.pricing.deferred.footer": (
        "No grid charging is scheduled now; it is planned again at the next daily evaluation."
    ),
    "plan.pricing.import.title": "Predictive Charging: Price Optimization - Grid import is cheaper",
    "plan.pricing.awaiting.title": "Predictive Charging: Price Optimization - Waiting for prices",
    "plan.pricing.no_slots.title": "Predictive Charging: Price Optimization - No slots available",
    "plan.pricing.import.headline": (
        "ℹ️ No grid charge: buying the energy when it is used is cheaper"
    ),
    "plan.pricing.import.storing": (
        "Storing it now would cost more after the round-trip losses."
    ),
    "plan.pricing.import.storing_margin": (
        "Storing it now would cost more after the round-trip losses and the "
        "{margin:.4f} {unit} arbitrage margin."
    ),
    "plan.pricing.warning": "⚠️ {headline}",
    "plan.pricing.hours": "{hours:g}h",
    "plan.pricing.hours_slots": "{hours:g}h ({slots})",
    "plan.pricing.slots": {"one": "{count} slot", "other": "{count} slots"},
    "plan.pricing.selected.title": "Predictive Charging: Price Optimization - {hours} selected",
    "plan.pricing.floor.title": (
        "Predictive Charging: Price Optimization + Min SOC floor - {hours} selected"
    ),
    "plan.pricing.negative.title": "Negative-price charging: {hours} selected",
    "plan.pricing.negative.opportunistic": "⚡ Opportunistic charge planned: {kwh:.2f} kWh",
    "plan.pricing.negative.no_deficit": "No predictive energy deficit is required.",
    "plan.pricing.negative.header": "💰 Most-negative qualifying slots:",
    "plan.pricing.info.title": "Predictive Charging: Price Info - {hours} cheapest",
    "plan.pricing.info.no_charging": "✓ No grid charging needed {horizon}",
    "plan.pricing.info.header": "💰 Cheapest hours today (informational):",
    "plan.pricing.info.footer": "No charging will activate.",
    "plan.pricing.opportunity": "⚡ Negative-price opportunity: {kwh:.2f} kWh",
    "plan.pricing.deadline": "🕒 Required before deadlines: {kwh:.2f} kWh",
    "plan.pricing.flexible": "💰 Flexible by price: {kwh:.2f} kWh",
    "plan.pricing.tomorrow": (
        "📅 Planned in tomorrow's slots (scheduled at 00:05): {kwh:.2f} kWh"
    ),
    "plan.pricing.import_left": (
        "🔌 Left to the grid (cheaper to import when used): {kwh:.2f} kWh"
    ),
    "plan.pricing.cannot_deliver": (
        "⚠️ Cannot deliver {kwh:.2f} kWh before its deadline ({reason})."
    ),
    "plan.pricing.selected_deadline": "Selected slots (cheapest able to meet each deadline)",
    "plan.pricing.selected_hours": "Selected hours (cheapest)",
    "plan.pricing.grid_charge_hours": (
        "🔌 Grid charge planned: {kwh:.2f} kWh → {hours:.1f}h of charging needed"
    ),
    "plan.pricing.slots_header": "💰 {label}:",
    "plan.pricing.average_price": "Average price: {price:.4f} {unit}",
    "plan.pricing.estimated_cost": "Estimated cost: ~{cost:.2f} {cost_unit}",
    # Pieces of one slot line (a price slot row); `extras` joins the optional ones.
    "plan.slot.line": "  • {span} → {price:.4f} {unit}{extras}",
    "plan.slot.kwh": ", {kwh:.2f} kWh",
    "plan.slot.before": " [before {time}]",
    "plan.slot.flexible": " [flexible]",
    "plan.slot.purpose": " [{purpose}]",
    "plan.slot.purpose_extra": " + {purpose}",
    "plan.table.header": "| Slot | Price | Energy | Cost |",
    "plan.table.power": "Power",
    "plan.table.price": "{price:.4f} {unit}",
    "plan.table.cost": "~{cost:.2f} {cost_unit}",
    "plan.table.total": "**Total**",
    "plan.table.kwh": "{kwh:.2f} kWh",
    # --- plan.py: format_slot_start_notification ---
    "slot_start.title": "Predictive Charging: STARTED ({price:.4f} {unit})",
    "slot_start.charging": "⚡ Charging at max {power:g}W",
    "slot_start.slot": "Slot: {span}",
    # English keeps the phase 3 text ("slot(s)"); other languages use their plural.
    "slot_start.next": {
        "one": "Next slot: {time} · {count} slot(s) remaining",
        "other": "Next slot: {time} · {count} slot(s) remaining",
    },
    "slot_start.last": "Last slot · No more slots today",
    # --- plan.py: format_dp_pre_slot_reevaluation_notification ---
    "pre_slot.title": "Predictive Charging: slot {time} confirmed — charging needed",
    "pre_slot.slot": "Slot: {span} @ {price:.4f} {unit}",
    "pre_slot.activate": "→ Charging will activate at {time}",
    # --- switch.py: predictive charging override and manual mode ---
    "switch.override.title": "Predictive Charging: disabled",
    "switch.override.paused": (
        "Predictive grid charging has been paused. Turn the switch back on to resume."
    ),
    "switch.override.disabled": (
        "Predictive charging is now disabled. It will not activate when the time "
        "slot becomes active."
    ),
    "switch.manual.title": "Manual Mode: active",
    "switch.manual.message": (
        "Automatic charge/discharge control is paused. "
        "All batteries have been set to idle (0W). "
        "You can now manually control each battery using the "
        "'Set Forcible Charge/Discharge Power' controls, or — "
        "for batteries whose own integration offers more "
        "operating modes — select another mode there without "
        "OmniBattery reverting it.\n\n"
        "Turn off Manual Mode to resume automatic control."
    ),
    # --- infra/alarm_notifier.py: AlarmNotifier._send (driver alarm labels stay English) ---
    "alarm.title.fault": "🚨 Battery Fault: {device}",
    "alarm.title.warning": "⚠️ Battery Warning: {device}",
    "alarm.header.fault": "🚨 Fault detected on {device}",
    "alarm.header.warning": "⚠️ Warning detected on {device}",
    "alarm.new_faults": "🆕 New faults:",
    "alarm.new_alarms": "🆕 New alarms:",
    "alarm.also_active": "⚡ Also active:",
    # --- tracking/balance_monitor.py: title and `issues` lines ---
    "balance.title": "{icon} Cell Balance: {name}",
    "balance.high": "Delta: {delta:.0f} mV — high cell imbalance.",
    "balance.moderate": "Delta: {delta:.0f} mV — moderate imbalance.",
    "balance.consecutive": (
        "High cell imbalance ({delta:.0f} mV) for {count} consecutive full charges. "
        "Consider running an active-balance blueprint for this battery to "
        "rebalance the cells."
    ),
    "balance.trend": (
        "Rising imbalance trend: +{slope:.1f} mV/reading, "
        "avg {avg:.0f} mV over last readings."
    ),
    # --- control/weekly_full_charge.py: solar outlook warning ---
    "weekly.title": "Weekly Full Charge: not enough solar",
    "weekly.not_enough_solar": (
        "Today's remaining solar ({net:.1f} kWh after home consumption) "
        "will not cover the {needed:.1f} kWh still needed to reach 100%. "
        "With Predictive Charging off, the weekly full charge does not charge "
        "from the grid. Enable Predictive Charging, or let an automation act on "
        "the solar_sufficient attribute of the Weekly Full Charge sensor."
    ),
    # --- __init__.py: legacy active-balance migration ---
    "migration.title": "Active Balance: migration paused ({name})",
    "migration.message": (
        "The old integrated active-balance run could not be migrated "
        "safely ({error}). The battery is held in manual mode at 0 W. "
        "Keep it available and reload Omnibattery to retry."
    ),
    # --- driver notices: control paused, firmware, settings, pacing ---
    "battery_control.paused_title": "Battery control paused: {name}",
    "battery_control.realtime_mode_required": (
        "Omnibattery paused control of **{name}** because the battery is not in "
        "**Realtime** mode.\n\n"
        "Open the battery's app and set the working mode to Realtime. Omnibattery "
        "then resumes control automatically."
    ),
    "battery_control.handover_failed": (
        "Omnibattery could not take control of **{name}**: the battery did not "
        "confirm standby after a stop command. It keeps monitoring and retries "
        "automatically."
    ),
    "battery_control.unknown_model": (
        "**{name}** reports model code `{model}`, which Omnibattery does not "
        "support yet. It is monitored, but not controlled. Please report the model "
        "code so it can be added."
    ),
    "battery_control.firmware_title": "Battery firmware update recommended: {name}",
    "battery_control.firmware": (
        "**{name}** runs firmware `{firmware}`. The manufacturer recommends at least "
        "`{minimum}` for the local API. Monitoring works; update the battery in "
        "its app if control misbehaves."
    ),
    "battery_control.setting_title": "Battery setting not applied: {name}",
    "battery_control.setting_failed": (
        "Omnibattery could not set the {setting} of **{name}** to {value}. The "
        "battery keeps its current value, and Omnibattery shows and uses that "
        "value again. Try again later, or change it in the battery's app."
    ),
    "battery_control.min_soc": "minimum SOC",
    "battery_control.max_charge_power": "maximum charge power",
    "battery_control.max_discharge_power": "maximum discharge power",
    "battery_control.throttled_title": "Battery slow: {name}",
    "battery_control.throttled": (
        "**{name}** answers slowly or not at all, so Omnibattery now waits "
        "{interval:.0f}s between requests. Something else may be polling the battery "
        "too, such as another Home Assistant integration, or its network connection "
        "may be weak."
    ),
    "battery_control.official": (
        "\n\nThe manufacturer's official integration is also polling this battery. "
        "Omnibattery is meant to replace it: disable that integration for "
        "this device to free the battery's request budget. Its controls (energy "
        "mode, discharge limit, feed-in limit) also write to the battery's flash "
        "memory whenever they are used."
    ),
    "battery_control.slower": (
        "\n\nIf this keeps happening, lower the request speed of **{name}** one step, "
        "to **{speed}**: in Settings → Devices & services, choose Reconfigure on "
        "Omnibattery."
    ),
    "battery_control.speed_fast": "Fast (2s)",
    "battery_control.speed_normal": "Normal (3s)",
    "battery_control.speed_conservative": "Conservative (5s)",
}
