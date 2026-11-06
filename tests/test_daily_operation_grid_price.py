"""The daily operation timeline carries the grid price of each interval.

Display only: the panel shows it in the tooltip and as a stepped line. The
price comes from the same source as the planner, past slots included, and
reading it leaves the planner's price-data status alone.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.sensor import _daily_operation_timeline_attributes

from tests.test_daily_operation_manager import MutableClock, _manager

_NOW = datetime(2026, 11, 6, 14, 10)


def _hourly(start: datetime, prices: list[float]) -> list[PriceSlot]:
    return [
        PriceSlot(start + timedelta(hours=i), start + timedelta(hours=i + 1), price)
        for i, price in enumerate(prices)
    ]


def _day_slots() -> list[PriceSlot]:
    # Today 00:00 to 24:00 at 0.20 + hour/100, tomorrow 00:00 to 12:00 at 0.40.
    today = datetime(2026, 11, 6)
    return _hourly(today, [0.20 + hour / 100 for hour in range(24)]) + _hourly(
        today + timedelta(days=1), [0.40] * 12
    )


def _timeline():
    from zoneinfo import ZoneInfo

    clock = MutableClock(_NOW.replace(tzinfo=ZoneInfo("Europe/Madrid")))
    return _manager(clock)


def test_every_interval_takes_the_price_of_its_slot_today_and_after_midnight():
    timeline = _timeline()

    assert timeline.update_prices(_day_slots()) is True

    snapshot = timeline.build_public_snapshot()
    today = snapshot["series"]["price_per_kwh"]
    tomorrow = snapshot["extended_horizon"]["price_per_kwh"]
    assert len(today) == 96 and len(tomorrow) == 48
    # Past intervals keep their price: 00:00 and 09:45 are before 14:10.
    assert today[0] == 0.20
    assert today[39] == 0.29
    assert today[4 * 14] == 0.34
    assert today[95] == 0.43
    assert tomorrow == [0.40] * 48


def test_quarter_hour_slots_and_missing_prices():
    timeline = _timeline()
    start = datetime(2026, 11, 6, 18, 0)
    slots = [
        PriceSlot(start + timedelta(minutes=15 * i), start + timedelta(minutes=15 * (i + 1)), price)
        for i, price in enumerate([0.51, 0.52, -0.01, 0.54])
    ]

    timeline.update_prices(slots)

    today = timeline.build_public_snapshot()["series"]["price_per_kwh"]
    assert today[72:76] == [0.51, 0.52, -0.01, 0.54]
    assert today[71] is None and today[76] is None
    assert timeline.build_public_snapshot()["extended_horizon"]["price_per_kwh"] == [None] * 48


def test_unchanged_prices_do_not_republish_the_timeline():
    timeline = _timeline()
    timeline.update_prices(_day_slots())
    revision = timeline.build_public_snapshot()["revision"]

    assert timeline.update_prices(_day_slots()) is False
    assert timeline.build_public_snapshot()["revision"] == revision

    assert timeline.update_prices([]) is True
    snapshot = timeline.build_public_snapshot()
    assert snapshot["revision"] > revision
    assert snapshot["series"]["price_per_kwh"] == [None] * 96


def test_the_entity_exposes_both_price_series():
    timeline = _timeline()
    timeline.update_prices(_day_slots())

    attributes = _daily_operation_timeline_attributes(
        SimpleNamespace(daily_operation_timeline=timeline)
    )

    assert attributes["series"]["price_per_kwh"][0] == 0.20
    assert attributes["series"]["price_per_kwh"][95] == 0.43
    assert attributes["extended_horizon"]["price_per_kwh"] == [0.40] * 48


def _pricing(slots):
    controller = SimpleNamespace(
        price_integration_type="tibber",
        price_sensor=None,
        _tibber_price_slots=list(slots),
        _price_data_status="ok (40 slots)",
    )
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Madrid"),
        states=SimpleNamespace(get=lambda _entity: None),
    )
    return controller, PricingManager(hass, controller)


def test_display_prices_include_past_slots_and_leave_the_status_alone():
    controller, pricing = _pricing(_day_slots())
    day_start = datetime(2026, 11, 6)

    slots = pricing.price_slots_between(day_start, day_start + timedelta(hours=36))

    assert len(slots) == 36
    assert slots[0].start == day_start
    assert slots[-1].end == day_start + timedelta(hours=36)
    assert controller._price_data_status == "ok (40 slots)"


def test_display_prices_without_data_leave_the_status_alone():
    controller, pricing = _pricing([])

    assert pricing.price_slots_between(datetime(2026, 11, 6), datetime(2026, 11, 7)) == []
    assert controller._price_data_status == "ok (40 slots)"


def test_the_controller_clears_prices_outside_dynamic_pricing():
    received = []
    timeline = SimpleNamespace(update_prices=lambda slots: received.append(list(slots)))
    _controller, pricing = _pricing(_day_slots())
    controller = SimpleNamespace(_pricing_mgr=pricing, predictive_charging_mode="time_slot")

    ChargeDischargeController._daily_operation_update_prices(controller, timeline, _NOW)
    controller.predictive_charging_mode = "dynamic_pricing"
    ChargeDischargeController._daily_operation_update_prices(controller, timeline, _NOW)

    assert received[0] == []
    assert len(received[1]) == 36
