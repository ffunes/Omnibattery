"""Characterization tests for the pure pricing notification formatters (PR2).

Pin the current (title, message) output of the formatters extracted from
``ChargeDischargeController`` into ``pricing.notifications`` so the move is
proven cero-cambio-funcional. Pure functions: no hass, no controller — schedule
is a SimpleNamespace exposing only the attributes the formatters read.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from custom_components.marstek_venus_energy_manager.pricing import (
    PriceSlot,
    notifications,
)

_DAY = datetime(2999, 1, 1, 18, 0)


def _decision(**overrides):
    base = {
        "should_charge": False,
        "solar_forecast_kwh": 3.0,
        "usable_energy_kwh": 5.0,
        "avg_soc": 60.0,
        "avg_consumption_kwh": 8.0,
        "total_available_kwh": 9.0,
        "energy_deficit_kwh": 0.0,
        "days_in_history": 7,
    }
    base.update(overrides)
    return base


def _slots(prices):
    return [
        PriceSlot(start=_DAY + timedelta(minutes=15 * i), end=_DAY + timedelta(minutes=15 * (i + 1)), price=p)
        for i, p in enumerate(prices)
    ]


def _schedule(prices, *, hours_needed=1.0, charging_needed=False, estimated_cost=0.5):
    slots = _slots(prices)
    return SimpleNamespace(
        hours_needed=hours_needed,
        selected_slots=slots,
        average_price=sum(prices) / len(prices),
        estimated_cost=estimated_cost,
        charging_needed=charging_needed,
    )


# ----------------------------------------------------------------------
# format_predictive_notification_message
# ----------------------------------------------------------------------

_CFG = dict(max_contracted_power=5000, max_charge_capacity=3000,
            charging_time_slot={"start_time": "23:00", "end_time": "06:00"})


def test_predictive_safe_mode_when_no_solar():
    title, message = notifications.format_predictive_notification_message(
        _decision(solar_forecast_kwh=None, should_charge=True), **_CFG
    )
    assert title == "Predictive Charging: Safe mode"
    assert "conservative mode" in message


def test_predictive_not_required():
    title, message = notifications.format_predictive_notification_message(
        _decision(should_charge=False), **_CFG
    )
    assert title == "Predictive Charging: Not required"
    assert "No grid charging required." in message


def test_predictive_started():
    title, message = notifications.format_predictive_notification_message(
        _decision(should_charge=True, energy_deficit_kwh=2.0), **_CFG
    )
    assert title == "Predictive Charging: STARTED"
    assert "06:00" in message  # end of charging slot
    assert "ICP: 5000W, batteries: 3000W" in message


def test_predictive_daily_evaluation_expected():
    title, _ = notifications.format_predictive_notification_message(
        _decision(should_charge=True, energy_deficit_kwh=2.0), True, **_CFG
    )
    assert title == "Predictive Charging: Expected today"


# ----------------------------------------------------------------------
# format_dynamic_pricing_notification
# ----------------------------------------------------------------------

_DP_CFG = dict(unit="€/kWh", max_price_threshold=0.30,
               max_contracted_power=5000, max_charge_capacity=3000)


def test_dynamic_none_schedule_not_needed():
    title, message = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False), None, **_DP_CFG
    )
    assert title == "Predictive Charging: Price Optimization - NOT needed"


def test_dynamic_none_schedule_no_slots():
    title, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=4.0), None, **_DP_CFG
    )
    assert title == "Predictive Charging: Price Optimization - No slots available"


def test_dynamic_informational_lists_slots():
    schedule = _schedule([0.10, 0.12, 0.11, 0.09], charging_needed=False)
    title, message = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False), schedule, **_DP_CFG
    )
    assert "Price Info" in title and "cheapest" in title
    assert "Cheapest hours today (informational):" in message
    assert message.count("→") == 4  # one bullet per slot
    assert "Max price limit: 0.3000 €/kWh" in message
    assert "No charging will activate." in message


def test_dynamic_charging_shows_cost():
    schedule = _schedule([0.10, 0.12], hours_needed=0.5, charging_needed=True, estimated_cost=0.75)
    title, message = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=1.0), schedule, **_DP_CFG
    )
    assert "selected" in title
    assert "Selected hours (cheapest):" in message
    assert "Estimated cost: ~0.75 €" in message


# ----------------------------------------------------------------------
# slot start / pre-slot / evening
# ----------------------------------------------------------------------

def test_slot_start_notification():
    schedule = _schedule([0.10, 0.12, 0.11])
    first = schedule.selected_slots[0]
    title, message = notifications.format_slot_start_notification(
        first, schedule, unit="€/kWh", max_contracted_power=5000
    )
    assert title == "Predictive Charging STARTED (0.1000 €/kWh)"
    assert "Charging at max 5000W" in message
    assert "2 slot(s) remaining" in message


def test_slot_start_last_slot():
    schedule = _schedule([0.10])
    title, message = notifications.format_slot_start_notification(
        schedule.selected_slots[0], schedule, unit="€/kWh", max_contracted_power=5000
    )
    assert "Last slot" in message and "No more slots today" in message


def test_dp_pre_slot_reevaluation():
    slot = _slots([0.09])[0]
    title, message = notifications.format_dp_pre_slot_reevaluation_notification(
        slot, _decision(energy_deficit_kwh=1.5), unit="€/kWh"
    )
    assert "confirmed" in title
    assert "0.0900 €/kWh" in message
    assert "Charging will activate" in message


def test_evening_recharge():
    slots = _slots([0.08, 0.09])
    title, message = notifications.format_evening_recharge_notification(
        2.5, slots, unit="€/kWh", avg_soc=55.0
    )
    assert title == "Predictive Charging: Evening re-evaluation"
    assert "55% avg" in message
    assert "Deficit: 2.50 kWh" in message
