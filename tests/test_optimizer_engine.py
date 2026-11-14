"""The engine executes the 2.0.0 optimizer's plan as planned (design §2.5, step 3).

* A slot marked "to the end" targets one SOC step above what each battery can
  charge in it, so the whole-percent target is not reached early.
* At the end of an optimizer slot only what the plan expected and the battery
  did not store moves to later slots, within one reading step (the reading
  truncates): the step asked on top of a "to the end" slot is not a shortfall.
* The room left in a receiving slot is sized with the plan's efficiency.
"""
from __future__ import annotations

import math
from datetime import timedelta

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing import SLOT_PURPOSE_DEFICIT
from tests.test_dynamic_pricing_slot_handover import (  # noqa: F401 (fixture)
    _Battery,
    _balance,
    _controller,
    _decision,
    _manager,
    _run,
    _schedule,
    _slots,
    _at,
    clock,
)
from tests.test_slot_target_split import _Coord, _ctrl

EFF = math.sqrt(0.85)
QUARTER_KWH = 2.5 * 0.25 * EFF  # what 2.5 kW stores in 15 min


def _optimizer_schedule(slots, quotas, to_end=()):
    return _schedule(
        {slot: SLOT_PURPOSE_DEFICIT for slot in slots},
        slot_energy_targets_kwh=dict(zip(slots, quotas)),
        chronological_planning_active=True,
        slot_charge_to_end=frozenset(to_end),
        plan_charge_efficiency=EFF,
    )


def test_a_to_end_slot_targets_a_step_above_what_each_battery_charges():
    # Unequal gaps through the time-window split: both batteries must still be
    # charging when the quarter ends.
    small = _Coord("small", 83.0, 10.0, 2500, max_soc=90)
    large = _Coord("large", 20.0, 10.0, 2500, max_soc=90)
    ctrl = _ctrl([small, large], 2 * QUARTER_KWH)
    targets = ChargeDischargeController._compute_deficit_target_soc(
        ctrl, planned_kwh=2 * QUARTER_KWH, log=False, window_hours=0.25, to_slot_end=True
    )
    for c in (small, large):
        asked = (targets[c] - c.data["battery_soc"]) / 100.0 * c.data["battery_total_energy"]
        assert asked > QUARTER_KWH + 0.05, c.name


def test_the_slot_s_target_carries_the_step(clock):
    battery = _Battery(15.0)
    a, b = _slots(_at(11, 0), [-0.05, 0.30])
    ctrl = _controller(battery, _decision(QUARTER_KWH), _optimizer_schedule([a], [QUARTER_KWH], [a]))
    ctrl.max_charge_capacity = 2500
    clock.value = a.start
    _run(_manager(ctrl, _balance(battery, 10.0)))
    # 15 % + 5.76 % stored + one step: unreachable in the quarter.
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(15.0 + QUARTER_KWH * 10 + 1.0)


def _end_slot(clock, battery, schedule, reached_soc, at):
    ctrl = _controller(battery, _decision(1.0), schedule)
    ctrl.max_charge_capacity = 2500
    manager = _manager(ctrl, _balance(battery, 10.0))
    clock.value = at.start
    _run(manager)
    battery.soc = reached_soc
    clock.value = at.end + timedelta(seconds=2)
    _run(manager)
    return schedule


def test_a_to_end_slot_that_charged_its_quarter_carries_nothing(clock):
    battery = _Battery(15.0)
    a, b, c = _slots(_at(11, 0), [-0.05, -0.05, 0.30])
    schedule = _optimizer_schedule([a, b, c], [QUARTER_KWH, QUARTER_KWH, 0.2], [a, b])
    # Charged the whole quarter: 15 + 5.76 -> reads 20.
    _end_slot(clock, battery, schedule, 20.0, a)
    assert schedule.slot_energy_targets_kwh[b] == pytest.approx(QUARTER_KWH)
    assert schedule.slot_energy_targets_kwh[c] == pytest.approx(0.2)
    assert schedule.deadline_shortfall_kwh == 0.0


