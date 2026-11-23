"""Pure tests for surplus discharge with solar-refill pricing (#270)."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing.curtailment import BatterySnapshot
from custom_components.omnibattery.pricing.high_price_discharge import (
    HorizonSlot,
    REASON_DISABLED,
    REASON_NO_ELIGIBLE_CANDIDATES,
    REASON_NO_REFILL_PRICE,
    REASON_NO_SURPLUS,
    REASON_PLANNED,
    STATUS_NO_OPPORTUNITY,
    REFILL_SEARCH_STEP_KWH,
    STATUS_PLANNED,
    _solar_refill_price,
    plan_high_price_discharge,
)


UTC = ZoneInfo("UTC")
MADRID = ZoneInfo("Europe/Madrid")
DAY = datetime(2026, 6, 1, tzinfo=UTC)


def _slot(hour, *, price=None, buy=None, consumption=0.0, solar=0.0, length=1.0):
    start = DAY + timedelta(hours=hour)
    return HorizonSlot(start, start + timedelta(hours=length), price, buy, consumption, solar)


def _battery(*, soc=80.0, capacity=10.0, floor=20.0, max_soc=100.0,
             power=5000.0, eligible=True):
    return BatterySnapshot("battery", soc, capacity, max_soc, floor, power, eligible, True)


def _plan(slots, *, batteries=None, fill=None, horizon=2, **kwargs):
    return plan_high_price_discharge(
        slots, [_battery()] if batteries is None else batteries,
        now=DAY, horizon_end=DAY + timedelta(hours=horizon),
        fill_slots=[_slot(horizon, price=0.10, solar=10.0)] if fill is None else fill,
        **kwargs,
    )


def test_disabled_trigger_1_preserves_trigger_2_allocations():
    slots = [_slot(0, price=0.5, buy=0.3), _slot(1, buy=0.2, consumption=1)]
    original = _plan(slots, enabled=True)
    explicit = _plan(slots, enabled=True, trigger_1_enabled=False)
    assert original.allocations == explicit.allocations
    assert explicit.trigger_1_reason == REASON_DISABLED
    assert explicit.refill_price is None
    assert explicit.trigger_1_budget_kwh == 0


def test_no_surplus_when_demand_exhausts_usable_energy():
    slots = [_slot(0, price=0.5), _slot(1, consumption=6)]
    plan = _plan(slots, trigger_1_enabled=True)
    assert plan.status == STATUS_NO_OPPORTUNITY
    assert plan.trigger_1_budget_kwh == 0
    assert plan.trigger_1_reason == REASON_NO_SURPLUS
    assert plan.allocations == ()


@pytest.mark.parametrize("price, accepted", [(0.30, False), (0.3001, True)])
def test_refill_gate_is_strict_and_accounts_for_efficiency_and_cost(price, accepted):
    slots = [_slot(0, price=price), _slot(1)]
    fill = [_slot(2, price=0.10, solar=20)]
    plan = _plan(slots, trigger_1_enabled=True, discharge_efficiency=0.5,
                 additional_cost_per_kwh=0.10, fill=fill)
    assert bool(plan.allocations) is accepted
    assert plan.refill_price == pytest.approx(0.10)
    assert plan.trigger_1_reason == (REASON_PLANNED if accepted else REASON_NO_ELIGIBLE_CANDIDATES)
    if accepted:
        assert plan.allocations[0].surplus_threshold == pytest.approx(0.30)


@pytest.mark.parametrize("fill", [
    [],
    [_slot(3, price=0.1, solar=10)],
    [_slot(2, price=0.1, solar=1), _slot(4, price=0.1, solar=10)],
    [_slot(2, price=0.1, solar=1), _slot(2.5, price=0.1, solar=10)],
    [_slot(2, price=0.1, solar=float("nan"))],
    [_slot(2, price=float("inf"), solar=10)],
    [HorizonSlot(datetime(2026, 6, 1, 2), DAY + timedelta(hours=3), 0.1, None, 0, 10)],
])
def test_bad_refill_window_blocks_only_trigger_1(fill):
    slots = [_slot(0, price=0.5), _slot(1, buy=0.1, consumption=1)]
    plan = _plan(slots, enabled=True, trigger_1_enabled=True, fill=fill)
    baseline = _plan(slots, enabled=True)
    assert plan.refill_price is None
    assert plan.trigger_1_reason == REASON_NO_REFILL_PRICE
    assert plan.allocations == baseline.allocations


def test_refill_uses_highest_positive_slot_price_only_until_full():
    slots = [_slot(0, price=0.6), _slot(1)]
    fill = [_slot(2, price=0.10, solar=3), _slot(3, price=0.20, solar=5),
            _slot(4, price=0.90, solar=5)]
    plan = _plan(slots, trigger_1_enabled=True, fill=fill)
    assert plan.refill_price == pytest.approx(0.20)
    assert plan.status == STATUS_PLANNED


@pytest.mark.parametrize("fill, budget", [
    ([_slot(2, price=0.1, solar=3)], 1.0),
    ([_slot(2, consumption=1), _slot(3, price=0.1, solar=20)], 5.0),
])
def test_insufficient_solar_or_morning_dry_spell_sells_the_refillable_part(fill, budget):
    plan = _plan([_slot(0, price=0.5), _slot(1)], trigger_1_enabled=True, fill=fill)
    assert plan.trigger_1_reason == REASON_PLANNED
    assert plan.refill_price == pytest.approx(0.1)
    assert plan.trigger_1_budget_kwh == pytest.approx(budget)
    assert sum(a.surplus_kwh for a in plan.allocations) == pytest.approx(budget)


@pytest.mark.parametrize("fill", [
    [_slot(2, price=0.1, solar=1.5)],
    [_slot(2, consumption=7), _slot(3, price=0.1, solar=20)],
])
def test_refill_short_even_keeping_the_whole_surplus_blocks_trigger_1(fill):
    plan = _plan([_slot(0, price=0.5), _slot(1)], trigger_1_enabled=True, fill=fill)
    assert plan.refill_price is None
    assert plan.trigger_1_reason == REASON_NO_REFILL_PRICE
    assert plan.trigger_1_budget_kwh == pytest.approx(6)
    assert plan.allocations == ()


def test_943_morning_standby_before_sun_sells_a_partial_budget():
    # Reporter's numbers: usable 7.22, protected 4.19, surplus 3.03, 10.82 kWh sun.
    battery = _battery(soc=12 + 722 / 10.24, capacity=10.24, floor=12.0)
    slots = [_slot(0, price=0.5), _slot(1, buy=0.3, consumption=4.19)]
    fill = [_slot(2, consumption=0.2), _slot(3, consumption=0.2),
            _slot(4, price=0.05, solar=2, consumption=0.3),
            _slot(5, price=0.05, solar=4, consumption=0.4),
            _slot(6, price=0.05, solar=4.82, consumption=0.4)]
    plan = _plan(slots, trigger_1_enabled=True, batteries=[battery], fill=fill)
    assert plan.trigger_1_reason == REASON_PLANNED
    assert 0 < plan.trigger_1_budget_kwh < 3.03
    full = (100 - 12) / 100 * 10.24
    kept = 3.03 - plan.trigger_1_budget_kwh
    assert _solar_refill_price(fill, DAY + timedelta(hours=2), kept, full, 1.0) is not None
    assert _solar_refill_price(fill, DAY + timedelta(hours=2),
                               kept - REFILL_SEARCH_STEP_KWH, full, 1.0) is None


def test_smaller_sale_with_cheaper_refill_beats_no_sale():
    # Selling all 6 kWh fills only in the 0.30 slot (> 0.25 export); keeping 5
    # fills in the 0.10 slot, so 1 kWh sells.
    fill = [_slot(2, price=0.1, solar=3), _slot(3, price=0.30, solar=10)]
    plan = _plan([_slot(0, price=0.25), _slot(1)], trigger_1_enabled=True, fill=fill)
    assert plan.trigger_1_reason == REASON_PLANNED
    assert plan.refill_price == pytest.approx(0.1)
    assert plan.trigger_1_budget_kwh == pytest.approx(1)


@pytest.mark.parametrize("chronological", [True, False])
def test_partial_sale_keeps_trigger_2_off_the_refill_start(chronological):
    # Surplus 5, the sun refills only when 4 are kept: Trigger 1 sells 1 and
    # Trigger 2 must not sell the protected demand on top of it.
    slots = [_slot(0, price=0.5), _slot(1, buy=0.1, consumption=1)]
    plan = _plan(slots, enabled=True, trigger_1_enabled=True,
                 chronological_coverage=chronological,
                 fill=[_slot(2, price=0.1, solar=4)])
    assert plan.trigger_1_budget_kwh == pytest.approx(1)
    assert plan.total_allocated_kwh <= plan.trigger_1_budget_kwh + 1e-6


def test_partial_sale_never_sells_less_than_all_or_nothing():
    # Partial Trigger 1 would sell 1 kWh and keep 2, blocking Trigger 2's
    # 3 kWh demand sale: the all-or-nothing plan sells more, so it wins.
    slots = [_slot(0, price=0.5), _slot(1, buy=0.1, consumption=3)]
    plan = _plan(slots, enabled=True, trigger_1_enabled=True,
                 fill=[_slot(2, price=0.1, solar=6)])
    assert plan.trigger_1_reason == REASON_NO_REFILL_PRICE
    assert plan.total_allocated_kwh == pytest.approx(3)
    assert plan.allocations == _plan(slots, enabled=True).allocations


def test_negative_solar_export_prices_floor_refill_cost_at_zero():
    plan = _plan([_slot(0, price=0.01), _slot(1)], trigger_1_enabled=True,
                 fill=[_slot(2, price=-0.2, solar=10)])
    assert plan.refill_price == 0.0
    assert plan.allocations[0].surplus_kwh > 0


def test_shared_capacity_merges_trigger_1_and_trigger_2_in_one_slot():
    slots = [_slot(0, price=0.5), _slot(1, buy=0.1, consumption=2)]
    plan = _plan(slots, enabled=True, trigger_1_enabled=True,
                 batteries=[_battery(soc=70.0)], max_export_power_w=4000)
    assert len(plan.allocations) == 1
    allocation = plan.allocations[0]
    assert allocation.surplus_kwh == pytest.approx(3)
    assert allocation.energy_kwh == pytest.approx(4)
    assert allocation.power_w == pytest.approx(4000)
    assert sum(link.energy_kwh for link in allocation.demand_links) == pytest.approx(1)
    assert allocation.threshold == pytest.approx(0.1)
    assert allocation.surplus_threshold == pytest.approx(0.1)
    assert plan.allocation_at(slots[0].start) == allocation
    assert plan.remaining_kwh(slots[0].start) == pytest.approx(4)


def test_price_ranking_breaks_ties_by_time_and_output_is_chronological():
    slots = [_slot(0, price=0.3), _slot(1, price=0.5), _slot(2, price=0.5)]
    battery = _battery(soc=40, floor=20, power=1000)
    kwargs = dict(trigger_1_enabled=True, horizon=3, batteries=[battery],
                  fill=[_slot(3, price=0.1, solar=10)])
    plan = _plan(slots, **kwargs)
    assert plan == _plan(slots, **kwargs)
    assert [a.start for a in plan.allocations] == [slots[1].start, slots[2].start]
    assert [a.surplus_kwh for a in plan.allocations] == pytest.approx([1, 1])


def test_ineligible_battery_contributes_nothing_to_budget_or_full():
    batteries = [_battery(soc=50, max_soc=60),
                 _battery(soc=100, capacity=100, eligible=False)]
    plan = _plan([_slot(0, price=0.5), _slot(1)], batteries=batteries,
                 trigger_1_enabled=True, fill=[_slot(2, price=0.1, solar=4)])
    assert plan.usable_energy_kwh == pytest.approx(3)
    assert plan.trigger_1_budget_kwh == pytest.approx(3)
    assert plan.refill_price == pytest.approx(0.1)
    assert sum(a.surplus_kwh for a in plan.allocations) == pytest.approx(3)


def test_below_floor_battery_adds_no_budget_but_its_gap_delays_the_refill():
    # Alone, the first pack refills with 4 kWh of solar. The second pack sits
    # 1 kWh below its floor: it sells nothing, but its 9 kWh of room to
    # max_soc absorb tomorrow's solar too, so 4 kWh no longer proves a fill.
    batteries = [_battery(soc=50, max_soc=60),
                 _battery(soc=10, capacity=10, floor=20, max_soc=100)]
    plan = _plan([_slot(0, price=0.5), _slot(1)], batteries=batteries,
                 trigger_1_enabled=True, fill=[_slot(2, price=0.1, solar=4)])
    assert plan.trigger_1_budget_kwh == pytest.approx(3)
    assert plan.refill_price is None
    assert plan.trigger_1_reason == REASON_NO_REFILL_PRICE
    assert plan.allocations == ()


def test_trigger_1_only_does_not_make_trigger_2_demand_links():
    slots = [_slot(0, price=0.5), _slot(1, buy=0.1, consumption=2)]
    plan = _plan(slots, trigger_1_enabled=True)
    assert plan.status == STATUS_PLANNED
    assert plan.total_allocated_kwh == pytest.approx(4)
    assert all(not a.demand_links and a.energy_kwh == a.surplus_kwh for a in plan.allocations)


def test_fall_back_fill_window_uses_real_instants():
    # The two 02:00 wall-clock hours are distinct real hours; the second
    # starts exactly where the first ends after UTC normalization.
    start = datetime(2026, 10, 25, 0, tzinfo=UTC).astimezone(MADRID)
    middle = datetime(2026, 10, 25, 1, tzinfo=UTC).astimezone(MADRID)
    end = datetime(2026, 10, 25, 2, tzinfo=UTC).astimezone(MADRID)
    assert start.hour == middle.hour == 2
    horizon = start
    slots = [HorizonSlot(horizon - timedelta(hours=1), horizon, 0.5, None, 0, 0)]
    fill = [HorizonSlot(start, middle, 0.1, None, 0, 0.4),
            HorizonSlot(middle, end, 0.2, None, 0, 0.4)]
    plan = plan_high_price_discharge(
        slots, [_battery(soc=21, capacity=10, floor=20, max_soc=28)],
        now=horizon - timedelta(hours=1), horizon_end=horizon,
        trigger_1_enabled=True, fill_slots=fill,
    )
    assert plan.refill_price == pytest.approx(0.2)
    assert plan.status == STATUS_PLANNED
