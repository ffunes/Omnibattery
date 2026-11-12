"""Days without consumption history count as 80 % of the fleet's capacity."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import DEFAULT_BASE_CONSUMPTION_KWH
from custom_components.omnibattery.tracking.consumption_tracker import ConsumptionTracker
from tests.conftest import FakeCoordinator

D = DEFAULT_BASE_CONSUMPTION_KWH


def _tracker(history, capacities=()):
    coords = [FakeCoordinator(data={"battery_total_energy": c} if c is not None else None)
              for c in capacities]
    t = ConsumptionTracker.__new__(ConsumptionTracker)
    t._controller = SimpleNamespace(
        _daily_consumption_history=history, coordinators=coords,
    )
    t.is_vacation_active = lambda: False
    return t


def _days(*values):
    today = date(2026, 1, 10)
    return [(today - timedelta(days=len(values) - i), v) for i, v in enumerate(values)]


def test_empty_history_uses_fleet_share():
    t = _tracker([], [5.0, 5.0])
    assert asyncio.run(t.get_dynamic_base_consumption()) == pytest.approx(8.0)


def test_sentinels_count_as_fleet_share():
    t = _tracker(_days(D, D, D, D, D, 12.0, 14.0), [10.0])
    expected = (12 + 14 + 5 * 8.0) / 7
    assert asyncio.run(t.get_dynamic_base_consumption()) == pytest.approx(expected)
    assert t.get_avg_daily_consumption() == pytest.approx(expected)


def test_empty_history_avg_uses_fleet_share():
    assert _tracker([], [10.0]).get_avg_daily_consumption() == pytest.approx(8.0)


def test_unknown_capacity_keeps_default():
    t = _tracker([], [None])
    assert asyncio.run(t.get_dynamic_base_consumption()) == D
    assert t.get_avg_daily_consumption() == D
    assert _tracker([], [0.0]).get_avg_daily_consumption() == D


def test_stored_history_and_real_count_unchanged():
    hist = _days(D, D, D, D, D, 12.0, 14.0)
    t = _tracker(hist, [10.0])
    asyncio.run(t.get_dynamic_base_consumption())
    assert [c for _, c in t._controller._daily_consumption_history].count(D) == 5
    assert sum(1 for _, c in hist if c != D) == 2
