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


def test_a_held_slot_blocks_the_discharge_through_the_reserve_blocker():
    """Step 5: the optimizer's hold is executed by the reserve's own blocker,
    for the whole slot; outside it nothing is blocked and the reserve module's
    level is not used."""
    from types import SimpleNamespace

    from tests.conftest import FakeCoordinator

    a, b = _slots(_at(11, 0), [0.15, 0.30])
    battery = FakeCoordinator(name="b1", data={"battery_soc": 60.0, "battery_total_energy": 10.0})
    blocks = {}
    clock_now = {"t": a.start}
    ctrl = SimpleNamespace(
        coordinators=[battery],
        _dp_plan_holds=frozenset({a}),
        _pricing_mgr=SimpleNamespace(_now=lambda: clock_now["t"]),
        _is_battery_manual_owned=lambda c: False,
        _discharge_reserve_mgr=SimpleNamespace(
            feature_enabled=lambda: True,
            reserve_soc_pct=lambda: (_ for _ in ()).throw(AssertionError("reserve module asked")),
        ),
        set_discharge_block=lambda source, reason, details=None, coordinator=None: blocks.__setitem__(
            (source, coordinator.name), details
        ),
        remove_discharge_block=lambda source, coordinator=None: blocks.pop((source, coordinator.name), None),
    )
    ChargeDischargeController._refresh_price_reserve_blocks(ctrl)
    assert blocks[("price_reserve", "b1")]["origin"] == "plan"
    clock_now["t"] = b.start
    ChargeDischargeController._refresh_price_reserve_blocks(ctrl)
    assert blocks == {}


def _hold_controller(holds, now, *, guard=None, discharging=()):
    from types import SimpleNamespace

    from tests.conftest import FakeCoordinator

    battery = FakeCoordinator(name="b1", data={"battery_soc": 60.0, "battery_total_energy": 10.0})
    writes = []
    ctrl = SimpleNamespace(
        coordinators=[battery],
        _dp_plan_holds=frozenset(holds),
        _pricing_mgr=SimpleNamespace(_now=lambda: now),
        _discharge_reserve_mgr=SimpleNamespace(
            feature_enabled=lambda: True, _release_guard=lambda: guard
        ),
        _battery_discharge_blockers={},
        _global_discharge_blockers={},
        _active_charge_batteries=[],
        _active_discharge_batteries=[battery] if discharging else [],
        _setpoint_overrides={},
        _capacity_protection_active=False,
    )

    async def set_power(c, charge, discharge):
        writes.append((c.name, charge, discharge))

    ctrl._set_battery_power = set_power
    ctrl.set_discharge_block = lambda source, reason, details=None, coordinator=None: (
        ctrl._battery_discharge_blockers.setdefault(coordinator, {}).__setitem__(source, details)
    )
    ctrl.remove_discharge_block = lambda source, coordinator=None: (
        ctrl._battery_discharge_blockers.get(coordinator, {}).pop(source, None)
    )
    ctrl.is_charge_blocked = lambda coordinator=None: False
    ctrl.is_discharge_blocked = lambda coordinator=None, **kw: (
        ChargeDischargeController.is_discharge_blocked(ctrl, coordinator, **kw)
    )
    ctrl._capacity_protection_overrides_curtailment = lambda: False
    return ctrl, battery, writes


def test_a_hold_stops_a_running_discharge_before_the_deadband_returns():
    """The cycle stops a battery a per-battery block catches mid-discharge
    (_stop_blocked_active_batteries runs before the deadband and stale-sensor
    early returns): a held slot that starts while discharging goes to 0 W."""
    import asyncio

    a, _b = _slots(_at(11, 0), [0.15, 0.30])
    ctrl, battery, writes = _hold_controller({a}, a.start, discharging=True)
    ChargeDischargeController._refresh_price_reserve_blocks(ctrl)
    assert asyncio.run(ChargeDischargeController._stop_blocked_active_batteries(ctrl))
    assert writes == [("b1", 0, 0)]
    # Peak shaving and the other protections ignore this economic blocker.
    assert not ChargeDischargeController.is_discharge_blocked(ctrl, battery, ignore_economic=True)


def test_the_reserve_guards_release_a_hold():
    a, _b = _slots(_at(11, 0), [0.15, 0.30])
    ctrl, battery, _writes = _hold_controller({a}, a.start, guard="capacity_protection")
    ChargeDischargeController._refresh_price_reserve_blocks(ctrl)
    assert not ChargeDischargeController.is_discharge_blocked(ctrl, battery)


def test_the_dashboard_projection_keeps_what_a_held_slot_holds():
    a, b = _slots(_at(11, 0), [0.15, 0.30])
    ctrl, _battery, _writes = _hold_controller({a}, a.start)
    ctrl._pricing_mgr.weekly_refill_hold_fn = lambda: None
    floor = ChargeDischargeController._daily_operation_reserve_floor(ctrl)
    assert floor(a.start, 3.0, 2.0) == 3.0
    assert floor(b.start, 3.0, 2.0) == 0.0


