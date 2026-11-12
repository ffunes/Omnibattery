"""The pricing engine reads "now" from one place: ``PricingManager._now``.

Overriding it must be enough to pin the time, with no ``datetime`` patching.
The pinned instant is years away from the real clock, so any code path that
still reads the system time fails these tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import PRICE_INTEGRATION_TIBBER
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.engine import PricingManager

_NOW = datetime(2031, 3, 12, 14, 30)  # a Wednesday


def _hour_slot(hour: int, day_offset: int = 0) -> PriceSlot:
    start = _NOW.replace(hour=hour, minute=0) + timedelta(days=day_offset)
    return PriceSlot(start, start + timedelta(hours=1), 0.10)


def _manager(ctrl) -> PricingManager:
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._now = lambda: _NOW
    return manager


def test_now_override_controls_the_active_dynamic_pricing_slot():
    ctrl = SimpleNamespace(
        _dynamic_pricing_schedule=SimpleNamespace(selected_slots=[_hour_slot(14)])
    )
    manager = _manager(ctrl)

    assert manager.is_in_dynamic_pricing_slot() is True

    manager._now = lambda: _NOW + timedelta(hours=1)
    assert manager.is_in_dynamic_pricing_slot() is False


def test_now_override_controls_the_future_slot_filter():
    ended, running, later, tomorrow = (
        _hour_slot(13), _hour_slot(14), _hour_slot(20), _hour_slot(9, day_offset=1)
    )
    ctrl = SimpleNamespace(
        price_integration_type=PRICE_INTEGRATION_TIBBER,
        price_sensor=None,
        _tibber_price_slots=[ended, running, later, tomorrow],
        _price_data_status=None,
    )
    manager = _manager(ctrl)

    # Default horizon is the end of the pinned day.
    assert manager._parse_price_data(quiet=True) == [running, later]
    assert ctrl._price_data_status == "ok (2 slots)"
    assert manager._parse_price_data(
        horizon_end=_NOW + timedelta(days=1), quiet=True
    ) == [running, later, tomorrow]

    manager._now = lambda: _NOW + timedelta(days=2)
    assert manager._parse_price_data(quiet=True) == []
    assert ctrl._price_data_status == "no_future_slots"


def test_controller_charging_window_follows_the_pricing_clock():
    window = {"days": ["wed"], "start_time": "14:00", "end_time": "15:00"}
    ctrl = SimpleNamespace(
        charging_time_slots=[window],
        _time_in_window=ChargeDischargeController._time_in_window,
        _pricing_mgr=SimpleNamespace(_now=lambda: _NOW),
    )

    assert ChargeDischargeController._active_charging_slot(ctrl) is window

    ctrl._pricing_mgr = SimpleNamespace(_now=lambda: _NOW + timedelta(hours=2))
    assert ChargeDischargeController._active_charging_slot(ctrl) is None
