"""A running Dynamic Pricing charge is handed over at every slot boundary.

Two defects let one charge fill the battery toward max_soc for a deficit of
well under 1 kWh:

* Adjacent selected slots keep ``in_slot`` true across the boundary, so the
  control loop never noticed that the slot changed: the first slot's identity,
  target and balance stayed in force for the whole run.
* Without per-slot quotas the deficit target is the *live* SOC plus the
  decision's ``planned_grid_charge_kwh``, which was sized from the SOC at
  evaluation time. The pre-slot re-evaluation that refreshes it cannot run for
  a slot that follows another closely, so every slot entered after a charge
  bought the same energy again on top of the higher SOC.

The controller's target lifecycle is mirrored by ``_handle_predictive`` below:
the target is (re)computed whenever the grid handler is uninitialised or the
target was cleared, and a battery counts as done on its least full pack,
exactly as ``_handle_predictive_grid_charging`` and ``_get_available_batteries``
do.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import custom_components.omnibattery as omnibattery_module
from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import PREDICTIVE_MODE_DYNAMIC_PRICING
from custom_components.omnibattery.control.pack_soc import soc_vs_ceiling
from custom_components.omnibattery.pricing import (
    DynamicPricingSchedule,
    PriceSlot,
    SLOT_PURPOSE_COMBINED,
    SLOT_PURPOSE_DEFICIT,
    SLOT_PURPOSE_NEGATIVE_PRICE,
)
from custom_components.omnibattery.pricing import engine as engine_module
from custom_components.omnibattery.pricing.engine import PricingManager

_DAY = datetime(2026, 9, 26)


def _at(hour: int, minute: int) -> datetime:
    return _DAY.replace(hour=hour, minute=minute)


@pytest.fixture
def clock(monkeypatch):
    class _Clock(datetime):
        value = _at(9, 30)

        @classmethod
        def now(cls, tz=None):
            return cls.value

    monkeypatch.setattr(engine_module, "datetime", _Clock)
    monkeypatch.setattr(omnibattery_module, "datetime", _Clock)
    return _Clock


class _Battery:
    def __init__(self, soc: float, capacity_kwh: float = 10.0, max_soc: float = 95.0):
        self.name = "b1"
        self.max_soc = max_soc
        self.is_available = True
        self.data = {"battery_soc": soc, "battery_total_energy": capacity_kwh}

    @property
    def soc(self) -> float:
        return self.data["battery_soc"]

    @soc.setter
    def soc(self, value: float) -> None:
        self.data["battery_soc"] = value


def _decision(planned_kwh: float, stored_kwh: float | None = None) -> dict:
    return {
        "should_charge": planned_kwh > 0.0,
        "energy_deficit_kwh": planned_kwh,
        "planned_grid_charge_kwh": planned_kwh,
        "stored_energy_kwh": stored_kwh,
    }


def _balance(battery: _Battery, required_stored_kwh: float):
    """Remaining-horizon balance: what the battery still lacks right now."""

    def evaluate() -> dict:
        stored = battery.soc / 100.0 * battery.data["battery_total_energy"]
        return _decision(max(0.0, required_stored_kwh - stored), stored)

    return evaluate


def _slots(start: datetime, prices: list[float]) -> list[PriceSlot]:
    return [
        PriceSlot(
            start + timedelta(minutes=15 * index),
            start + timedelta(minutes=15 * (index + 1)),
            price,
        )
        for index, price in enumerate(prices)
    ]


def _schedule(purposes: dict[PriceSlot, str], **kwargs) -> DynamicPricingSchedule:
    slots = sorted(purposes, key=lambda slot: slot.start)
    return DynamicPricingSchedule(
        hours_needed=len(slots) * 0.25,
        selected_slots=slots,
        average_price=sum(slot.price for slot in slots) / len(slots),
        estimated_cost=0.0,
        total_available_slots=len(slots),
        evaluation_time=_at(0, 5),
        energy_deficit_kwh=1.0,
        charging_needed=True,
        slot_purposes=dict(purposes),
        deficit_charging_needed=any(
            purpose != SLOT_PURPOSE_NEGATIVE_PRICE for purpose in purposes.values()
        ),
        negative_price_charging_needed=any(
            purpose != SLOT_PURPOSE_DEFICIT for purpose in purposes.values()
        ),
        **kwargs,
    )


def _controller(battery: _Battery, decision: dict | None, schedule) -> SimpleNamespace:
    ctrl = SimpleNamespace(
        coordinators=[battery],
        predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        predictive_charging_overridden=False,
        negative_price_charging_enabled=True,
        max_contracted_power=10000,
        max_charge_capacity=10000,
        _last_decision_data=decision,
        _dynamic_pricing_schedule=schedule,
        _dynamic_pricing_evaluated_date=_DAY.date(),
        _dp_evening_reevaluated_date=_DAY.date(),
        _dp_eval_retry_count=0,
        _dp_pre_evaluated_slots={},
        _dp_pre_evaluated_purposes={},
        _dp_completed_slots=set(),
        _current_price_slot_active=False,
        _active_dynamic_slot_purpose=None,
        _active_dynamic_price_slot=None,
        _predictive_charge_target_soc=None,
        _predictive_deficit_target_soc=None,
        grid_charging_active=False,
        _grid_charging_initialized=False,
        previous_power=0,
        previous_error=0,
        first_execution=True,
        is_charge_blocked=lambda: False,
        idle_writes=[],
    )

    async def set_power(coordinator, charge, discharge):
        ctrl.idle_writes.append((coordinator.name, charge, discharge))

    async def handle_predictive():
        if not ctrl._grid_charging_initialized or ctrl._predictive_charge_target_soc is None:
            ctrl._predictive_charge_target_soc = ctrl._compute_predictive_target_soc()
        target = (ctrl._predictive_charge_target_soc or {}).get(battery, battery.max_soc)
        if soc_vs_ceiling(battery, battery.soc) >= target:
            ctrl.grid_charging_active = False
            ctrl._grid_charging_initialized = False
            ctrl.first_execution = True
            return
        if not ctrl._grid_charging_initialized:
            ctrl.previous_power = -2500.0
            ctrl.first_execution = False
            ctrl._grid_charging_initialized = True

    ctrl._set_battery_power = set_power
    ctrl._handle_predictive_grid_charging = handle_predictive
    ctrl._compute_predictive_target_soc = (
        lambda **kwargs: ChargeDischargeController._compute_predictive_target_soc(
            ctrl, **kwargs
        )
    )
    return ctrl


async def _noop(*_args, **_kwargs):
    return None


def _manager(ctrl, balance=None) -> PricingManager:
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager.balance_calls = []

    async def remaining(*, now=None, horizon_end=None):
        manager.balance_calls.append(now)
        return balance()

    manager._evaluate_remaining_grid_charging = remaining
    manager.notifications = []

    async def notify(slot):
        manager.notifications.append(slot.start)

    manager._maybe_refresh_service_prices = _noop
    manager._send_dynamic_pricing_slot_start_notification = notify
    manager._send_dp_pre_slot_reevaluation_notification = _noop
    manager._is_evening_reevaluation_time = lambda: False
    manager._is_dp_soc_drop_reeval = lambda: False
    manager._is_excluded_demand_reeval = lambda _now: False
    manager._is_solar_forecast_reeval = lambda _now: False
    manager._is_price_publication_reeval = lambda _now: False
    manager._refresh_excluded_demand_reference = lambda: None
    manager._refresh_solar_forecast_reference = lambda _now: None
    schedule = ctrl._dynamic_pricing_schedule
    manager._get_current_price = lambda: next(
        (
            slot.price
            for slot in schedule.selected_slots
            if slot.start <= engine_module.datetime.now() < slot.end
        ),
        0.30,
    )
    return manager


def _run(manager) -> None:
    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())


def test_adjacent_deficit_slot_takes_over_and_targets_the_remaining_requirement(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [0.30, 0.29])
    # The entry decision was sized before the day's balance moved; the live
    # balance at the boundary needs 1 kWh more (the battery must hold 5 kWh).
    ctrl = _controller(
        battery,
        _decision(8.0),
        _schedule({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT}),
    )
    manager = _manager(ctrl, _balance(battery, 5.0))

    _run(manager)
    assert ctrl._current_price_slot_active is True
    assert ctrl._active_dynamic_price_slot == a
    assert ctrl._predictive_charge_target_soc == {battery: 95.0}

    battery.soc = 40.0
    clock.value = b.start
    _run(manager)

    assert ctrl._current_price_slot_active is True
    assert ctrl._active_dynamic_price_slot == b
    assert ctrl._active_dynamic_slot_purpose == SLOT_PURPOSE_DEFICIT
    assert ctrl._last_decision_data["planned_grid_charge_kwh"] == pytest.approx(1.0)
    # 40 % of 10 kWh plus the 1 kWh still missing, not slot A's 95 %.
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(50.0)
    assert manager.balance_calls == [b.start]


def test_adjacent_slot_boundary_keeps_the_charge_running(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [0.30, 0.29])
    ctrl = _controller(
        battery,
        _decision(8.0),
        _schedule({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT}),
    )
    manager = _manager(ctrl, _balance(battery, 5.0))
    _run(manager)
    ctrl.previous_power = -1800.0

    battery.soc = 40.0
    clock.value = b.start
    _run(manager)

    # A logical hand-over: no idle command, no PD or ownership reset.
    assert ctrl.idle_writes == []
    assert ctrl.grid_charging_active is True
    assert ctrl._grid_charging_initialized is True
    assert ctrl.first_execution is False
    assert ctrl.previous_power == -1800.0
    assert b.start not in ctrl._dp_completed_slots


def test_adjacent_slot_that_is_no_longer_needed_stops_without_idle_write(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [0.30, 0.29])
    ctrl = _controller(
        battery,
        _decision(8.0),
        _schedule({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT}),
    )
    manager = _manager(ctrl, _balance(battery, 3.0))
    _run(manager)

    battery.soc = 40.0
    clock.value = b.start
    _run(manager)

    # The same end as a selected run that ends: normal control takes over.
    assert ctrl._current_price_slot_active is False
    assert ctrl.grid_charging_active is False
    assert ctrl._predictive_charge_target_soc is None
    assert ctrl._dp_pre_evaluated_purposes[b.start] is None
    assert ctrl.idle_writes == []
    assert manager.balance_calls == [b.start]


def test_slot_entered_after_the_target_was_reached_does_not_rebuy_the_energy(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [0.30, 0.29])
    # Planned 2 kWh at 20 %: the battery must hold 4.5 kWh by the horizon.
    ctrl = _controller(
        battery,
        _decision(2.0),
        _schedule({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT}),
    )
    manager = _manager(ctrl, _balance(battery, 4.5))
    _run(manager)
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(40.0)

    battery.soc = 40.0
    clock.value = _at(9, 40)
    _run(manager)
    assert ctrl._current_price_slot_active is False

    clock.value = b.start
    _run(manager)

    # Slot B is entered on the live balance (0.5 kWh), not on the spent
    # 2 kWh added to the new SOC again (which would have targeted 60 %).
    assert ctrl._active_dynamic_price_slot == b
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(45.0)
    assert manager.balance_calls == [b.start]


def test_slot_entered_without_an_earlier_charge_keeps_its_decision(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [0.30, 0.29])
    ctrl = _controller(
        battery,
        _decision(2.0),
        _schedule({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT}),
    )
    manager = _manager(ctrl, _balance(battery, 4.5))

    _run(manager)

    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(40.0)
    assert manager.balance_calls == []


def test_combined_to_deficit_boundary_drops_the_opportunistic_target(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [-0.05, 0.20])
    ctrl = _controller(
        battery,
        _decision(3.0),
        _schedule({a: SLOT_PURPOSE_COMBINED, b: SLOT_PURPOSE_DEFICIT}),
    )
    manager = _manager(ctrl, _balance(battery, 5.5))
    _run(manager)
    assert ctrl._active_dynamic_slot_purpose == SLOT_PURPOSE_COMBINED
    assert ctrl._predictive_charge_target_soc == {battery: 95.0}
    assert ctrl._predictive_deficit_target_soc == {battery: 50.0}

    battery.soc = 45.0
    clock.value = b.start
    _run(manager)

    assert ctrl._active_dynamic_price_slot == b
    assert ctrl._active_dynamic_slot_purpose == SLOT_PURPOSE_DEFICIT
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(55.0)
    assert ctrl._predictive_deficit_target_soc[battery] == pytest.approx(55.0)
    assert getattr(ctrl, "_curtailment_opportunistic_target_soc", None) is None


def test_negative_price_to_deficit_boundary_uses_the_live_balance(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [-0.05, 0.20])
    ctrl = _controller(
        battery,
        _decision(3.0),
        _schedule({a: SLOT_PURPOSE_NEGATIVE_PRICE, b: SLOT_PURPOSE_DEFICIT}),
    )
    manager = _manager(ctrl, _balance(battery, 6.5))
    _run(manager)
    assert ctrl._predictive_charge_target_soc == {battery: 95.0}

    battery.soc = 60.0
    clock.value = b.start
    _run(manager)

    assert ctrl._active_dynamic_slot_purpose == SLOT_PURPOSE_DEFICIT
    # 60 % plus the 0.5 kWh still missing; neither max_soc from slot A nor
    # the 3 kWh morning figure rebased on 60 %.
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(65.0)


def test_deficit_to_combined_boundary_refreshes_both_targets(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [0.20, -0.05])
    ctrl = _controller(
        battery,
        _decision(3.0),
        _schedule({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_COMBINED}),
    )
    manager = _manager(ctrl, _balance(battery, 5.5))
    _run(manager)
    assert ctrl._predictive_charge_target_soc == {battery: 50.0}

    battery.soc = 45.0
    clock.value = b.start
    _run(manager)

    assert ctrl._active_dynamic_slot_purpose == SLOT_PURPOSE_COMBINED
    assert ctrl._predictive_deficit_target_soc[battery] == pytest.approx(55.0)
    assert ctrl._predictive_charge_target_soc == {battery: 95.0}


def test_adjacent_negative_price_slots_keep_the_opportunistic_target(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [-0.05, -0.04])
    ctrl = _controller(
        battery,
        None,
        _schedule({a: SLOT_PURPOSE_NEGATIVE_PRICE, b: SLOT_PURPOSE_NEGATIVE_PRICE}),
    )
    manager = _manager(ctrl, _balance(battery, 0.0))
    _run(manager)

    battery.soc = 60.0
    clock.value = b.start
    _run(manager)

    assert ctrl._active_dynamic_price_slot == b
    assert ctrl._active_dynamic_slot_purpose == SLOT_PURPOSE_NEGATIVE_PRICE
    assert ctrl._predictive_charge_target_soc == {battery: 95.0}
    assert manager.balance_calls == []


def test_adjacent_chronological_slot_charges_its_own_quota(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [0.30, 0.29])
    schedule = _schedule(
        {a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT},
        slot_energy_targets_kwh={a: 1.0, b: 1.5},
        chronological_planning_active=True,
    )
    ctrl = _controller(battery, _decision(2.5), schedule)
    manager = _manager(ctrl, _balance(battery, 4.5))
    _run(manager)
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(30.0)

    # Slot A falls 0.2 kWh short; the boundary moves it into slot B, which
    # has already started by the time the control loop sees it.
    battery.soc = 28.0
    clock.value = b.start + timedelta(seconds=2)
    _run(manager)

    assert ctrl._active_dynamic_price_slot == b
    assert schedule.slot_energy_targets_kwh[b] == pytest.approx(1.7)
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(45.0)
    assert schedule.deadline_shortfall_kwh == 0.0
    # Per-slot quotas are authoritative; no balance re-evaluation is needed.
    assert manager.balance_calls == []


def test_contiguous_run_stops_near_the_remaining_requirement(clock):
    """Replay of a 7.5 h run of 15-minute deficit slots.

    10.24 kWh battery, max SOC 95 %. The pre-slot re-evaluation at 08:30 sees
    32 % and a 0.72 kWh deficit (the battery must hold 4.0 kWh), the house
    then drains it to 28 % by 09:30, and a charge adds 0.4 %/min. Before the
    fix every slot entered after the target was reached added the same
    0.72 kWh to the new SOC, and the run charged to 95 %.
    """
    battery = _Battery(32.0, capacity_kwh=10.24)
    slots = _slots(_at(9, 30), [0.30] * 30)
    ctrl = _controller(
        battery,
        _decision(6.9),
        _schedule({slot: SLOT_PURPOSE_DEFICIT for slot in slots}),
    )
    manager = _manager(ctrl, _balance(battery, 4.0))

    clock.value = _at(8, 30)
    _run(manager)
    assert ctrl._last_decision_data["planned_grid_charge_kwh"] == pytest.approx(0.7232)

    battery.soc = 28.0
    clock.value = _at(9, 30)
    while clock.value < slots[-1].end:
        _run(manager)
        if ctrl._current_price_slot_active and ctrl.grid_charging_active:
            battery.soc = min(95.0, battery.soc + 0.4)
        clock.value += timedelta(minutes=1)

    assert battery.soc < 4.0 / 10.24 * 100.0 + 0.5
    # One charge, stopped once when the requirement was met; the remaining
    # slots are skipped without taking the battery or notifying again.
    assert manager.notifications == [slots[0].start]
    assert ctrl.idle_writes == [("b1", 0, 0)]
    assert len(manager.balance_calls) <= 3


def test_slot_with_nothing_left_to_buy_is_not_taken(clock):
    # A 0 kWh balance used to enter the slot, stop it on the first cycle and
    # write 0 W in between, once per remaining slot of a long run.
    battery = _Battery(40.0)
    a, b = _slots(_at(9, 30), [0.30, 0.29])
    ctrl = _controller(
        battery,
        _decision(0.0),
        _schedule({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT}),
    )
    manager = _manager(ctrl, _balance(battery, 4.0))

    _run(manager)
    clock.value = b.start
    _run(manager)

    assert ctrl._current_price_slot_active is False
    assert ctrl._dp_completed_slots == {a.start, b.start}
    assert ctrl._predictive_charge_target_soc is None
    assert ctrl.idle_writes == []
    assert manager.notifications == []


def test_evening_plan_made_in_the_boundary_cycle_is_not_replaced(clock):
    # The evening fallback runs at 16:00, a quarter-hour boundary. Its plan
    # already sees the energy slot A delivered and must size slot B as is.
    battery = _Battery(20.0)
    a, b = _slots(_at(15, 45), [0.30, 0.29])
    clock.value = a.start
    ctrl = _controller(
        battery,
        _decision(2.0),
        _schedule({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT}),
    )
    manager = _manager(ctrl, _balance(battery, 4.5))
    _run(manager)

    async def evening_recharge():
        ctrl._last_decision_data = _decision(3.0)

    battery.soc = 35.0
    clock.value = b.start
    manager._is_evening_reevaluation_time = lambda: True
    manager._evaluate_evening_recharge = evening_recharge
    _run(manager)

    assert ctrl._active_dynamic_price_slot == b
    assert ctrl._last_decision_data["planned_grid_charge_kwh"] == 3.0
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(65.0)
    assert manager.balance_calls == []


def test_chronological_slot_without_a_quota_is_not_rebased(clock):
    # Evening top-up and weekly reservation slots join a chronological
    # calendar without a quota; they are sized from the decision like any
    # slot of a plain calendar.
    battery = _Battery(20.0)
    a, gap, b = _slots(_at(22, 0), [0.30, 0.40, 0.29])
    clock.value = a.start
    schedule = _schedule(
        {a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT},
        chronological_planning_active=True,
    )
    ctrl = _controller(battery, _decision(2.0), schedule)
    manager = _manager(ctrl, _balance(battery, 4.5))
    _run(manager)
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(40.0)

    battery.soc = 40.0
    clock.value = gap.start
    _run(manager)
    clock.value = b.start
    _run(manager)

    assert ctrl._active_dynamic_price_slot == b
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(45.0)
    assert manager.balance_calls == [b.start]


def test_boundary_keeps_a_demand_suspension_in_place(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [0.30, 0.29])
    ctrl = _controller(
        battery,
        _decision(8.0),
        _schedule({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT}),
    )
    manager = _manager(ctrl, _balance(battery, 5.0))
    _run(manager)

    # Peak shaving (or a charge blocker) has the charge suspended when the
    # boundary passes; that state belongs to the controller, not the slot.
    ctrl._predictive_charge_suspended_for_demand = True
    battery.soc = 40.0
    clock.value = b.start
    _run(manager)

    assert ctrl._active_dynamic_price_slot == b
    assert ctrl._predictive_charge_suspended_for_demand is True
    assert ctrl.idle_writes == []


def test_a_repriced_running_slot_is_not_handed_over(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [0.30, 0.29])
    schedule = _schedule({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT})
    ctrl = _controller(battery, _decision(3.0), schedule)
    manager = _manager(ctrl, _balance(battery, 5.0))
    _run(manager)

    repriced = a._replace(price=0.31)
    schedule.selected_slots = [repriced, b]
    schedule.slot_purposes = {repriced: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT}
    battery.soc = 30.0
    clock.value = _at(9, 35)
    _run(manager)

    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(50.0)
    assert manager.balance_calls == []


def test_combined_slot_in_a_full_solar_reserve_still_refreshes_its_deficit(clock):
    # With no solar headroom the pre-slot gate leaves a combined slot's
    # opportunity to the live check, but its deficit part is still sized from
    # the balance and must not be rebased on the spent one.
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [0.30, -0.05])
    ctrl = _controller(
        battery,
        _decision(2.0),
        _schedule({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_COMBINED}),
    )
    ctrl._curtailment_plan = SimpleNamespace(solar_reserve_by_slot={b: 1.0})
    manager = _manager(ctrl, _balance(battery, 4.5))
    manager._slot_overlaps_curtailment_risk = lambda _slot: True
    manager._curtailment_opportunistic_space = lambda _plan: 0.0
    _run(manager)

    battery.soc = 40.0
    clock.value = _at(9, 40)
    _run(manager)
    for seconds in (0, 3, 6):
        clock.value = b.start + timedelta(seconds=seconds)
        _run(manager)

    assert ctrl._active_dynamic_price_slot == b
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(45.0)
    assert manager.balance_calls == [b.start]


def test_spent_decision_on_an_unarmed_calendar_is_not_re_checked_every_cycle(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [0.30, 0.29])
    schedule = _schedule({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT})
    ctrl = _controller(battery, _decision(2.0), schedule)
    manager = _manager(ctrl, _balance(battery, 4.5))
    _run(manager)
    battery.soc = 40.0
    clock.value = _at(9, 40)
    _run(manager)

    # A later rebuild found no deficit: the calendar is informational only.
    schedule.deficit_charging_needed = False
    gate_runs = []

    async def gate(slot, now, **_kwargs):
        gate_runs.append(now)

    manager._reevaluate_slot_purpose = gate
    for seconds in (0, 3, 6):
        clock.value = b.start + timedelta(seconds=seconds)
        _run(manager)

    assert gate_runs == []
    assert ctrl._current_price_slot_active is False


def test_pre_slot_gate_in_a_full_solar_reserve_evaluates_the_balance_once(clock):
    # The gate returns early for a combined slot whose solar headroom is
    # already reserved. It must still record its deficit verdict, or it runs
    # a full remaining-horizon evaluation on every cycle of its window.
    battery = _Battery(20.0)
    (slot,) = _slots(_at(10, 30), [-0.05])
    ctrl = _controller(battery, _decision(2.0), _schedule({slot: SLOT_PURPOSE_COMBINED}))
    ctrl._curtailment_plan = SimpleNamespace(solar_reserve_by_slot={slot: 1.0})
    manager = _manager(ctrl, _balance(battery, 4.5))
    manager._slot_overlaps_curtailment_risk = lambda _slot: True
    manager._curtailment_opportunistic_space = lambda _plan: 0.0

    clock.value = _at(9, 25)
    while clock.value <= _at(9, 35):
        _run(manager)
        clock.value += timedelta(seconds=2.5)

    assert len(manager.balance_calls) == 1
    assert ctrl._dp_pre_evaluated_slots == {slot.start: True}
    # The opportunity stays with the live gate.
    assert slot.start not in ctrl._dp_pre_evaluated_purposes


def test_slots_are_skipped_while_the_battery_sits_at_its_ceiling(clock):
    # Solar filled the pack before a midday cheap run: there is no gap left
    # to size a deficit target from, which must not read as "charge".
    battery = _Battery(95.0)
    slots = _slots(_at(12, 0), [0.20] * 8)
    ctrl = _controller(
        battery,
        _decision(1.5),
        _schedule({slot: SLOT_PURPOSE_DEFICIT for slot in slots}),
    )
    manager = _manager(ctrl, _balance(battery, 12.0))

    clock.value = slots[0].start
    while clock.value < slots[-1].end:
        _run(manager)
        clock.value += timedelta(minutes=1)

    assert manager.notifications == []
    assert ctrl.idle_writes == []
    assert ctrl._dp_completed_slots == {slot.start for slot in slots}


def test_a_coupled_pack_battery_is_judged_on_its_least_full_pack(clock):
    # #350: the aggregate SOC can reach the target while a pack is still
    # below it, and the handler keeps charging on that pack.
    battery = _Battery(40.0)
    battery.data["battery_soc_pack_1"] = 44.0
    battery.data["battery_soc_pack_2"] = 36.0
    (slot,) = _slots(_at(9, 30), [0.30])
    ctrl = _controller(battery, _decision(0.0), _schedule({slot: SLOT_PURPOSE_DEFICIT}))
    ctrl._predictive_min_soc_floor_enabled = True
    ctrl._predictive_min_soc_floor = 40.0
    ctrl._last_decision_data["floor_active"] = True
    manager = _manager(ctrl, _balance(battery, 4.0))

    _run(manager)

    assert ctrl._current_price_slot_active is True
    assert ctrl._predictive_charge_target_soc == {battery: 40.0}


def test_guaranteed_minimum_floor_slot_is_taken_without_a_deficit(clock):
    battery = _Battery(25.0)
    (slot,) = _slots(_at(9, 30), [0.30])
    ctrl = _controller(battery, _decision(0.0), _schedule({slot: SLOT_PURPOSE_DEFICIT}))
    ctrl._predictive_min_soc_floor_enabled = True
    ctrl._predictive_min_soc_floor = 30.0
    ctrl._last_decision_data["floor_active"] = True
    manager = _manager(ctrl, _balance(battery, 2.0))

    _run(manager)

    assert ctrl._current_price_slot_active is True
    assert ctrl._predictive_charge_target_soc == {battery: 30.0}
    assert manager.notifications == [slot.start]


def test_weekly_full_charge_slot_is_taken_toward_one_hundred_percent(clock):
    battery = _Battery(60.0)
    (slot,) = _slots(_at(21, 0), [0.25])
    clock.value = slot.start
    ctrl = _controller(battery, _decision(4.0), _schedule({slot: SLOT_PURPOSE_DEFICIT}))
    ctrl._weekly_charge_mgr = SimpleNamespace(is_active=lambda: True)
    manager = _manager(ctrl, _balance(battery, 10.0))

    _run(manager)

    assert ctrl._current_price_slot_active is True
    assert ctrl._predictive_charge_target_soc == {battery: 100.0}


def test_charge_delay_holding_a_slot_after_a_charge_evaluates_once(clock):
    battery = _Battery(20.0)
    a, b = _slots(_at(9, 30), [0.30, 0.29])
    ctrl = _controller(
        battery,
        _decision(2.0),
        _schedule({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT}),
    )
    manager = _manager(ctrl, _balance(battery, 4.5))
    _run(manager)
    battery.soc = 40.0
    clock.value = _at(9, 40)
    _run(manager)

    blocked = {"on": True}
    ctrl.is_charge_blocked = lambda: blocked["on"]
    for seconds in (0, 30, 60):
        clock.value = b.start + timedelta(seconds=seconds)
        _run(manager)
    assert ctrl._current_price_slot_active is False

    blocked["on"] = False
    clock.value = b.start + timedelta(minutes=2)
    _run(manager)

    assert ctrl._active_dynamic_price_slot == b
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(45.0)
    assert manager.balance_calls == [b.start]


def test_full_battery_with_a_one_hundred_percent_ceiling_is_skipped(clock):
    # Only a Venus A/D keeps charging past a reported 100% (to its BMS
    # cutoff); any other battery at 100% of a 100% ceiling has nothing to take.
    battery = _Battery(100.0, max_soc=100.0)
    slots = _slots(_at(12, 0), [0.20] * 4)
    ctrl = _controller(
        battery,
        _decision(0.0),
        _schedule({slot: SLOT_PURPOSE_DEFICIT for slot in slots}),
    )
    manager = _manager(ctrl, _balance(battery, 12.0))

    clock.value = slots[0].start
    while clock.value < slots[-1].end:
        _run(manager)
        clock.value += timedelta(minutes=1)

    assert manager.notifications == []
    assert ctrl.idle_writes == []
    assert manager.balance_calls == []


def test_venus_a_d_at_one_hundred_percent_is_left_to_the_handler(clock):
    battery = _Battery(100.0, max_soc=100.0)
    battery.battery_version = "vD"
    (slot,) = _slots(_at(12, 0), [0.20])
    ctrl = _controller(battery, _decision(0.0), _schedule({slot: SLOT_PURPOSE_DEFICIT}))
    manager = _manager(ctrl, _balance(battery, 12.0))
    ctrl._handle_predictive_grid_charging = _noop

    clock.value = slot.start
    _run(manager)

    assert ctrl._current_price_slot_active is True
    assert manager.notifications == [slot.start]
