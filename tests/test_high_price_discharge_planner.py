"""Pure tests for high-price discharge, trigger 2 only (#270)."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing.curtailment import BatterySnapshot
from custom_components.omnibattery.pricing.high_price_discharge import (
    HorizonSlot,
    REASON_COVERAGE_GAP,
    REASON_COVERAGE_OVERLAP,
    REASON_DISABLED,
    REASON_INVALID_CONFIGURATION,
    REASON_INVALID_HORIZON_END,
    REASON_NO_PROTECTED_DEMAND,
    STATUS_DISABLED,
    STATUS_FAIL_SAFE,
    STATUS_NO_OPPORTUNITY,
    STATUS_PLANNED,
    plan_high_price_discharge,
)


MADRID = ZoneInfo("Europe/Madrid")
UTC = ZoneInfo("UTC")
DAY = datetime(2026, 6, 1, tzinfo=UTC)


def _slot(
    start_offset_hours: float,
    duration_hours: float = 1.0,
    *,
    export_price: float | None = None,
    import_price: float | None = None,
    consumption_kwh: float = 0.0,
    solar_kwh: float = 0.0,
) -> HorizonSlot:
    start = DAY + timedelta(hours=start_offset_hours)
    end = start + timedelta(hours=duration_hours)
    return HorizonSlot(
        start=start,
        end=end,
        export_price=export_price,
        import_price=import_price,
        consumption_kwh=consumption_kwh,
        solar_kwh=solar_kwh,
    )


def _battery(
    *,
    name: str = "battery-1",
    soc: float = 80.0,
    capacity: float = 10.0,
    max_soc: float = 100.0,
    floor: float = 20.0,
    power: float = 5000.0,
    eligible: bool = True,
    can_discharge: bool = True,
) -> BatterySnapshot:
    return BatterySnapshot(
        name, soc, capacity, max_soc, floor, power, eligible, can_discharge
    )


def _tiled_deficit_slots(hours: int, *, consumption: float = 1.0) -> list[HorizonSlot]:
    """A contiguous run of hourly slots, each with a plain household deficit."""
    return [
        _slot(hour, consumption_kwh=consumption, solar_kwh=0.0)
        for hour in range(hours)
    ]


# ----------------------------------------------------------------------
# Disabled / fail-safe defaults
# ----------------------------------------------------------------------


def test_disabled_by_default_returns_disabled_status():
    plan = plan_high_price_discharge(
        [], now=DAY, horizon_end=DAY + timedelta(hours=6)
    )

    assert plan.status == STATUS_DISABLED
    assert plan.reason == REASON_DISABLED
    assert plan.allocations == ()
    assert plan.total_allocated_kwh == 0.0


def test_disabled_ignores_otherwise_invalid_horizon():
    plan = plan_high_price_discharge(
        [], now=DAY, horizon_end=DAY - timedelta(hours=1), enabled=False
    )

    assert plan.status == STATUS_DISABLED


def test_horizon_end_in_the_past_is_fail_safe():
    plan = plan_high_price_discharge(
        [], now=DAY, horizon_end=DAY - timedelta(hours=1), enabled=True
    )

    assert plan.status == STATUS_FAIL_SAFE
    assert plan.reason == REASON_INVALID_HORIZON_END
    assert plan.allocations == ()


def test_horizon_end_equal_to_now_is_fail_safe():
    plan = plan_high_price_discharge([], now=DAY, horizon_end=DAY, enabled=True)

    assert plan.status == STATUS_FAIL_SAFE
    assert plan.reason == REASON_INVALID_HORIZON_END


def test_naive_horizon_end_against_aware_now_is_fail_safe():
    naive_horizon = datetime(2026, 6, 1, 6, 0)

    plan = plan_high_price_discharge(
        [], now=DAY, horizon_end=naive_horizon, enabled=True
    )

    assert plan.status == STATUS_FAIL_SAFE
    assert plan.reason == REASON_INVALID_HORIZON_END


def test_negative_additional_cost_is_fail_safe():
    plan = plan_high_price_discharge(
        [],
        now=DAY,
        horizon_end=DAY + timedelta(hours=6),
        enabled=True,
        additional_cost_per_kwh=-0.01,
    )

    assert plan.status == STATUS_FAIL_SAFE
    assert plan.reason == REASON_INVALID_CONFIGURATION


def test_negative_export_limit_is_fail_safe():
    plan = plan_high_price_discharge(
        [],
        now=DAY,
        horizon_end=DAY + timedelta(hours=6),
        enabled=True,
        max_export_power_w=-1.0,
    )

    assert plan.status == STATUS_FAIL_SAFE
    assert plan.reason == REASON_INVALID_CONFIGURATION


def test_non_positive_discharge_efficiency_is_fail_safe():
    plan = plan_high_price_discharge(
        [],
        now=DAY,
        horizon_end=DAY + timedelta(hours=6),
        enabled=True,
        discharge_efficiency=0.0,
    )

    assert plan.status == STATUS_FAIL_SAFE
    assert plan.reason == REASON_INVALID_CONFIGURATION


# ----------------------------------------------------------------------
# Coverage
# ----------------------------------------------------------------------


def test_coverage_stopping_short_of_horizon_end_is_fail_safe():
    slots = _tiled_deficit_slots(3)  # covers [0h, 3h) only

    plan = plan_high_price_discharge(
        slots, now=DAY, horizon_end=DAY + timedelta(hours=6), enabled=True
    )

    assert plan.status == STATUS_FAIL_SAFE
    assert plan.reason == REASON_COVERAGE_GAP
    assert plan.allocations == ()


def test_gap_inside_the_window_is_fail_safe():
    slots = [
        _slot(0, 1, consumption_kwh=1.0),
        # Gap: nothing covers [1h, 2h).
        _slot(2, 1, consumption_kwh=1.0),
        _slot(3, 1, consumption_kwh=1.0),
    ]

    plan = plan_high_price_discharge(
        slots, now=DAY, horizon_end=DAY + timedelta(hours=4), enabled=True
    )

    assert plan.status == STATUS_FAIL_SAFE
    assert plan.reason == REASON_COVERAGE_GAP


def test_overlap_inside_the_window_is_fail_safe():
    slots = [
        _slot(0, 1, consumption_kwh=1.0),
        _slot(0.5, 1, consumption_kwh=1.0),  # overlaps the first slot
        _slot(1.5, 1.5, consumption_kwh=1.0),
    ]

    plan = plan_high_price_discharge(
        slots, now=DAY, horizon_end=DAY + timedelta(hours=3), enabled=True
    )

    assert plan.status == STATUS_FAIL_SAFE
    assert plan.reason == REASON_COVERAGE_OVERLAP


def test_no_slots_at_all_is_fail_safe():
    plan = plan_high_price_discharge(
        [], now=DAY, horizon_end=DAY + timedelta(hours=3), enabled=True
    )

    assert plan.status == STATUS_FAIL_SAFE


# ----------------------------------------------------------------------
# Partial current slot / protected demand
# ----------------------------------------------------------------------


def test_partial_current_slot_scales_its_forecast_by_remaining_fraction():
    # The evaluation happens 45 minutes into a 1h slot with 4 kWh consumption
    # and no solar: only the remaining quarter-hour (1 kWh) should count.
    now = DAY + timedelta(minutes=45)
    slots = [
        _slot(0, 1, consumption_kwh=4.0, solar_kwh=0.0),
        _slot(1, 1, consumption_kwh=0.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots, now=now, horizon_end=DAY + timedelta(hours=2), enabled=True
    )

    assert plan.status != STATUS_FAIL_SAFE
    assert plan.protected_demand_kwh == pytest.approx(1.0)


def test_protected_demand_is_not_reduced_by_an_intermediate_surplus():
    slots = [
        _slot(0, 1, consumption_kwh=2.0, solar_kwh=0.0),  # 2 kWh deficit
        _slot(1, 1, consumption_kwh=0.0, solar_kwh=5.0),  # surplus, ignored
        _slot(2, 1, consumption_kwh=3.0, solar_kwh=0.0),  # 3 kWh deficit
    ]

    plan = plan_high_price_discharge(
        slots, now=DAY, horizon_end=DAY + timedelta(hours=3), enabled=True
    )

    assert plan.status != STATUS_FAIL_SAFE
    assert plan.protected_demand_kwh == pytest.approx(5.0)


def test_no_future_deficit_reports_no_protected_demand():
    slots = [_slot(0, 1, consumption_kwh=1.0, solar_kwh=5.0)]

    plan = plan_high_price_discharge(
        slots,
        [_battery()],
        now=DAY,
        horizon_end=DAY + timedelta(hours=1),
        enabled=True,
    )

    assert plan.status == STATUS_NO_OPPORTUNITY
    assert plan.reason == REASON_NO_PROTECTED_DEMAND
    assert plan.protected_demand_kwh == 0.0
    assert plan.allocations == ()


# ----------------------------------------------------------------------
# Worked example from spec.md (#270 criterio 7): 0.24 + 0.19 = 0.43
# ----------------------------------------------------------------------


def test_export_price_equal_to_threshold_is_rejected():
    slots = [
        _slot(0, 1, export_price=0.43, import_price=0.90, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(1, 1, import_price=0.24, consumption_kwh=2.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery()],
        now=DAY,
        horizon_end=DAY + timedelta(hours=2),
        enabled=True,
        additional_cost_per_kwh=0.19,
    )

    assert plan.allocations == ()


def test_export_price_strictly_above_threshold_is_accepted():
    slots = [
        _slot(0, 1, export_price=0.44, import_price=0.90, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(1, 1, import_price=0.24, consumption_kwh=2.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery()],
        now=DAY,
        horizon_end=DAY + timedelta(hours=2),
        enabled=True,
        additional_cost_per_kwh=0.19,
    )

    assert len(plan.allocations) == 1
    allocation = plan.allocations[0]
    assert allocation.threshold == pytest.approx(0.43)
    assert allocation.export_price == pytest.approx(0.44)


def test_earlier_or_simultaneous_import_price_never_raises_the_threshold():
    # The 0.90 price sits in the candidate's own slot; only the 0.24 slot
    # that starts after the candidate's end may enter the threshold.
    slots = [
        _slot(0, 1, export_price=0.44, import_price=0.90, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(1, 1, import_price=0.24, consumption_kwh=2.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery()],
        now=DAY,
        horizon_end=DAY + timedelta(hours=2),
        enabled=True,
        additional_cost_per_kwh=0.19,
    )

    assert len(plan.allocations) == 1
    assert plan.allocations[0].threshold == pytest.approx(0.43)


# ----------------------------------------------------------------------
# Import gaps and import bans block only the affected candidate
# ----------------------------------------------------------------------


def test_import_price_gap_blocks_only_the_dependent_candidate():
    slots = [
        # Candidate A depends on the gap slot and must be blocked.
        _slot(0, 1, export_price=0.50, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(1, 1, import_price=None, consumption_kwh=1.0, solar_kwh=0.0),
        # Candidate B only depends on the last (valid) slot's import price.
        _slot(2, 1, export_price=0.50, import_price=0.10, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(3, 1, import_price=0.10, consumption_kwh=1.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery()],
        now=DAY,
        horizon_end=DAY + timedelta(hours=4),
        enabled=True,
    )

    assert len(plan.allocations) == 1
    assert plan.allocations[0].start == slots[2].start


# ----------------------------------------------------------------------
# 1:1 demand linking
# ----------------------------------------------------------------------


def test_demand_links_sum_to_the_allocation_and_never_touch_earlier_consumption():
    slots = [
        _slot(0, 1, consumption_kwh=1.0, solar_kwh=0.0),  # consumption before any candidate
        _slot(1, 1, export_price=0.50, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(2, 1, import_price=0.01, consumption_kwh=2.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery(soc=90.0, capacity=10.0, floor=20.0)],
        now=DAY,
        horizon_end=DAY + timedelta(hours=3),
        enabled=True,
    )

    assert len(plan.allocations) == 1
    allocation = plan.allocations[0]
    linked_total = sum(link.energy_kwh for link in allocation.demand_links)
    assert linked_total == pytest.approx(allocation.energy_kwh)
    for link in allocation.demand_links:
        assert link.demand_start >= allocation.end
        assert link.demand_start != slots[0].start


def test_no_demand_is_double_assigned_across_candidates():
    # Two candidates, one deficit slot after both. The earlier (higher
    # priced) candidate should claim it; the later candidate gets nothing.
    slots = [
        _slot(0, 1, export_price=0.60, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(1, 1, export_price=0.50, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(2, 1, import_price=0.01, consumption_kwh=1.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery(soc=90.0, capacity=10.0, floor=20.0, power=100000.0)],
        now=DAY,
        horizon_end=DAY + timedelta(hours=3),
        enabled=True,
    )

    total_linked = sum(
        link.energy_kwh
        for allocation in plan.allocations
        for link in allocation.demand_links
    )
    assert total_linked == pytest.approx(1.0)
    # The higher-priced (earlier-ranked) candidate is the one that sold.
    assert plan.allocations[0].export_price == pytest.approx(0.60)
    assert plan.allocations[0].energy_kwh == pytest.approx(1.0)


# ----------------------------------------------------------------------
# Reserve/floor and battery eligibility
# ----------------------------------------------------------------------


def test_reserve_below_the_floor_is_never_counted_as_usable():
    battery = _battery(soc=25.0, capacity=10.0, floor=20.0)
    slots = [
        _slot(0, 1, export_price=0.50, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(1, 1, import_price=0.01, consumption_kwh=5.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots, [battery], now=DAY, horizon_end=DAY + timedelta(hours=2), enabled=True
    )

    # Only 0.5 kWh sits above the floor (25% - 20% of 10 kWh).
    assert plan.usable_energy_kwh == pytest.approx(0.5)
    assert plan.total_allocated_kwh <= plan.usable_energy_kwh + 1e-6


def test_ineligible_battery_contributes_nothing():
    slots = [
        _slot(0, 1, export_price=0.50, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(1, 1, import_price=0.01, consumption_kwh=5.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery(eligible=False)],
        now=DAY,
        horizon_end=DAY + timedelta(hours=2),
        enabled=True,
    )

    assert plan.usable_energy_kwh == 0.0
    assert plan.allocations == ()


def test_non_dischargeable_battery_contributes_nothing():
    slots = [
        _slot(0, 1, export_price=0.50, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(1, 1, import_price=0.01, consumption_kwh=5.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery(can_discharge=False)],
        now=DAY,
        horizon_end=DAY + timedelta(hours=2),
        enabled=True,
    )

    assert plan.usable_energy_kwh == 0.0
    assert plan.allocations == ()


# ----------------------------------------------------------------------
# Ranking and chronological tie-break
# ----------------------------------------------------------------------


def test_higher_export_price_is_ranked_first():
    slots = [
        _slot(0, 1, export_price=0.30, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(1, 1, export_price=0.60, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(2, 1, import_price=0.01, consumption_kwh=0.5, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery(soc=90.0, capacity=10.0, floor=20.0)],
        now=DAY,
        horizon_end=DAY + timedelta(hours=3),
        enabled=True,
    )

    assert len(plan.allocations) == 1
    assert plan.allocations[0].start == slots[1].start


def test_ties_break_chronologically():
    slots = [
        _slot(0, 1, export_price=0.50, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(1, 1, export_price=0.50, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(2, 1, import_price=0.01, consumption_kwh=0.5, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery(soc=90.0, capacity=10.0, floor=20.0)],
        now=DAY,
        horizon_end=DAY + timedelta(hours=3),
        enabled=True,
    )

    assert len(plan.allocations) == 1
    assert plan.allocations[0].start == slots[0].start


# ----------------------------------------------------------------------
# Slot capacity: scarce capacity and a zero export limit
# ----------------------------------------------------------------------


def test_scarce_slot_capacity_limits_the_allocation():
    slots = [
        _slot(0, 1, export_price=0.50, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(1, 1, import_price=0.01, consumption_kwh=5.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery(soc=90.0, capacity=10.0, floor=20.0, power=5000.0)],
        now=DAY,
        horizon_end=DAY + timedelta(hours=2),
        enabled=True,
        max_export_power_w=300.0,  # tight net-export ceiling
    )

    assert len(plan.allocations) == 1
    assert plan.allocations[0].energy_kwh == pytest.approx(0.3)


def test_zero_export_limit_forbids_selling():
    slots = [
        _slot(0, 1, export_price=0.50, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(1, 1, import_price=0.01, consumption_kwh=5.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery(soc=90.0, capacity=10.0, floor=20.0)],
        now=DAY,
        horizon_end=DAY + timedelta(hours=2),
        enabled=True,
        max_export_power_w=0.0,
    )

    assert plan.allocations == ()
    assert plan.status == STATUS_NO_OPPORTUNITY


# ----------------------------------------------------------------------
# 15- and 60-minute slot mixes
# ----------------------------------------------------------------------


def test_mixed_15_and_60_minute_slots_are_each_evaluated_at_their_own_duration():
    slots = [
        _slot(0, 0.25, export_price=0.50, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(0.25, 0.25, export_price=0.10, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(0.5, 0.5, import_price=0.01, consumption_kwh=1.0, solar_kwh=0.0),
        _slot(1, 1, import_price=0.01, consumption_kwh=1.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery(soc=90.0, capacity=10.0, floor=20.0, power=100000.0)],
        now=DAY,
        horizon_end=DAY + timedelta(hours=2),
        enabled=True,
        max_export_power_w=2000.0,
    )

    # Both quarter-hour candidates clear their threshold, and each is sized by
    # its own 15 minutes at the 2 kW ceiling, not by the hourly grid.
    assert [a.start for a in plan.allocations] == [slots[0].start, slots[1].start]
    for allocation in plan.allocations:
        assert allocation.energy_kwh == pytest.approx(0.5)
        assert allocation.power_w == pytest.approx(2000.0)


# ----------------------------------------------------------------------
# DST transitions (Europe/Madrid)
# ----------------------------------------------------------------------


def test_spring_forward_dst_transition_keeps_real_slot_durations():
    # 2026-03-29: 02:00 CET -> 03:00 CEST. Madrid wall clock jumps an hour, so
    # consecutive slots must be built from absolute instants; the planner must
    # see two real hours, not the one hour the wall clock shows.
    start = datetime(2026, 3, 29, 0, 0, tzinfo=UTC).astimezone(MADRID)
    middle = datetime(2026, 3, 29, 1, 0, tzinfo=UTC).astimezone(MADRID)
    end = datetime(2026, 3, 29, 2, 0, tzinfo=UTC).astimezone(MADRID)
    assert (middle.hour, end.hour) == (3, 4)  # the 02:00 hour never happens

    slots = [
        HorizonSlot(start, middle, 0.50, 0.01, 0.0, 0.0),
        HorizonSlot(middle, end, None, 0.01, 1.0, 0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery(soc=90.0, capacity=10.0, floor=20.0, power=400.0)],
        now=start,
        horizon_end=end,
        enabled=True,
    )

    assert plan.status == STATUS_PLANNED
    assert len(plan.allocations) == 1
    # One real hour at 400 W, even though the wall clock reads 01:00 -> 03:00.
    assert plan.allocations[0].energy_kwh == pytest.approx(0.4)


def test_fall_back_dst_transition_keeps_real_slot_durations():
    # 2026-10-25: 03:00 CEST -> 02:00 CET, so the 02:00 wall-clock hour happens
    # twice. Both instances must count as a real hour each.
    start = datetime(2026, 10, 25, 0, 0, tzinfo=UTC).astimezone(MADRID)
    middle = datetime(2026, 10, 25, 1, 0, tzinfo=UTC).astimezone(MADRID)
    end = datetime(2026, 10, 25, 2, 0, tzinfo=UTC).astimezone(MADRID)
    assert (start.hour, middle.hour) == (2, 2)  # the repeated hour
    assert start.utcoffset() != middle.utcoffset()

    slots = [
        HorizonSlot(start, middle, 0.50, 0.01, 0.0, 0.0),
        HorizonSlot(middle, end, None, 0.01, 1.0, 0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery(soc=90.0, capacity=10.0, floor=20.0, power=400.0)],
        now=start,
        horizon_end=end,
        enabled=True,
    )

    assert plan.status == STATUS_PLANNED
    assert len(plan.allocations) == 1
    # The repeated wall-clock hour is still one real hour of export.
    assert plan.allocations[0].energy_kwh == pytest.approx(0.4)


# ----------------------------------------------------------------------
# Plan helpers
# ----------------------------------------------------------------------


def test_allocation_at_and_remaining_kwh():
    slots = [
        _slot(0, 1, export_price=0.50, import_price=0.01, consumption_kwh=0.0, solar_kwh=0.0),
        _slot(1, 1, import_price=0.01, consumption_kwh=2.0, solar_kwh=0.0),
    ]

    plan = plan_high_price_discharge(
        slots,
        [_battery(soc=90.0, capacity=10.0, floor=20.0)],
        now=DAY,
        horizon_end=DAY + timedelta(hours=2),
        enabled=True,
    )

    allocation = plan.allocations[0]
    assert plan.allocation_at(slots[0].start) is allocation
    assert plan.allocation_at(slots[1].start) is None
    assert plan.remaining_kwh(slots[0].start) == pytest.approx(allocation.energy_kwh)
    assert plan.remaining_kwh(slots[0].end) == 0.0