def test_a_real_shortfall_moves_with_the_plan_s_efficiency(clock):
    battery = _Battery(15.0)
    a, b, c = _slots(_at(11, 0), [0.10, 0.10, 0.30])
    schedule = _optimizer_schedule([a, b, c], [0.5, 0.4, 0.2])
    # Expected 0.5 kWh, reads 17 % (0.2 stored): 0.3 short, less one step.
    _end_slot(clock, battery, schedule, 17.0, a)
    # Slot b has room for 0.176 more at the plan's efficiency (0.131 at 0.85):
    # it takes 0.176 and only the rest reaches the dearer slot c.
    # (b has started 2 s before the loop sees it.)
    assert schedule.slot_energy_targets_kwh[b] == pytest.approx(QUARTER_KWH, abs=0.002)
    assert schedule.slot_energy_targets_kwh[c] == pytest.approx(0.2 + 0.2 - (QUARTER_KWH - 0.4), abs=0.002)


def test_only_the_dynamic_pricing_control_plan_runs_the_optimizer(monkeypatch):
    """The dashboard projection and the diagnostics refresh keep the allocator:
    they run every minute on the event loop (design §6); the dashboard reads
    the stored plan from step 11 on."""
    import asyncio

    from custom_components.omnibattery.pricing import engine as engine_module
    from tests import test_predictive_scenarios as h

    calls = []
    real = engine_module.optimize_charge
    monkeypatch.setattr(engine_module, "optimize_charge", lambda r: calls.append(r) or real(r))
    box = {}
    real_controller = h._controller
    monkeypatch.setattr(h, "_controller", lambda s: box.setdefault("c", real_controller(s)))
    scenario = h.load_scenario(h.SCENARIO_DIR / "834-midnight-reevaluation.json")
    outcome = h._run_engine(scenario, monkeypatch)
    assert calls and outcome.planner == "optimizer"

    calls.clear()
    manager = box["c"]._pricing_mgr
    day_after = scenario.now.replace(hour=12, minute=0) + timedelta(days=1)
    projection = manager.build_extended_chronological_projection(
        now=scenario.now,
        slots=list(scenario.slots),
        base_decision_data=dict(box["c"]._last_decision_data or {}),
        price_ceiling=None,
        horizon_end=day_after,
    )
    asyncio.run(manager.async_refresh_chronological_diagnostics(now=scenario.now))
    assert projection.plan is not None
    assert calls == []


def test_the_optimizer_runs_off_the_event_loop(monkeypatch):
    """With Home Assistant's executor the plan is computed in a worker thread
    while the event loop keeps serving callbacks, and it is the same plan."""
    import asyncio
    import threading

    from custom_components.omnibattery.pricing.engine import PricingManager
    from tests import test_predictive_scenarios as h

    scenario = h.load_scenario(h.SCENARIO_DIR / "834-midnight-reevaluation.json")
    inline = h._run_engine(scenario, monkeypatch)

    seen = {}
    real = PricingManager.evaluate_dynamic_pricing_plan.__func__

    def in_worker(cls, request):
        seen["thread"] = threading.current_thread()
        loop_free = threading.Event()
        seen["loop"].call_soon_threadsafe(loop_free.set)
        seen["loop_served"] = loop_free.wait(timeout=5)
        return real(cls, request)

    monkeypatch.setattr(PricingManager, "evaluate_dynamic_pricing_plan", classmethod(in_worker))
    real_run = asyncio.run

    def run(coro):
        async def main():
            seen["loop"] = asyncio.get_running_loop()
            return await coro
        return real_run(main())

    monkeypatch.setattr(h.asyncio, "run", run)
    threaded = h._run_engine(scenario, monkeypatch, executor=True)
    assert seen["thread"] is not threading.main_thread()
    assert seen["loop_served"]
    assert threaded.booked == inline.booked


def test_an_optimizer_failure_falls_back_to_the_previous_planner(monkeypatch):
    from custom_components.omnibattery.pricing import engine as engine_module
    from custom_components.omnibattery.pricing.engine import PricingManager
    from tests.test_optimizer import _request

    def broken(_request):
        raise ValueError("boom")

    monkeypatch.setattr(engine_module, "optimize_charge", broken)
    import dataclasses

    request = dataclasses.replace(
        _request([0.10, 0.40], house=0.3), grid_carries_load_while_charging=True
    )
    plan = PricingManager.evaluate_dynamic_pricing_plan(request).plan
    assert plan.planner == "allocator: optimizer_error"