def test_export_prices_come_from_the_export_sensor_lowest_overlap():
    from types import SimpleNamespace

    from custom_components.omnibattery.pricing import PriceSlot
    from custom_components.omnibattery.pricing.engine import PricingManager

    hour, half = _slots(_at(11, 0), [0.20, 0.20])
    exports = [
        PriceSlot(hour.start, hour.start + timedelta(minutes=10), 0.05),
        PriceSlot(hour.start + timedelta(minutes=10), half.end, 0.08),
    ]
    with_sensor = PricingManager(SimpleNamespace(), SimpleNamespace(export_price_sensor="sensor.export"))
    with_sensor._now = lambda: hour.start
    with_sensor.get_future_export_price_slots = lambda _end: exports
    assert with_sensor._optimizer_export_prices([hour, half], half.end) == {
        hour.start: 0.05,
        half.start: 0.08,
    }
    without = PricingManager(SimpleNamespace(), SimpleNamespace(export_price_sensor=None))
    assert without._optimizer_export_prices([hour, half], half.end) is None


def test_the_control_lock_is_held_while_the_optimizer_runs(monkeypatch):
    """An evaluation from outside the control cycle holds the control lock
    across the executor await, so no cycle runs on the half-replaced plan
    (the decision is published before the plan, the schedule after it)."""
    import asyncio
    import threading

    from custom_components.omnibattery.pricing.engine import PricingManager
    from tests import test_predictive_scenarios as h

    scenario = h.load_scenario(h.SCENARIO_DIR / "834-midnight-reevaluation.json")
    seen = {}
    real_plan = PricingManager.evaluate_dynamic_pricing_plan.__func__

    def in_worker(cls, request):
        seen["locked"] = seen["lock"].locked()
        seen["worker"] = threading.current_thread() is not threading.main_thread()
        return real_plan(cls, request)

    monkeypatch.setattr(PricingManager, "evaluate_dynamic_pricing_plan", classmethod(in_worker))
    real_eval = PricingManager._evaluate_dynamic_pricing

    async def via_now(self, **kwargs):
        if getattr(self, "_via_now", False):
            return await real_eval(self, **kwargs)
        self._via_now = True
        seen["lock"] = self._controller._control_lock
        return await self.evaluate_dynamic_pricing_now(**kwargs)

    monkeypatch.setattr(PricingManager, "_evaluate_dynamic_pricing", via_now)
    real_controller = h._controller

    def build(s):
        controller = real_controller(s)
        controller._control_lock = asyncio.Lock()
        return controller

    monkeypatch.setattr(h, "_controller", build)
    h._run_engine(scenario, monkeypatch, executor=True)
    assert seen["worker"] and seen["locked"]


def test_a_plan_without_charge_keeps_its_projection_for_the_soc_drop_trigger(monkeypatch):
    """Without a charge slot there is no schedule; the SOC-drop re-evaluation
    then fell back to a 30-point drop. The optimizer's trajectory is kept."""
    from tests import test_predictive_scenarios as h

    box = {}
    real_controller = h._controller
    monkeypatch.setattr(h, "_controller", lambda s: box.setdefault("c", real_controller(s)))
    scenario = h.load_scenario(h.SCENARIO_DIR / "858-hourly-prices-falling-night.json")
    outcome = h._run_engine(scenario, monkeypatch)
    assert outcome.planner == "optimizer" and not outcome.booked
    projection = box["c"]._dp_eval_projection
    assert projection and projection[-1][1] < 0  # the night drains the battery


def _export_controller(exports, now, *, guard=None, projected=50.0, soc=50.0):
    from types import SimpleNamespace

    from tests.conftest import FakeCoordinator

    blocks = {}
    clock = {"t": now}
    ctrl = SimpleNamespace(
        coordinators=[FakeCoordinator(name="b1", data={"battery_soc": soc, "battery_total_energy": 10.0})],
        _dp_plan_exports=frozenset(exports),
        _pricing_mgr=SimpleNamespace(_now=lambda: clock["t"], projected_soc_now=lambda: projected),
        _surplus_hold_mgr=SimpleNamespace(
            feature_enabled=lambda: True,
            _release_guard=lambda for_plan=False: guard,
            is_hold_active=lambda: (_ for _ in ()).throw(AssertionError("module asked")),
        ),
        set_charge_block=lambda source, reason, details=None: blocks.__setitem__(source, details),
        remove_charge_block=lambda source: blocks.pop(source, None),
    )
    return ctrl, blocks, clock


def test_an_exported_slot_on_the_plan_s_track_blocks_the_charge():
    a, b = _slots(_at(10, 0), [0.20, 0.20])
    ctrl, blocks, clock = _export_controller({a}, a.start, projected=51.5, soc=50.0)
    ChargeDischargeController._refresh_surplus_price_hold_block(ctrl)
    assert blocks["surplus_price_hold"]["origin"] == "plan"
    # Decided once per slot: a later reading behind the track keeps it.
    ctrl.coordinators[0].data["battery_soc"] = 40.0
    ChargeDischargeController._refresh_surplus_price_hold_block(ctrl)
    assert "surplus_price_hold" in blocks
    clock["t"] = b.start
    ChargeDischargeController._refresh_surplus_price_hold_block(ctrl)
    assert blocks == {}


@pytest.mark.parametrize(
    "kwargs",
    [dict(guard="charge_delay"), dict(projected=53.0, soc=50.0), dict(projected=None)],
    ids=["guard", "behind_the_plan", "no_projection"],
)
def test_an_export_is_released_off_the_plan_s_track(kwargs):
    a, _b = _slots(_at(10, 0), [0.20, 0.20])
    ctrl, blocks, _clock = _export_controller({a}, a.start, **kwargs)
    ChargeDischargeController._refresh_surplus_price_hold_block(ctrl)
    assert blocks == {}
