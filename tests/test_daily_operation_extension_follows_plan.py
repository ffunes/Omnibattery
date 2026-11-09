"""The Daily Operation chart reaches as far after midnight as the plan does.

It showed a fixed 12 hours of the next day. The discharge reserve can plan to
the first slot where the sun covers the house, which in winter is past noon,
and once tomorrow's prices are out they reach midnight. The part after
midnight now runs to the latest of the planner's energy horizon, the
reserve's horizon and, with dynamic pricing, the last published price: at
least to noon, at most the whole next day. Without price data the plan alone
decides.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.sensor import _timeline_horizon
from tests.test_daily_operation_manager import MADRID, FakeStore, MutableClock, _manager

NOW = datetime(2026, 8, 23, 10, 7, tzinfo=MADRID)
PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")


def _extension_item(index):
    start = datetime(2026, 8, 24, tzinfo=MADRID) + timedelta(minutes=15 * index)
    return {
        "extension_index": index,
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=15)).isoformat(),
        "consumption_kwh": 0.2,
    }


def _projection(count, *indexes):
    return {
        "extended_intervals": [_extension_item(index) for index in indexes],
        "extended_horizon": {"interval_count": count},
    }


def test_the_extension_runs_as_far_as_the_projection_reaches():
    manager = _manager(MutableClock(NOW))
    manager.rebuild_future_projection(_projection(60, 0, 50, 59, 60), mode="dynamic_pricing")

    snapshot = manager.build_public_snapshot()
    horizon = snapshot["extended_horizon"]

    assert horizon["interval_count"] == 60
    assert horizon["end"].startswith("2026-08-24T15:00")
    assert len(horizon["duration_s"]) == len(horizon["price_per_kwh"]) == 60
    assert [item["extension_index"] for item in snapshot["extended_projection"]] == [0, 50, 59]


@pytest.mark.parametrize(("published", "kept"), [(None, 48), (10, 48), (200, 96)])
def test_the_extension_stays_between_noon_and_midnight(published, kept):
    manager = _manager(MutableClock(NOW))
    projection = _projection(published, 0)
    if published is None:
        del projection["extended_horizon"]
    manager.rebuild_future_projection(projection, mode="dynamic_pricing")

    assert manager.build_public_snapshot()["extended_horizon"]["interval_count"] == kept


@pytest.mark.asyncio
async def test_the_extension_length_survives_a_restart():
    clock = MutableClock(NOW)
    store = FakeStore()
    manager = _manager(clock, store)
    manager.rebuild_future_projection(_projection(64, 60), mode="dynamic_pricing")
    await manager.async_save_all()

    restored = _manager(clock, store)
    assert await restored.async_load() is True
    snapshot = restored.build_public_snapshot()

    assert snapshot["extended_horizon"]["interval_count"] == 64
    assert snapshot["extended_projection"][0]["extension_index"] == 60


@pytest.mark.asyncio
async def test_a_store_from_before_keeps_twelve_hours():
    clock = MutableClock(NOW)
    store = FakeStore()
    manager = _manager(clock, store)
    manager.rebuild_future_projection(_projection(64, 4, 60), mode="dynamic_pricing")
    await manager.async_save_all()
    del store.data["extended_interval_count"]

    restored = _manager(clock, store)
    assert await restored.async_load() is True
    snapshot = restored.build_public_snapshot()

    assert snapshot["extended_horizon"]["interval_count"] == 48
    assert [item["extension_index"] for item in snapshot["extended_projection"]] == [4]


def _controller(energy_end=None, reserve_end=None, prices_until=None, mode="dynamic_pricing"):
    def price_slots_between(start, end):
        if prices_until is None:
            return []
        slots, slot_start = [], datetime(2026, 8, 23)
        while slot_start < prices_until:
            slot_end = slot_start + timedelta(minutes=15)
            if slot_end > start and slot_start < end:
                slots.append(SimpleNamespace(start=slot_start, end=slot_end, price=0.3))
            slot_start = slot_end
        return slots

    return SimpleNamespace(
        _last_decision_data={"energy_horizon_end": energy_end},
        _discharge_reserve_mgr=SimpleNamespace(plan_horizon_end=lambda: reserve_end),
        _pricing_mgr=SimpleNamespace(price_slots_between=price_slots_between),
        predictive_charging_mode=mode,
    )


@pytest.mark.parametrize(
    ("energy_end", "reserve_end", "hours"),
    [
        (None, None, 12),  # nothing planned past the morning
        ("2026-08-24T07:24:46", None, 12),  # sunrise: noon is the floor
        ("2026-08-24T07:24:46", datetime(2026, 8, 24, 12, 45), 13),  # rounded up
        (datetime(2026, 8, 24, 16, 0), None, 16),
        ("2026-08-25T03:00:00", None, 24),  # never past the next day
    ],
)
def test_the_controller_takes_the_later_horizon(energy_end, reserve_end, hours):
    controller = _controller(energy_end, reserve_end)

    assert ChargeDischargeController._daily_operation_extension_hours(controller, NOW) == hours


SUNRISE = "2026-08-24T07:24:46"


@pytest.mark.parametrize(
    ("prices_until", "mode", "hours"),
    [
        (datetime(2026, 8, 25), "dynamic_pricing", 24),  # tomorrow's prices are out
        (datetime(2026, 8, 24, 14, 0), "dynamic_pricing", 14),  # published up to 14:00
        (datetime(2026, 8, 24), "dynamic_pricing", 12),  # today only: the plan decides
        (None, "dynamic_pricing", 12),  # no price data at all
        (datetime(2026, 8, 25), "time_slot", 12),  # no market prices in this mode
        (datetime(2026, 8, 25), None, 12),  # predictive charging without a mode
    ],
)
def test_published_prices_carry_the_extension(prices_until, mode, hours):
    controller = _controller(SUNRISE, None, prices_until, mode)

    assert ChargeDischargeController._daily_operation_extension_hours(controller, NOW) == hours


def test_the_plan_still_counts_when_prices_end_earlier():
    controller = _controller(SUNRISE, datetime(2026, 8, 24, 16, 0), datetime(2026, 8, 24, 13, 0))

    assert ChargeDischargeController._daily_operation_extension_hours(controller, NOW) == 16


@pytest.mark.parametrize(
    "pricing",
    [
        None,  # no pricing manager
        SimpleNamespace(),  # a planner without the display helper
        SimpleNamespace(price_slots_between=lambda start, end: 1 / 0),  # broken source
    ],
)
def test_a_missing_or_broken_price_source_keeps_the_plan(pricing):
    controller = _controller(SUNRISE, datetime(2026, 8, 24, 13, 30))
    controller._pricing_mgr = pricing

    assert ChargeDischargeController._daily_operation_extension_hours(controller, NOW) == 14


def test_the_sensor_pads_to_the_published_length():
    horizon = _timeline_horizon({"interval_count": 60, "price_per_kwh": [0.4] * 50})

    assert horizon["interval_count"] == 60
    assert len(horizon["price_per_kwh"]) == 60
    assert horizon["price_per_kwh"][49] == 0.4
    assert horizon["price_per_kwh"][59] is None


def test_the_panel_sizes_the_chart_and_centres_now():
    panel = PANEL.read_text(encoding="utf-8")

    # The chart follows the published span instead of a fixed 36 hours.
    assert "extensionHorizon.interval_count" in panel
    assert "repeat(var(--daily-op-hours, 36), minmax(0, 1fr))" in panel
    assert 'ref.stage.style.setProperty("--daily-op-hours"' in panel
    # "Now" goes to the middle of the first view, also when that reaches past
    # midnight; the view no longer stops at the end of today.
    assert "now - ref.viewport.clientWidth / 2" in panel
    assert "baseMax" not in panel
