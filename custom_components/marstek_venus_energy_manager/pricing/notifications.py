"""Pure formatters for predictive-charging / dynamic-pricing notifications.

Extracted verbatim from ``ChargeDischargeController`` (module-8 PR2). Each
function builds a ``(title, message)`` pair from already-gathered data; the
controller keeps the thin ``_send_*`` wrappers that read its config and push the
``persistent_notification`` to Home Assistant. No side effects here.
"""
from __future__ import annotations


def format_predictive_notification_message(
    decision_data: dict,
    is_daily_evaluation: bool = False,
    *,
    max_contracted_power,
    max_charge_capacity,
    charging_time_slot,
) -> tuple[str, str]:
    """Format notification title and message from decision data.

    Args:
        decision_data: Dict from _should_activate_grid_charging() with energy balance data
        is_daily_evaluation: True when called from daily evaluation in automation_slots mode

    Returns:
        tuple: (title, message)
    """
    from datetime import time as dt_time

    should_charge = decision_data["should_charge"]
    solar_forecast = decision_data["solar_forecast_kwh"]
    usable_energy = decision_data["usable_energy_kwh"]
    avg_soc = decision_data["avg_soc"]
    avg_consumption = decision_data["avg_consumption_kwh"]
    total_available = decision_data["total_available_kwh"]
    energy_deficit = decision_data["energy_deficit_kwh"]
    days_in_history = decision_data["days_in_history"]

    solar_str = f"{solar_forecast:.2f} kWh" if solar_forecast is not None else "unavailable"
    consumption_str = (
        f"{avg_consumption:.2f} kWh (default)" if days_in_history == 0
        else f"{avg_consumption:.2f} kWh ({days_in_history}-day avg)"
    )
    effective_power = min(max_contracted_power, max_charge_capacity)
    power_str = (
        f"{effective_power}W (ICP: {max_contracted_power}W, batteries: {max_charge_capacity}W)"
    )

    # Safe mode: no solar forecast
    if solar_forecast is None:
        title = "Predictive Charging: Safe mode"
        message = (
            f"⚠️ No solar forecast available — conservative mode\n\n"
            f"🔋 Battery: {avg_soc:.0f}% ({usable_energy:.2f} kWh usable)\n"
            f"📊 Consumption: {consumption_str}\n\n"
            f"Grid charging NOT activated."
        )
        return (title, message)

    # Sufficient energy — no charging needed
    if not should_charge:
        title = "Predictive Charging: Not required"
        message = (
            f"✓ Sufficient energy for today\n\n"
            f"🔋 Battery: {avg_soc:.0f}% ({usable_energy:.2f} kWh usable)\n"
            f"☀️ Solar forecast: {solar_str}\n"
            f"📊 Consumption: {consumption_str}\n"
            f"✅ Available: {total_available:.2f} kWh ≥ {avg_consumption:.2f} kWh needed\n\n"
            f"No grid charging required."
        )
        return (title, message)

    # Charging needed
    try:
        start_time = dt_time.fromisoformat(charging_time_slot["start_time"])
        end_time = dt_time.fromisoformat(charging_time_slot["end_time"])
        slot_str = f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}"
    except Exception:
        slot_str = None

    if is_daily_evaluation:
        title = "Predictive Charging: Expected today"
        timing_line = "⏰ Charging will activate when prices are low\n"
    else:
        title = "Predictive Charging: STARTED"
        timing_line = (
            f"⏰ Charging until: {end_time.strftime('%H:%M')}\n"
            if slot_str else "⏰ Charging now from grid\n"
        )

    grid_charge = decision_data.get("grid_charge_kwh")
    solar_surplus = decision_data.get("solar_surplus_kwh")
    if grid_charge is not None and solar_surplus is not None:
        # When charging triggers, solar_surplus ≤ gap_to_max, so solar will contribute exactly solar_surplus to battery
        charge_split_line = (
            f"🔌 Grid: {grid_charge:.2f} kWh — solar will charge the remaining {solar_surplus:.2f} kWh\n"
        )
    else:
        charge_split_line = f"⚡ Deficit: {energy_deficit:.2f} kWh\n"

    message = (
        f"⚡ Energy deficit — grid charging needed\n\n"
        f"🔋 Battery: {avg_soc:.0f}% ({usable_energy:.2f} kWh usable)\n"
        f"☀️ Solar forecast: {solar_str}\n"
        f"📊 Consumption: {consumption_str}\n"
        f"{charge_split_line}\n"
        f"{timing_line}"
        f"Max charge power: {power_str}"
    )

    return (title, message)


