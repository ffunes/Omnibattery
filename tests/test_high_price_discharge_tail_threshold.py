"""Trigger-2 buy-back follows the end of chronologically covered demand."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing.curtailment import BatterySnapshot
from custom_components.omnibattery.pricing.high_price_discharge import (
    HorizonSlot,
    plan_high_price_discharge,
)


DAY = datetime(2026, 6, 1, tzinfo=ZoneInfo("UTC"))


def _slot(hour, *, price=None, buy=None, consumption=0.0):
    start = DAY + timedelta(hours=hour)
    return HorizonSlot(start, start + timedelta(hours=1), price, buy, consumption, 0.0)


def _battery(*, usable=3.0):
    return BatterySnapshot("battery", 20 + usable * 10, 10.0, 100.0, 20.0, 5000.0, True, True)


def _plan(slots, *, usable=3.0, **kwargs):
    return plan_high_price_discharge(
        slots, [_battery(usable=usable)], now=DAY,
        horizon_end=slots[-1].end, enabled=True,
        additional_cost_per_kwh=0.05, **kwargs,
    )


def test_peak_is_covered_but_cheap_runout_tail_is_sold():
    slots = [_slot(0, price=.5, buy=.1), _slot(1, buy=.8, consumption=2),
             _slot(2, buy=.1, consumption=2), _slot(3, buy=.1, consumption=1)]
    plan = _plan(slots)
    assert len(plan.allocations) == 1
    allocation = plan.allocations[0]
    assert allocation.energy_kwh == pytest.approx(1)
    assert allocation.threshold == pytest.approx(.15)
    assert [(link.demand_start, link.energy_kwh) for link in allocation.demand_links] == [
        (slots[2].start, pytest.approx(1))]
    assert _plan(slots, chronological_coverage=False).allocations == ()


def test_walking_backward_stops_at_expensive_covered_peak():
    slots = [_slot(0, price=.5, buy=.1), _slot(1, buy=.8, consumption=2),
             _slot(2, buy=.1, consumption=1), _slot(3, buy=.1, consumption=2)]
    allocation, = _plan(slots).allocations
    assert allocation.energy_kwh == pytest.approx(1)
    assert allocation.threshold == pytest.approx(.15)
    assert [link.demand_start for link in allocation.demand_links] == [slots[2].start]


def test_no_runout_keeps_original_plan():
    slots = [_slot(0, price=.5, buy=.1), _slot(1, buy=.1, consumption=1),
             _slot(2, buy=.3, consumption=1)]
    new = _plan(slots)
    old = _plan(slots, chronological_coverage=False)
    assert new == old
    assert new.allocations[0].threshold == pytest.approx(.35)
    assert [link.demand_start for link in new.allocations[0].demand_links] == [slots[1].start, slots[2].start]


def test_two_candidates_share_one_reverse_chain_without_double_linking():
    slots = [_slot(0, price=.6, buy=.1), _slot(1, price=.5, buy=.1),
             _slot(2, buy=.1, consumption=1), _slot(3, buy=.2, consumption=1),
             _slot(4, buy=.1, consumption=2)]
    plan = _plan(slots, usable=2.5, max_export_power_w=1000)
    assert [a.start for a in plan.allocations] == [slots[0].start, slots[1].start]
    assert [a.threshold for a in plan.allocations] == pytest.approx([.25, .25])
    links = [link for a in plan.allocations for link in a.demand_links]
    assert [(link.demand_start, link.energy_kwh) for link in links] == [
        (slots[4].start, pytest.approx(.5)), (slots[3].start, pytest.approx(.5)),
        (slots[3].start, pytest.approx(.5)), (slots[2].start, pytest.approx(.5))]
    assert sum(link.energy_kwh for link in links) <= 2.5
    assert sum(link.energy_kwh for link in links if link.demand_start == slots[3].start) == pytest.approx(1)


def test_chain_head_before_candidate_end_blocks_candidate():
    slots = [_slot(0, buy=.1, consumption=1), _slot(1, price=.5, buy=.1),
             _slot(2, price=.6, buy=.1), _slot(3, buy=.1, consumption=2)]
    plan = _plan(slots, usable=1.0)
    assert plan.allocations == ()


def test_unprofitable_head_is_not_skipped_for_cheaper_earlier_demand():
    slots = [_slot(0, price=.5, buy=.1), _slot(1, buy=.1, consumption=1),
             _slot(2, buy=.8, consumption=1), _slot(3, buy=.1, consumption=2)]
    allocation, = _plan(slots, usable=2.5).allocations
    assert allocation.energy_kwh == pytest.approx(.5)
    assert [link.demand_start for link in allocation.demand_links] == [slots[3].start]


def test_missing_later_import_still_blocks_candidate():
    slots = [_slot(0, price=.5, buy=.1), _slot(1, buy=None, consumption=1),
             _slot(2, buy=.1, consumption=2)]
    assert _plan(slots, usable=1).allocations == ()
