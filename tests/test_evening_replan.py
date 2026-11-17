"""The evening re-evaluation re-plans the slots it keeps (P9).

The evening pass built a chronological plan from the live SOC over every
slot left in the horizon, but only added its new slots: kept slots kept their
00:05 quota (charged on top of the new evening slots), a kept slot the new
plan no longer needed fell back to the whole deficit, and energy the plan put
in a kept slot was dropped.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.omnibattery.pricing import (
    SLOT_PURPOSE_COMBINED,
    SLOT_PURPOSE_DEFICIT,
    SLOT_PURPOSE_NEGATIVE_PRICE,
    DynamicPricingSchedule,
    PriceSlot,
)
from custom_components.omnibattery.pricing.chronological import (
    ChronologicalPlan,
    SlotAllocation,
)
from custom_components.omnibattery.pricing.engine import PricingManager

NOW = datetime(2026, 11, 9, 19, 0)


def _slot(hour, day=0):
    start = datetime(2026, 11, 9 + day, hour)
    return PriceSlot(start, start + timedelta(hours=1), 0.1)


def _schedule(slots, purposes, targets):
    return DynamicPricingSchedule(
        hours_needed=len(slots),
        selected_slots=list(slots),
        average_price=0.1,
        estimated_cost=0.0,
        total_available_slots=24,
        evaluation_time=NOW - timedelta(hours=19),
        energy_deficit_kwh=sum(targets.values()),
        slot_purposes=dict(purposes),
        slot_energy_targets_kwh=dict(targets),
        chronological_planning_active=True,
    )


def _plan(*allocations):
    return ChronologicalPlan(
        allocations=[
            SlotAllocation(slot, kwh, slot.end + timedelta(hours=6), "deadline")
            for slot, kwh in allocations
        ]
    )


def test_kept_slots_take_the_new_plan_quota_or_leave():
    running = _slot(18)  # charging now: keeps its quota
    late = _slot(22)  # still needed, but less
    night = _slot(2, day=1)  # no longer needed
    combined = _slot(3, day=1)  # also a negative-price slot
    new = _slot(20)  # the evening pass's own slot
    schedule = _schedule(
        [running, late, night, combined],
        {
            running: SLOT_PURPOSE_DEFICIT,
            late: SLOT_PURPOSE_DEFICIT,
            night: SLOT_PURPOSE_DEFICIT,
            combined: SLOT_PURPOSE_COMBINED,
        },
        {running: 1.0, late: 3.0, night: 2.0, combined: 1.5},
    )

    kept = PricingManager._replan_kept_deficit_slots(
        schedule,
        [running, late, night, combined],
        _plan((running, 0.4), (late, 1.2), (new, 2.5)),
        now=NOW,
    )

    assert kept == [running, late, combined]
    targets = schedule.slot_energy_targets_kwh
    assert targets[running] == 1.0  # running slot untouched
    assert targets[late] == 1.2  # re-planned, not the 00:05 3.0
    assert targets[new] == 2.5
    assert night not in targets and combined not in targets
    assert schedule.slot_purposes[combined] == SLOT_PURPOSE_NEGATIVE_PRICE
    assert night not in schedule.slot_purposes
    assert schedule.chronological_planning_active is True


def test_negative_price_slots_are_left_alone():
    negative = _slot(23)
    schedule = _schedule(
        [negative], {negative: SLOT_PURPOSE_NEGATIVE_PRICE}, {}
    )

    kept = PricingManager._replan_kept_deficit_slots(
        schedule, [negative], _plan(), now=NOW
    )

    assert kept == [negative]
    assert schedule.slot_purposes[negative] == SLOT_PURPOSE_NEGATIVE_PRICE