def format_dynamic_pricing_notification(
    decision_data: dict,
    schedule,
    *,
    unit: str,
    max_price_threshold,
    max_contracted_power,
    max_charge_capacity,
) -> tuple[str, str]:
    """Format dynamic pricing evaluation notification."""
    avg_soc = decision_data.get("avg_soc", 0)
    usable_energy = decision_data.get("usable_energy_kwh", 0)
    solar_forecast = decision_data.get("solar_forecast_kwh")
    avg_consumption = decision_data.get("avg_consumption_kwh", 0)
    energy_deficit = decision_data.get("energy_deficit_kwh", 0)
    days_in_history = decision_data.get("days_in_history", 0)

    solar_str = f"{solar_forecast:.2f} kWh" if solar_forecast is not None else "N/A"
    consumption_str = (
        f"{avg_consumption:.2f} kWh ({days_in_history}-day avg)"
        if days_in_history > 0 else f"{avg_consumption:.2f} kWh (default)"
    )

    if schedule is None or not schedule.selected_slots:
        if not decision_data.get("should_charge", False):
            title = "Predictive Charging: Price Optimization - NOT needed"
            message = (
                f"✓ Sufficient energy for today\n\n"
                f"🔋 Battery: {avg_soc:.0f}% ({usable_energy:.2f} kWh usable)\n"
                f"☀️ Solar forecast: {solar_str}\n"
                f"📊 Consumption: {consumption_str}\n\n"
                f"✅ Available: {decision_data.get('total_available_kwh', 0):.2f} kWh ≥ {avg_consumption:.2f} kWh needed\n"
                f"No grid charging required."
            )
        else:
            title = "Predictive Charging: Price Optimization - No slots available"
            message = (
                f"⚠️ Charging needed but no valid price slots found\n\n"
                f"🔋 Battery: {avg_soc:.0f}% ({usable_energy:.2f} kWh usable)\n"
                f"☀️ Solar forecast: {solar_str}\n"
                f"📊 Consumption: {consumption_str}\n"
                f"⚡ Energy deficit: {energy_deficit:.2f} kWh\n\n"
                f"Check price sensor or raise max price threshold."
            )
    else:
        hours_needed = schedule.hours_needed
        n_slots = len(schedule.selected_slots)
        slots_label = f"{n_slots} slot{'s' if n_slots != 1 else ''}" if n_slots != int(hours_needed) else ""
        hours_label = f"{hours_needed:.1f}h" + (f" ({slots_label})" if slots_label else "")
        title = f"Predictive Charging: Price Optimization - {hours_label} selected"

        cost_unit = unit.split("/")[0]  # "€/kWh" → "€", "CHF" → "CHF"
        slot_lines = "\n".join(
            f"  • {s.start.strftime('%H:%M')}-{s.end.strftime('%H:%M')} → {s.price:.4f} {unit}"
            for s in schedule.selected_slots
        )
        threshold_line = (
            f"Max price limit: {max_price_threshold:.4f} {unit}\n"
            if max_price_threshold is not None else ""
        )
        if not schedule.charging_needed:
            title = f"Predictive Charging: Price Info - {hours_label} cheapest"
            message = (
                f"✓ No grid charging needed today\n\n"
                f"🔋 Battery: {avg_soc:.0f}% ({usable_energy:.2f} kWh usable)\n"
                f"☀️ Solar forecast: {solar_str}\n"
                f"📊 Consumption: {consumption_str}\n"
                f"✅ Available: {decision_data.get('total_available_kwh', 0):.2f} kWh ≥ {decision_data.get('avg_consumption_kwh', 0):.2f} kWh needed\n\n"
                f"💰 Cheapest hours today (informational):\n{slot_lines}\n\n"
                f"Average price: {schedule.average_price:.4f} {unit}\n"
                f"{threshold_line}"
                f"No charging will activate."
            )
        else:
            message = (
                f"🔋 Battery: {avg_soc:.0f}% ({usable_energy:.2f} kWh usable)\n"
                f"☀️ Solar forecast: {solar_str}\n"
                f"📊 Consumption: {consumption_str}\n"
                f"⚡ Energy deficit: {energy_deficit:.2f} kWh → {hours_needed:.1f}h of charging needed\n\n"
                f"💰 Selected hours (cheapest):\n{slot_lines}\n\n"
                f"Average price: {schedule.average_price:.4f} {unit}\n"
                f"Estimated cost: ~{schedule.estimated_cost:.2f} {cost_unit}\n"
                f"{threshold_line}"
                f"Max charge power: {min(max_contracted_power, max_charge_capacity)}W "
                f"(ICP: {max_contracted_power}W, batteries: {max_charge_capacity}W)"
            )

    return (title, message)


