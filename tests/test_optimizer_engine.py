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


def test_a_to_end_slot_that_fell_short_grows_the_next_small_quota(clock):
    # #899: a slot charged to its end (target 15 + 5.76 + 1 step) reads 17 %:
    # 2.76 points short beyond the step and the hidden percent move to b.
    battery = _Battery(15.0)
    a, b, c = _slots(_at(11, 0), [0.10, 0.10, 0.30])
    schedule = _optimizer_schedule([a, b, c], [QUARTER_KWH, 0.2, 0.2], [a])
    _end_slot(clock, battery, schedule, 17.0, a)
    assert schedule.slot_energy_targets_kwh[b] == pytest.approx(0.2 + QUARTER_KWH - 0.3, abs=0.002)
    assert schedule.slot_energy_targets_kwh[c] == pytest.approx(0.2)


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
    """The optimizer's hold is executed by the reserve's own blocker, for the
    whole slot, without the old switch (#888); outside it nothing is blocked
    and the reserve module's level is not used."""
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
            dynamic_pricing_active=lambda: True,
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


def _hold_controller(holds, now, *, guard=None, discharging=(), dynamic_pricing=True):
    from types import SimpleNamespace

    from tests.conftest import FakeCoordinator

    battery = FakeCoordinator(name="b1", data={"battery_soc": 60.0, "battery_total_energy": 10.0})
    writes = []
    ctrl = SimpleNamespace(
        coordinators=[battery],
        _dp_plan_holds=frozenset(holds),
        _pricing_mgr=SimpleNamespace(_now=lambda: now),
        _discharge_reserve_mgr=SimpleNamespace(
            dynamic_pricing_active=lambda: dynamic_pricing, _release_guard=lambda: guard,
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


def test_without_the_plan_s_holds_nothing_is_held():
    """#888 D2: the previous allocator planned (holds None), or Dynamic
    Pricing is not running: no block, the reserve module is not asked."""
    a, _b = _slots(_at(11, 0), [0.15, 0.30])
    for holds, dp in ((None, True), ({a}, False)):
        ctrl, battery, _writes = _hold_controller(holds or (), a.start, dynamic_pricing=dp)
        ctrl._dp_plan_holds = holds if holds is None else frozenset(holds)
        ChargeDischargeController._refresh_price_reserve_blocks(ctrl)
        assert not ChargeDischargeController.is_discharge_blocked(ctrl, battery)
        assert ctrl._plan_hold_status["held_batteries"] == []


def test_a_manually_owned_or_slot_owned_battery_is_not_held():
    """Manual control and a manual slot own the battery: the plan's hold
    leaves it alone."""
    a, _b = _slots(_at(11, 0), [0.15, 0.30])
    ctrl, battery, _writes = _hold_controller({a}, a.start)
    battery.battery_manual_mode_enabled = True
    ChargeDischargeController._refresh_price_reserve_blocks(ctrl)
    assert not ChargeDischargeController.is_discharge_blocked(ctrl, battery)
    battery.battery_manual_mode_enabled = False
    ctrl._is_manual_slot_owned = lambda coordinator: True
    ChargeDischargeController._refresh_price_reserve_blocks(ctrl)
    assert not ChargeDischargeController.is_discharge_blocked(ctrl, battery)
    ctrl._is_manual_slot_owned = lambda coordinator: False
    ChargeDischargeController._refresh_price_reserve_blocks(ctrl)
    assert ChargeDischargeController.is_discharge_blocked(ctrl, battery)


def test_the_status_sensor_is_on_while_the_plan_holds():
    """#888: the status sensor says why the battery does not discharge: on in
    a held slot, with the slot and the end of its run."""
    from custom_components.omnibattery.binary_sensor import DischargeReserveSensor
    from types import SimpleNamespace

    a, b, c = _slots(_at(11, 0), [0.15, 0.16, 0.30])
    ctrl, _battery, _writes = _hold_controller({a, b}, a.start)
    ChargeDischargeController._refresh_price_reserve_blocks(ctrl)
    sensor = DischargeReserveSensor(None, None, ctrl)
    assert sensor.is_on
    attrs = sensor.extra_state_attributes
    assert attrs["held_slot"] == a.start.isoformat()
    assert attrs["held_until"] == b.end.isoformat()
    ctrl._pricing_mgr = SimpleNamespace(_now=lambda: c.start)
    ChargeDischargeController._refresh_price_reserve_blocks(ctrl)
    assert not sensor.is_on
    # A guard releases the hold: off, and it says why.
    ctrl, _battery, _writes = _hold_controller({a}, a.start, guard="manual_control")
    ChargeDischargeController._refresh_price_reserve_blocks(ctrl)
    sensor = DischargeReserveSensor(None, None, ctrl)
    assert not sensor.is_on
    assert sensor.extra_state_attributes["release_reason"] == "manual_control"


def test_a_fallback_plan_publishes_no_holds(monkeypatch):
    """#888 D2: when the previous allocator plans, nothing is held."""
    from custom_components.omnibattery.pricing import engine as pricing_engine
    from tests import test_predictive_scenarios as h

    box = {}
    real_controller = h._controller

    def controller(s):
        box["c"] = real_controller(s)
        return box["c"]

    monkeypatch.setattr(h, "_controller", controller)
    monkeypatch.setattr(pricing_engine, "optimizer_fallback_reason", lambda r: "no_fleet_data")
    scenario = h.load_scenario(h.SCENARIO_DIR / "834-midnight-reevaluation.json")
    outcome = h._run_engine(scenario, monkeypatch)
    assert outcome.planner != "optimizer"
    assert box["c"]._dp_plan_holds is None


def test_the_dashboard_projection_keeps_what_a_held_slot_holds():
    a, b = _slots(_at(11, 0), [0.15, 0.30])
    ctrl, _battery, _writes = _hold_controller({a}, a.start)
    ctrl._pricing_mgr.weekly_refill_hold_fn = lambda: None
    floor = ChargeDischargeController._daily_operation_reserve_floor(ctrl)
    assert floor(a.start, 3.0, 2.0) == 3.0
    assert floor(b.start, 3.0, 2.0) == 0.0
    # The projection's intervals are local aware while price slots are naive:
    # this TypeError emptied the whole forecast half of the chart.
    from datetime import timezone

    assert a.start.tzinfo is None
    assert floor(a.start.replace(tzinfo=timezone.utc), 3.0, 2.0) == 3.0
    assert floor(b.start.replace(tzinfo=timezone.utc), 3.0, 2.0) == 0.0


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
    with_sensor.get_future_export_price_slots = lambda _end, solar_bonus=True: exports
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


@pytest.mark.parametrize(("slot_active", "charging", "expected"), [
    (True, True, True), (True, False, False), (False, False, False)])
def test_a_re_plan_while_a_slot_charges_tells_the_optimizer(monkeypatch, slot_active, charging,
                                                            expected):
    """The start cost must not cut a running charge: going on is no start."""
    from tests import test_predictive_scenarios as h

    real_controller = h._controller

    def controller(s):
        c = real_controller(s)
        c._current_price_slot_active = slot_active
        c.grid_charging_active = charging
        return c

    monkeypatch.setattr(h, "_controller", controller)
    scenario = h.load_scenario(h.SCENARIO_DIR / "834-midnight-reevaluation.json")
    outcome = h._run_engine(scenario, monkeypatch)
    assert outcome.planner == "optimizer"
    assert outcome.request.charging_now is expected


@pytest.mark.parametrize(("status", "expected"), [
    ({"held_batteries": ["b1"]}, True), ({"held_batteries": []}, False), (None, False)])
def test_a_re_plan_while_the_plan_holds_tells_the_optimizer(monkeypatch, status, expected):
    """#888: the hold start cost must not cut a running hold. The evaluation
    drops the plan's holds first, so it reads the last cycle's block."""
    from tests import test_predictive_scenarios as h

    real_controller = h._controller

    def controller(s):
        c = real_controller(s)
        c._plan_hold_status = status
        c._dp_plan_holds = frozenset(s for s in scenario.slots)  # dropped first
        return c

    monkeypatch.setattr(h, "_controller", controller)
    scenario = h.load_scenario(h.SCENARIO_DIR / "834-midnight-reevaluation.json")
    outcome = h._run_engine(scenario, monkeypatch)
    assert outcome.planner == "optimizer"
    assert outcome.request.holding_now is expected


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


def _surplus_controller(monkeypatch, *, sensor):
    from types import SimpleNamespace

    from custom_components.omnibattery.pricing import PriceSlot
    from custom_components.omnibattery.pricing.engine import PricingManager
    from tests import test_predictive_scenarios as h

    async def _noop(*_a, **_k):
        return None

    box = {}
    real_controller = h._controller

    def build(s):
        c = real_controller(s)
        c._surplus_hold_mgr = SimpleNamespace(
            feature_enabled=lambda: True, mark_stale=lambda _r: None, async_rebuild_plan=_noop,
            clear=lambda _r: None,
        )
        c.export_price_sensor = "sensor.export" if sensor else None
        c._dp_plan_holds = frozenset({"stale"})
        c._dp_plan_exports = frozenset({"stale"})
        box["c"] = c
        return c

    monkeypatch.setattr(h, "_controller", build)
    monkeypatch.setattr(
        PricingManager, "get_future_export_price_slots",
        lambda self, horizon_end=None, **_kw: [
            PriceSlot(x.start, x.end, x.price - 0.02) for x in self._raw_price_slots(quiet=True)[0]
        ],
    )
    return box


def test_without_an_export_sensor_the_surplus_hold_module_decides(monkeypatch):
    """No export prices: the plan would value export at 0 and never export, so
    it neither offers the choice nor silences the module (which uses the
    import curve)."""
    from tests import test_predictive_scenarios as h

    box = _surplus_controller(monkeypatch, sensor=False)
    outcome = h._run_engine(h.load_scenario(h.SCENARIO_DIR / "834-midnight-reevaluation.json"), monkeypatch)
    assert not outcome.request.export_choice_allowed
    assert box["c"]._dp_plan_exports is None
    box = _surplus_controller(monkeypatch, sensor=True)
    outcome = h._run_engine(h.load_scenario(h.SCENARIO_DIR / "834-midnight-reevaluation.json"), monkeypatch)
    assert outcome.request.export_choice_allowed
    assert box["c"]._dp_plan_exports is not None


def test_an_evaluation_that_ends_early_drops_the_previous_plan_s_orders():
    """No prices: the evaluation returns before any plan; the previous plan's
    holds and exports must not outlive it (the modules decide)."""
    import asyncio
    from types import SimpleNamespace

    from custom_components.omnibattery.pricing.engine import PricingManager

    ctrl = SimpleNamespace(_dp_plan_holds=frozenset({"x"}), _dp_plan_exports=frozenset({"x"}),
                           _dp_export_decision=("x", True))
    PricingManager._clear_plan_orders(SimpleNamespace(_controller=ctrl))
    assert ctrl._dp_plan_holds is None and ctrl._dp_plan_exports is None
    assert ctrl._dp_export_decision is None


def _sale_manager(sales, now, *, soc=60.0, end_soc=50.0, start_soc=None, guard=None):
    from types import SimpleNamespace

    from custom_components.omnibattery.control.high_price_discharge import HighPriceDischargeManager
    from tests.conftest import FakeCoordinator

    overrides = {}
    ctrl = SimpleNamespace(
        coordinators=[FakeCoordinator(name="b1", data={"battery_soc": soc, "battery_total_energy": 10.0})],
        _dp_plan_sales=sales,
        _pricing_mgr=SimpleNamespace(projected_soc_now=lambda at=None: (
            start_soc if start_soc is not None and at == now else end_soc)),
        set_setpoint_override=lambda src, value, priority=0: overrides.__setitem__(src, value),
        remove_setpoint_override=lambda src: overrides.pop(src, None),
    )
    manager = HighPriceDischargeManager(SimpleNamespace(), ctrl)
    manager._now = lambda: now
    manager._block_guard = lambda: guard
    return manager, ctrl, overrides


def test_a_planned_sale_runs_on_the_plan_s_track_until_the_slot_s_projected_energy():
    a, b = _slots(_at(19, 0), [0.40, 0.40])
    manager, ctrl, overrides = _sale_manager({a: 2.5}, a.start)
    manager._refresh_plan_sale(ctrl._dp_plan_sales)
    assert list(overrides.values()) == [-2500.0]
    ctrl.coordinators[0].data["battery_soc"] = 50.0  # the reading of the projection: still on
    manager._refresh_plan_sale(ctrl._dp_plan_sales)
    assert list(overrides.values()) == [-2500.0]
    ctrl.coordinators[0].data["battery_soc"] = 49.0  # below it
    manager._refresh_plan_sale(ctrl._dp_plan_sales)
    assert overrides == {}


def test_a_planned_sale_stops_under_the_whole_percent_of_the_projection():
    """The reading truncates: stopping as soon as it reached the projection
    (50.4 -> reading 50) left each run of sales about a point short of the plan.
    It sells down to the projection's whole percent: at most one reading step."""
    a, _b = _slots(_at(19, 0), [0.40, 0.40])
    manager, ctrl, overrides = _sale_manager({a: 2.5}, a.start, soc=50.0, end_soc=50.4)
    manager._refresh_plan_sale(ctrl._dp_plan_sales)
    assert list(overrides.values()) == [-2500.0]


@pytest.mark.parametrize("start_soc, end_soc", [(10.0, 10.0), (10.0, 12.0)],
                         ids=["at_the_floor", "solar_beyond_the_sale_charges"])
def test_a_planned_sale_whose_projection_does_not_fall_still_runs(start_soc, end_soc):
    """A sale with the battery at its floor exports the sun; one whose extra sun
    charges the battery projects a rise. Neither has energy to stop at: they
    used to be skipped (9 of 40 planned quarters in one night)."""
    a, _b = _slots(_at(19, 0), [0.40, 0.40])
    manager, ctrl, overrides = _sale_manager({a: 2.5}, a.start, soc=10.0, start_soc=start_soc, end_soc=end_soc)
    manager._refresh_plan_sale(ctrl._dp_plan_sales)
    assert list(overrides.values()) == [-2500.0]


def test_a_planned_sale_behind_the_plan_still_sells_down_to_the_slot_s_projection():
    """No all-or-nothing "on the plan's track" check: a 5 kW quarter is 12 points
    of a 10 kWh fleet, and skipping it on a 2-point margin dropped its whole
    margin. Behind the plan it sells less: it stops at the slot end's projection."""
    a, _b = _slots(_at(19, 0), [0.40, 0.40])
    manager, ctrl, overrides = _sale_manager({a: 2.5}, a.start, soc=55.0)  # behind a plan at 60
    manager._refresh_plan_sale(ctrl._dp_plan_sales)
    assert list(overrides.values()) == [-2500.0]
    ctrl.coordinators[0].data["battery_soc"] = 49.0
    manager._refresh_plan_sale(ctrl._dp_plan_sales)
    assert overrides == {}


@pytest.mark.parametrize("kwargs", [dict(soc=49.0), dict(guard="charge_order"), dict(end_soc=None)],
                         ids=["under_the_slot_end_projection", "guard", "no_projection"])
def test_a_planned_sale_is_released(kwargs):
    a, _b = _slots(_at(19, 0), [0.40, 0.40])
    manager, ctrl, overrides = _sale_manager({a: 2.5}, a.start, **kwargs)
    manager._refresh_plan_sale(ctrl._dp_plan_sales)
    assert overrides == {}


def test_battery_sales_are_priced_without_the_solar_bonus():
    from types import SimpleNamespace

    from custom_components.omnibattery.pricing import PriceSlot
    from custom_components.omnibattery.pricing.engine import PricingManager

    hour, _half = _slots(_at(10, 0), [0.20, 0.20])
    seen = []
    manager = PricingManager(SimpleNamespace(), SimpleNamespace(export_price_sensor="sensor.export"))
    manager._now = lambda: hour.start

    def export_slots(_end, solar_bonus=True):
        seen.append(solar_bonus)
        return [PriceSlot(hour.start, hour.end, 0.30 if solar_bonus else 0.10)]

    manager.get_future_export_price_slots = export_slots
    assert manager._optimizer_export_prices([hour], hour.end, solar_bonus=False) == {hour.start: 0.10}
    assert seen == [False]


@pytest.mark.parametrize(
    ("case", "soc"),
    [
        # Nothing to charge (should_charge false): the calendar is built with
        # no plan, and storing that wiped the projection the plan had stored.
        (("1x7k-800", 60, "flat0.4", "night-cheap", False, "sunny", "07:00"), 17),
        (("1x5k-2500", 60, "flat0.4", "night-cheap", False, "sunny", "00:05"), None),
        # An extended plan trimmed to today's slots (energy deferred to tomorrow).
        (("1x10k-800", 15, "flat1.2", "night-cheap", True, "dull", "16:00"), None),
    ],
    ids=["no-charge", "charge", "trimmed"],
)
def test_the_soc_drop_projection_is_the_optimizer_trajectory(monkeypatch, case, soc):
    """The SOC-drop trigger compares the live SOC with the published plan's
    trajectory, whatever the plan books (a plan with sales or holds and no
    charge fell back to the old drop rule)."""
    from tests import test_predictive_scenarios as h
    from tests import test_predictive_sweep as sw

    box = {}
    real_controller = h._controller
    monkeypatch.setattr(h, "_controller", lambda s: box.setdefault("c", real_controller(s)))
    outcome = h._run_engine(sw.scenario(case, soc=soc), monkeypatch)
    assert outcome.planner == "optimizer"
    usable = outcome.plan.projection_inputs[0]
    expected = tuple((end, energy - usable) for end, energy in outcome.plan.trajectory)
    assert expected and box["c"]._dp_eval_projection == expected
    # The panel draws the same rows from the same anchor (a trimmed plan is a copy).
    assert len(box["c"]._dp_eval_rows) == len(expected) and box["c"]._dp_eval_anchor is not None


def _delay_engine(monkeypatch, *, enabled=True, setpoint=None):
    from tests import test_predictive_scenarios as h
    from tests import test_predictive_sweep as sw

    box = {}
    real_controller = h._controller

    def build(s):
        c = real_controller(s)
        c.charge_delay_enabled = enabled
        c._delay_safety_margin_h = 1.0
        c._delay_soc_setpoint_enabled = setpoint is not None
        c._delay_soc_setpoint = setpoint or 50
        c._balance_monitor_overrides_delay = lambda: False
        box["c"] = c
        return c

    monkeypatch.setattr(h, "_controller", build)
    case = ("1x7k-800", 60, "flat0.4", "night-cheap", False, "sunny", "07:00")
    return h._run_engine(sw.scenario(case, soc=17), monkeypatch), box["c"]


def test_the_charge_delay_is_planned_by_the_optimizer(monkeypatch):
    """Step 8 (D9): the delay's margin and setpoint reach the optimizer, which
    exports the morning sun it delays (no export sensor needed) and owns the
    delay while its plan stands."""
    outcome, c = _delay_engine(monkeypatch, setpoint=40)
    assert outcome.request.solar_delay_margin_h == 1.0
    assert outcome.request.solar_delay_setpoint_pct == 40
    assert c._dp_plan_solar_delay is True and c._dp_plan_exports
    outcome, c = _delay_engine(monkeypatch, enabled=False)
    assert outcome.request.solar_delay_margin_h is None
    assert not getattr(c, "_dp_plan_solar_delay", False) and c._dp_plan_exports is None


def test_a_delay_export_runs_without_the_surplus_hold():
    a, _b = _slots(_at(10, 0), [0.20, 0.20])
    ctrl, blocks, _clock = _export_controller({a}, a.start, projected=50.0, soc=50.0)
    ctrl._surplus_hold_mgr.feature_enabled = lambda: False
    ctrl._dp_plan_solar_delay = True
    ctrl.predictive_charging_enabled = True
    ctrl.predictive_charging_mode = "dynamic_pricing"
    ChargeDischargeController._refresh_surplus_price_hold_block(ctrl)
    assert blocks["surplus_price_hold"]["origin"] == "plan"


def test_an_evaluation_drops_the_delay_ownership_with_the_plan_orders():
    from types import SimpleNamespace

    from custom_components.omnibattery.pricing.engine import PricingManager

    ctrl = SimpleNamespace(_dp_plan_solar_delay=True)
    PricingManager._clear_plan_orders(SimpleNamespace(_controller=ctrl))
    assert ctrl._dp_plan_solar_delay is False


def test_a_closed_delay_day_is_kept_until_midnight(monkeypatch):
    """Like the module, a day whose balance needs the grid when it is planned
    stays without delay until midnight, across re-plans."""
    from tests import test_predictive_scenarios as h
    from tests import test_predictive_sweep as sw

    case = ("1x5k-2500", 15, "evening", "night-cheap", False, "sunny", "00:05")
    box = {}
    real_controller = h._controller

    def build(s, closed=None):
        c = real_controller(s)
        c.charge_delay_enabled = True
        c._delay_safety_margin_h = 1.0
        c._delay_soc_setpoint_enabled = False
        c._delay_soc_setpoint = 50
        c._balance_monitor_overrides_delay = lambda: False
        if closed is not None:
            c._dp_solar_delay_closed_date = closed
        box["c"] = c
        return c

    monkeypatch.setattr(h, "_controller", build)
    h._run_engine(sw.scenario(case), monkeypatch)
    assert box["c"]._dp_solar_delay_closed_date == sw.DAY.date()
    assert box["c"]._dp_plan_solar_delay is True  # the module still stands aside
    monkeypatch.setattr(h, "_controller", lambda s: build(s, closed=sw.DAY.date()))
    outcome = h._run_engine(sw.scenario(case, now="10:00"), monkeypatch)
    assert outcome.request.solar_delay_closed_until.date() == (sw.DAY + timedelta(days=1)).date()
    assert outcome.request.solar_delay_closed_until.hour == 0


def test_a_delay_export_of_a_stale_plan_does_not_run_in_another_mode():
    a, _b = _slots(_at(10, 0), [0.20, 0.20])
    ctrl, blocks, _clock = _export_controller({a}, a.start, projected=50.0, soc=50.0)
    ctrl._surplus_hold_mgr.feature_enabled = lambda: False
    ctrl._surplus_hold_mgr.is_hold_active = lambda: False
    ctrl._dp_plan_solar_delay = True
    ctrl.predictive_charging_enabled = True
    ctrl.predictive_charging_mode = "time_slot"
    ChargeDischargeController._refresh_surplus_price_hold_block(ctrl)
    assert blocks == {}


def test_an_aborted_evaluation_drops_the_rows_the_panel_draws(monkeypatch):
    """The panel draws the stored plan rows; an evaluation that stops after
    clearing the projection must not leave the previous plan's rows drawn."""
    import asyncio

    from custom_components.omnibattery.pricing.engine import DynamicPricingEvaluationHorizon
    from tests import test_predictive_scenarios as h
    from tests import test_predictive_sweep as sw

    box = {}
    real_controller = h._controller
    monkeypatch.setattr(h, "_controller", lambda s: box.setdefault("c", real_controller(s)))
    case = ("1x5k-2500", 60, "flat0.4", "night-cheap", False, "sunny", "00:05")
    assert h._run_engine(sw.scenario(case), monkeypatch).planner == "optimizer"
    c = box["c"]
    assert c._dp_eval_rows and len(c._dp_eval_rows) == len(c._dp_eval_projection)

    async def boom(**_kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(c._pricing_mgr, "_maybe_refresh_service_prices", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(c._pricing_mgr._evaluate_dynamic_pricing_unlocked(
            horizon=DynamicPricingEvaluationHorizon.REMAINING))
    assert c._dp_eval_projection is None and c._dp_eval_rows is None


@pytest.mark.parametrize("manual", [False, True], ids=["mixed", "one-manual"])
def test_the_panel_anchor_is_the_fleet_energy_over_every_capacity(monkeypatch, manual):
    """The trigger's reference is a plain SOC mean over the planned batteries;
    the panel draws every battery's energy over every capacity. On 5.12 kWh at
    80 % + 2.5 kWh at 20 % the mean is 10 points off (21 with one in manual)."""
    import dataclasses

    from tests import test_predictive_scenarios as h
    from tests import test_predictive_sweep as sw

    box = {}
    real_controller = h._controller

    def build(s):
        c = real_controller(s)
        if manual:
            c.coordinators[1].battery_manual_mode_enabled = True
        return box.setdefault("c", c)

    monkeypatch.setattr(h, "_controller", build)
    sc = sw.scenario(("2x5k-2500", 60, "evening", "night-cheap", False, "sunny", "16:00"))
    sc = dataclasses.replace(sc, fleet=(dict(sc.fleet[0], soc=80.0), dict(sc.fleet[1], capacity_kwh=2.5, soc=20.0)))
    assert h._run_engine(sc, monkeypatch).planner == "optimizer"
    start, capacity = box["c"]._dp_eval_anchor
    assert start == pytest.approx(5.12 * 0.8 + 2.5 * 0.2) and capacity == pytest.approx(7.62)


# --- Smart Pre-Discharge decided by the optimizer (stage 3) ----------------

def _neg_noon(hr):
    return 0.15 if hr < 7 else (-0.05 if 11 <= hr < 15 else (0.30 if 18 <= hr < 22 else 0.20))


def _room_engine(monkeypatch, *, enabled=True, sensor=True, negative=False, weekly=False, calls=None,
                 case=("1x10k-800", 15, "flat0.4", "neg-noon", False, "sunny", "00:05")):
    """A neg-noon night through the engine with Smart Pre-Discharge on: the
    module's plan stubbed (risk = today's negative slots, per-slot reserve),
    the export curve = import - 0.02."""
    from custom_components.omnibattery.pricing import PriceSlot
    from custom_components.omnibattery.pricing import engine as engine_module
    from custom_components.omnibattery.pricing.curtailment import CurtailmentPlan
    from custom_components.omnibattery.pricing.engine import PricingManager
    from tests import test_predictive_scenarios as h
    from tests import test_predictive_sweep as sw

    monkeypatch.setitem(sw.PRICES, "neg-noon", _neg_noon)
    box = {}
    real_controller = h._controller

    def build(s):
        c = real_controller(s)
        c.smart_predischarge_enabled = enabled
        c.negative_injection_threshold = 0.0
        c.predischarge_export_mode = "automatic"  # the only mode the options offer
        c.predischarge_reserve_soc = 20.0
        c.negative_price_charging_enabled = negative
        c.export_price_sensor = "sensor.export" if sensor else None
        c._dp_plan_room = "stale"
        if weekly:
            c._weekly_full_charge_pending = lambda: True
        box["c"] = c
        return c

    def fake_build(self, slots, reserved=None, *, now=None):
        risk = [s for s in slots if s.price < 0 and s.start.date() == sw.DAY.date()]
        plan = CurtailmentPlan(status="planned", reason="headroom_required", risk_slots=risk,
                               solar_reserve_by_slot={s: 0.2 for s in risk}, opportunistic_space_kwh=3.0)
        self._controller._curtailment_plan = plan
        return plan

    monkeypatch.setattr(h, "_controller", build)
    monkeypatch.setattr(PricingManager, "_build_curtailment_plan", fake_build)
    monkeypatch.setattr(PricingManager, "clear_curtailment_runtime", lambda self, *a, **k: None)
    monkeypatch.setattr(
        PricingManager, "get_future_export_price_slots",
        lambda self, horizon_end=None, **_kw: [
            PriceSlot(x.start, x.end, x.price - 0.02) for x in self._raw_price_slots(quiet=True)[0]
        ],
    )
    if calls is not None:
        from custom_components.omnibattery.pricing.optimizer import optimize_charge as real

        monkeypatch.setattr(engine_module, "optimize_charge", lambda r: calls.append(r) or real(r))
    outcome = h._run_engine(sw.scenario(case), monkeypatch)
    return outcome, box["c"]


def test_the_request_carries_the_room_fields_only_with_pre_discharge_on(monkeypatch):
    outcome, c = _room_engine(monkeypatch)
    req = outcome.request
    assert req.room_threshold == 0.0
    assert req.room_power_kw == pytest.approx(0.8)  # automatic: the fleet's discharge power
    assert req.room_floor_kwh == pytest.approx(0.10 * 10.0)  # reserve 20 % over the 10 % floor
    assert req.room_export_prices and all(
        p == pytest.approx(req.export_prices[t]) for t, p in req.room_export_prices.items()
    )
    assert c._dp_plan_room is not None and c._dp_plan_room.risk
    outcome, c = _room_engine(monkeypatch, enabled=False)
    assert outcome.request.room_threshold is None and not outcome.request.room_export_prices
    assert c._dp_plan_room is None
    # No export sensor (design §2.6 A): the module keeps deciding.
    outcome, c = _room_engine(monkeypatch, sensor=False)
    assert outcome.request.room_threshold is None and c._dp_plan_room is None


def test_with_the_plan_deciding_the_optimizer_runs_once(monkeypatch):
    calls = []
    outcome, c = _room_engine(monkeypatch, calls=calls)
    assert outcome.planner == "optimizer" and len(calls) == 1
    calls.clear()
    # The module decides: its risk window is taken out and the plan rebuilt.
    _room_engine(monkeypatch, enabled=False, calls=calls)
    assert len(calls) == 2


def test_the_negative_price_calendar_still_fills_inside_the_risk(monkeypatch):
    """Decision 3 of the stage-0 consultation: only the second optimizer call
    is skipped; the risk's negative quarters stay booked as today (the
    module's live opportunistic space limits them)."""
    from custom_components.omnibattery.pricing import SLOT_PURPOSE_NEGATIVE_PRICE

    _outcome, c = _room_engine(monkeypatch, negative=True)
    purposes = c._dynamic_pricing_schedule.slot_purposes
    negative = sorted(s.start.strftime("%H:%M") for s, p in purposes.items() if p == SLOT_PURPOSE_NEGATIVE_PRICE)
    assert negative[0] == "11:00" and negative[-1] == "14:45" and len(negative) == 16


def test_a_weekly_full_charge_day_is_the_optimizer_s(monkeypatch):
    """Step 4: a pending weekly day is planned by the optimizer, Smart
    Pre-Discharge included (no room made while the 100 % is pending). With
    no manager (the force button outside the target day) the deadline is
    tomorrow's sunrise."""
    from tests import test_predictive_sweep as sw

    outcome, c = _room_engine(monkeypatch, weekly=True)
    assert outcome.planner == "optimizer"
    req = outcome.request
    assert req.full_by == sw.DAY + timedelta(days=1, hours=sw.SUNRISE)
    # 10 kWh at 15 %: the last 5 % (0.5 kWh) at half of 2.5 kW.
    ((below, kw),) = req.charge_curve
    assert below == pytest.approx(0.5) and kw == pytest.approx(1.25)
    assert req.full_by_power_kw == pytest.approx(2.5)
    assert c._dp_plan_weekly_holds is not None
    assert c._dp_plan_room is not None
    assert not [s for s in c._dp_plan_room.orders if s.start < req.full_by]


def test_without_a_weekly_charge_the_request_carries_none(monkeypatch):
    outcome, c = _room_engine(monkeypatch)
    assert outcome.request.full_by is None and outcome.request.charge_curve == ()
    assert c._dp_plan_weekly_holds is None


def test_a_plan_that_delays_the_sun_unlocks_the_weekly_ceiling(monkeypatch):
    """With "skip the delay" off, the plan owns the delay and the module that
    unlocks 100 % stands aside: the plan unlocks it (latched), or the
    ceiling stayed at max_soc and the run never completed."""
    from tests import test_predictive_scenarios as h
    from tests import test_predictive_sweep as sw

    box = {}
    real_controller = h._controller

    def build(s):
        c = real_controller(s)
        c.charge_delay_enabled = True
        c._charge_delay_unlocked = False
        c._weekly_delay_released = False
        c._weekly_full_charge_pending = lambda: True
        box["c"] = c
        return c

    monkeypatch.setattr(h, "_controller", build)
    outcome = h._run_engine(
        sw.scenario(("1x5k-2500", 60, "flat0.4", "night-cheap", False, "sunny", "00:05")), monkeypatch
    )
    c = box["c"]
    assert outcome.planner == "optimizer" and c._dp_plan_solar_delay
    assert c._weekly_delay_released is True
    # Neither the next evaluation's clear nor the delay module's midnight reset
    # (inside the run's night after) undoes it: with SOC above max_soc the
    # ceiling stays at 100 %, no max_soc block, no hysteresis latch.
    c._pricing_mgr._clear_plan_orders()
    c._charge_delay_unlocked = False
    c._weekly_charge_mgr = type("M", (), {"is_active": lambda self: True})()
    c._balance_monitor_overrides_delay = lambda: False
    assert ChargeDischargeController._weekly_full_charge_unlocked(c) is True
    # Once the run is over it is cleared.
    c._weekly_charge_mgr = type("M", (), {"is_active": lambda self: False})()
    assert ChargeDischargeController._weekly_full_charge_unlocked(c) is False
    assert c._weekly_delay_released is False


def test_room_prices_leave_out_slots_where_the_battery_may_not_discharge(monkeypatch):
    # D4 (stage-0 decision 7): a slot at or below the discharge price
    # threshold, with price discharge control on, is no room slot.
    from custom_components.omnibattery.pricing.engine import PricingManager

    monkeypatch.setattr(PricingManager, "_dp_price_discharge_threshold", lambda self: 0.15)
    outcome, _c = _room_engine(monkeypatch)
    req = outcome.request
    prices = {s.start: s.price for s in req.price_slots}
    assert req.room_export_prices
    assert all(prices[t] > 0.15 for t in req.room_export_prices)


def test_room_prices_leave_out_a_no_discharge_window(monkeypatch):
    from tests import test_predictive_scenarios as h

    real_controller = h._controller

    def build(s):
        c = real_controller(s)
        c.config_entry.data["no_discharge_time_slots"] = [
            {"start_time": "07:00:00", "end_time": "09:00:00", "days": [], "enabled": True,
             "allow_discharge": False}
        ]
        return c

    monkeypatch.setattr(h, "_controller", build)
    outcome, _c = _room_engine(monkeypatch)
    req = outcome.request
    assert req.room_export_prices
    assert not [t for t in req.room_export_prices if 7 <= t.hour < 9]
    assert [t for t in req.export_prices if 7 <= t.hour < 9]


def test_an_evaluation_drops_the_room_orders_with_the_plan_orders():
    from types import SimpleNamespace

    from custom_components.omnibattery.pricing.engine import PricingManager

    ctrl = SimpleNamespace(_dp_plan_room="stale")
    PricingManager._clear_plan_orders(SimpleNamespace(_controller=ctrl))
    assert ctrl._dp_plan_room is None


def test_a_real_room_order_runs_stops_at_its_level_and_does_not_re_plan(monkeypatch):
    """The whole chain with an order the plan made: the module exports at the
    plan's power on the plan's line, stops at the whole percent of the plan's
    level (in the request's own SOC terms), and the SOC-drop trigger sees the
    planned discharge as on the line."""
    from custom_components.omnibattery.pricing.curtailment import BatterySnapshot

    outcome, c = _room_engine(monkeypatch, case=("1x5k-2500", 100, "flat0.4", "neg-noon", False, "sunny", "00:05"))
    room = c._dp_plan_room
    assert room.orders
    manager = c._pricing_mgr
    # The order whose projection falls the most: a real discharge.
    first = max(room.orders, key=lambda s: manager.projected_soc_now(s.start) - manager.projected_soc_now(s.end))
    assert manager.projected_soc_now(first.start) - manager.projected_soc_now(first.end) > 1.0
    kw, level = room.orders[first]
    req = outcome.request
    level_soc = req.fleet_soc_pct + level / req.fleet_capacity_kwh * 100.0
    assert level_soc == pytest.approx(round(level_soc), abs=1e-6)  # on the whole percent

    overrides = {}
    c._setpoint_overrides = overrides
    c.set_setpoint_override = lambda source, value, priority=0: overrides.__setitem__(source, (priority, value))
    c.remove_setpoint_override = lambda source: overrides.pop(source, None)
    c.set_discharge_block = lambda *a, **k: None
    c.remove_discharge_block = lambda *a, **k: None
    battery = c.coordinators[0]
    manager._curtailment_battery_snapshots = lambda: [
        BatterySnapshot("b", battery.data["battery_soc"], 5.12, 100.0, 20.0, 2500.0)
    ]

    def at(moment, soc):
        manager._now = lambda: moment
        battery.data["battery_soc"] = soc
        manager._refresh_plan_room(room, None)
        return c._curtailment_runtime_status

    start = first.start + timedelta(minutes=1)
    assert at(start, manager.projected_soc_now(start)) == "predischarging"
    assert overrides["curtailment_predischarge"] == (5, -kw * 1000.0)
    assert at(start, round(level_soc)) == "predischarging"  # the reading at the level still runs
    assert at(start, round(level_soc) - 1) == "predischarging"  # held: no export, no charge
    assert "curtailment_predischarge" not in overrides and manager._room_hold
    assert at(start, round(level_soc) - 2) == "target_reached"
    # Executed as planned to the slot's end: on the line, no re-plan.
    manager._now = lambda: first.end
    battery.data["battery_soc"] = manager.projected_soc_now(first.end)
    assert manager._is_dp_soc_drop_reeval() is False
    battery.data["battery_soc"] -= 3.0
    assert manager._is_dp_soc_drop_reeval() is True


def test_a_weekly_charge_behind_its_plan_re_plans_once():
    from datetime import datetime
    from types import SimpleNamespace

    from custom_components.omnibattery.pricing.engine import PricingManager

    at = datetime(2026, 11, 11, 6, 45)
    ctrl = SimpleNamespace(_dp_weekly_full_at=at, _weekly_full_charge_pending=lambda: True)
    manager = PricingManager(SimpleNamespace(), ctrl)
    assert not manager._is_weekly_overdue(at + timedelta(minutes=1))
    assert manager._is_weekly_overdue(at + timedelta(minutes=2))
    assert not manager._is_weekly_overdue(at + timedelta(minutes=20))  # once per plan
    ctrl._dp_weekly_full_at = at
    ctrl._weekly_full_charge_pending = lambda: False  # completed meanwhile
    assert not manager._is_weekly_overdue(at + timedelta(minutes=30))


def test_a_weekly_charge_pending_at_a_full_reading_books_a_slot_to_the_top(monkeypatch):
    """A Venus A/D in its taper reads 100 % and is not full until its BMS
    cuts a commanded charge: the plan still books one, to the top, instead
    of leaving the battery to cover the house."""
    import dataclasses

    from tests import test_predictive_scenarios as h
    from tests import test_predictive_sweep as sw

    box = {}
    real_controller = h._controller

    def build(s):
        c = real_controller(s)
        c._weekly_full_charge_pending = lambda: True
        box["c"] = c
        return c

    monkeypatch.setattr(h, "_controller", build)
    sc = sw.scenario(("1x5k-2500", 60, "flat0.4", "night-cheap", True, "none", "00:05"))
    sc = dataclasses.replace(sc, fleet=(dict(sc.fleet[0], soc=100.0, max_soc=90),))
    outcome = h._run_engine(sc, monkeypatch)
    assert outcome.planner == "optimizer" and outcome.booked
    assert outcome.to_end & {slot for slot, _kwh in outcome.booked}



def test_global_manual_mode_releases_the_plan_s_hold():
    """#888: the manual-mode cycle returns before the blocker refresh; the
    plan's hold must not stay latched (block, status sensor, holding_now)."""
    import asyncio
    from types import SimpleNamespace

    from custom_components.omnibattery.const import PREDICTIVE_MODE_DYNAMIC_PRICING
    from custom_components.omnibattery.control.discharge_reserve import DischargeReserveManager
    from custom_components.omnibattery.infra.clock import local_now
    from custom_components.omnibattery.pricing import PriceSlot
    from tests.test_grid_import_cap_policy import _cycle, _noop

    start = local_now().replace(tzinfo=None, second=0, microsecond=0) - timedelta(minutes=5)
    held = PriceSlot(start, start + timedelta(minutes=30), 0.2)
    controller = _cycle(
        manual_mode_enabled=True,
        _pricing_mgr=SimpleNamespace(
            maybe_check_price_data_health=lambda: None,
            clear_curtailment_runtime=lambda _reason: None,
        ),
        _surplus_hold_mgr=None,
        _discharge_reserve_mgr=None,
        _apply_software_manual_setpoints=_noop,
        _dp_plan_holds=frozenset({held}),
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
    )
    controller._discharge_reserve_mgr = DischargeReserveManager(None, controller)
    blocks = {(c.name, "price_reserve"): {"origin": "plan"} for c in controller.coordinators}
    controller.set_discharge_block = lambda source, reason, details=None, coordinator=None: (
        blocks.__setitem__((coordinator.name, source), details))
    controller.remove_discharge_block = lambda source, coordinator=None: (
        blocks.pop((coordinator.name, source), None))
    asyncio.run(controller._run_control_cycle())
    assert blocks == {}
    assert controller._plan_hold_status["held_batteries"] == []
    assert controller._plan_hold_status["release_reason"] == "manual_control"


def test_the_plan_s_holds_are_published_without_any_switch(monkeypatch):
    """#888: Dynamic Pricing publishes the optimizer's holds for everyone;
    here the battery is kept from 05:00 (0.366) to 08:00 for 0.55 and up."""
    from datetime import datetime

    from tests import test_predictive_scenarios as h

    box = {}
    real_controller = h._controller

    def controller(s):
        box["c"] = real_controller(s)
        return box["c"]

    monkeypatch.setattr(h, "_controller", controller)
    scenario = h.load_scenario(h.SCENARIO_DIR / "834-midnight-reevaluation.json")
    outcome = h._run_engine(scenario, monkeypatch)
    assert outcome.planner == "optimizer"
    holds = box["c"]._dp_plan_holds
    assert holds is not None
    # Hourly slots in this file.
    assert {s.start for s in holds} == {datetime(2026, 11, 11, hour) for hour in (5, 6, 7)}