def format_slot_start_notification(
    slot,
    schedule,
    *,
    unit: str,
    max_contracted_power,
) -> tuple[str, str]:
    """Format the 'cheap pricing slot started' notification.

    ``schedule`` must be non-None (the caller guards on it).
    """
    remaining_slots = [
        s for s in schedule.selected_slots if s.start > slot.start
    ]
    next_slot_str = (
        f"Next slot: {remaining_slots[0].start.strftime('%H:%M')}"
        if remaining_slots else "Last slot"
    )
    remaining_str = (
        f"{len(remaining_slots)} slot(s) remaining"
        if remaining_slots else "No more slots today"
    )

    title = f"Predictive Charging STARTED ({slot.price:.4f} {unit})"
    message = (
        f"⚡ Charging at max {max_contracted_power}W\n"
        f"Slot: {slot.start.strftime('%H:%M')}-{slot.end.strftime('%H:%M')}\n"
        f"{next_slot_str} · {remaining_str}"
    )
    return (title, message)


def format_dp_pre_slot_reevaluation_notification(
    slot,
    decision: dict,
    *,
    unit: str,
) -> tuple[str, str]:
    """Format the pre-slot re-evaluation 'charging still needed' notification."""
    avg_soc = decision.get("avg_soc", 0)
    usable_energy = decision.get("usable_energy_kwh", 0)
    solar_forecast = decision.get("solar_forecast_kwh")
    avg_consumption = decision.get("avg_consumption_kwh", 0)
    energy_deficit = decision.get("energy_deficit_kwh", 0)
    days_in_history = decision.get("days_in_history", 0)

    solar_str = f"{solar_forecast:.2f} kWh" if solar_forecast is not None else "N/A"
    consumption_str = (
        f"{avg_consumption:.2f} kWh ({days_in_history}-day avg)"
        if days_in_history > 0 else f"{avg_consumption:.2f} kWh (default)"
    )

    title = f"Predictive Charging: slot {slot.start.strftime('%H:%M')} confirmed — charging needed"
    message = (
        f"🔋 Battery: {avg_soc:.0f}% ({usable_energy:.2f} kWh usable)\n"
        f"☀️ Solar forecast: {solar_str}\n"
        f"📊 Consumption: {consumption_str}\n"
        f"⚡ Energy deficit: {energy_deficit:.2f} kWh\n\n"
        f"Slot: {slot.start.strftime('%H:%M')}–{slot.end.strftime('%H:%M')} "
        f"@ {slot.price:.4f} {unit}\n"
        f"→ Charging will activate at {slot.start.strftime('%H:%M')}"
    )
    return (title, message)


def format_evening_recharge_notification(
    deficit_kwh: float,
    slots: list,
    *,
    unit: str,
    avg_soc: float,
) -> tuple[str, str]:
    """Format the evening re-evaluation notification."""
    slots_str = ", ".join(
        f"{s.start.strftime('%H:%M')}-{s.end.strftime('%H:%M')} ({s.price:.4f} {unit})"
        for s in slots
    )
    title = "Predictive Charging: Evening re-evaluation"
    message = (
        f"☀️ Solar ending — batteries not full ({avg_soc:.0f}% avg)\n"
        f"⚡ Deficit: {deficit_kwh:.2f} kWh\n\n"
        f"Cheap slots scheduled:\n{slots_str}"
    )
    return (title, message)
