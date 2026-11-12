"""Real-Time Price mode: stop paths and the per-cycle "not needed" decision.

While ``grid_charging_active`` is True the control loop leaves the batteries
to this handler alone, so every way out of a charge has to clear that flag:
a handler that just returns leaves them on their last charge command with
nothing regulating the grid import.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from custom_components.omnibattery.pricing.engine import PricingManager

DAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 23, 30)


class _Coord:  # hashable: target maps are keyed by coordinator
    name = "battery"

    def __init__(self):
        self.data = {"battery_soc": 40.0, "battery_total_energy": 10.0}


def _engine(*, charging, should_charge=False, now=NOW):
    coord = _Coord()
    calls = []
    handled = []
    clock = {"now": now}
    price = {"value": 0.10}
    states = {}

    async def _decision():
        calls.append(clock["now"])
        return {"should_charge": should_charge}

    async def _handle():
        handled.append(clock["now"])

    controller = SimpleNamespace(
        price_sensor="sensor.price",
        average_price_sensor=None,
        max_price_threshold=0.20,
        predictive_charging_overridden=False,
        coordinators=[coord],
        _realtime_price_charging=charging,
        _realtime_eval_date=DAY,
        grid_charging_active=charging,
        _grid_charging_initialized=charging,
        # 55 % target on a 10 kWh battery at 40 %: 1.5 kWh still missing.
        _predictive_charge_target_soc={coord: 55.0} if charging else None,
        _last_decision_data=None,
        previous_power=1200,
        previous_error=300,
        charge_delay_enabled=False,
        _is_operation_allowed=lambda is_charging=True: True,
        _handle_predictive_grid_charging=_handle,
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    engine = PricingManager(hass=hass, controller=controller)
    engine._get_current_price = lambda: price["value"]
    engine._current_horizon_grid_charging_decision = _decision
    engine._reset_predictive_demand_runtime = lambda: None
    engine._net_scalar_grid_carried_load = lambda decision_data, now: None
    engine._now = lambda: clock["now"]
    return SimpleNamespace(
        engine=engine, controller=controller, coord=coord, calls=calls,
        handled=handled, clock=clock, price=price, states=states,
    )


def _cycle(env):
    asyncio.run(env.engine.handle_realtime_price_predictive_charging())


def _assert_stopped(env, *, shortfall_recorded=True):
    controller = env.controller
    assert controller._realtime_price_charging is False
    assert controller.grid_charging_active is False
    assert controller._grid_charging_initialized is False
    assert controller.previous_power == 0
    assert controller.previous_error == 0
    assert env.handled == []
    if shortfall_recorded:
        assert controller._last_decision_data["shortfall_mode"] == "realtime_price"
        assert controller._last_decision_data["predictive_shortfall_kwh"] == 1.5


# ---------------------------------------------------------------------------
# The threshold disappears while charging
# ---------------------------------------------------------------------------

def test_charge_stops_when_the_average_sensor_goes_unavailable():
    env = _engine(charging=True)
    env.controller.average_price_sensor = "sensor.average_price"
    env.controller.max_price_threshold = None
    env.states["sensor.average_price"] = SimpleNamespace(state="unavailable")

    _cycle(env)

    _assert_stopped(env)


def test_charge_stops_when_the_manual_threshold_is_cleared():
    env = _engine(charging=True)
    env.controller.max_price_threshold = None

    _cycle(env)

    _assert_stopped(env)


def test_no_threshold_while_idle_changes_nothing():
    env = _engine(charging=False)
    env.controller.max_price_threshold = None

    _cycle(env)

    assert env.controller.previous_power == 1200
    assert env.controller._last_decision_data is None
    assert env.calls == []


def test_unavailable_average_sensor_falls_back_to_the_fixed_threshold():
    env = _engine(charging=True)
    env.controller.average_price_sensor = "sensor.average_price"
    env.states["sensor.average_price"] = SimpleNamespace(state="unavailable")

    _cycle(env)

    assert env.controller.grid_charging_active is True
    assert env.handled == [NOW]


# ---------------------------------------------------------------------------
# The other ways out of a charge
# ---------------------------------------------------------------------------

def test_charge_stops_when_the_price_is_unavailable():
    env = _engine(charging=True)
    env.price["value"] = None

    _cycle(env)

    _assert_stopped(env)


def test_charge_stops_when_the_override_is_active():
    env = _engine(charging=True)
    env.controller.predictive_charging_overridden = True

    _cycle(env)

    _assert_stopped(env)


def test_charge_stops_when_the_price_rises_above_the_threshold():
    env = _engine(charging=True)
    env.price["value"] = 0.25

    _cycle(env)

    _assert_stopped(env)


def test_charge_stops_outside_the_charge_time_slot():
    env = _engine(charging=True)
    env.controller._is_operation_allowed = lambda is_charging=True: False

    _cycle(env)

    _assert_stopped(env)


def test_new_day_stop_records_no_shortfall():
    env = _engine(charging=True, now=datetime(2026, 11, 10, 0, 5))

    _cycle(env)

    _assert_stopped(env, shortfall_recorded=False)
    assert env.controller._last_decision_data == {"should_charge": False}
    assert env.controller._predictive_charge_target_soc is None


# ---------------------------------------------------------------------------
# Cheap price, charge not needed: the decision is not redone every cycle
# ---------------------------------------------------------------------------

def test_not_needed_decision_is_reused_between_cycles():
    env = _engine(charging=False)

    for _ in range(5):
        _cycle(env)
        env.clock["now"] += timedelta(seconds=2.5)

    assert len(env.calls) == 1
    assert env.controller._last_decision_data == {"should_charge": False}
    assert env.controller.grid_charging_active is False


def test_not_needed_decision_is_redone_when_something_moved():
    env = _engine(charging=False)
    _cycle(env)
    _cycle(env)
    assert len(env.calls) == 1

    def _one_more(expected):
        _cycle(env)
        assert len(env.calls) == expected
        _cycle(env)
        assert len(env.calls) == expected

    env.clock["now"] += timedelta(minutes=4, seconds=59)
    _cycle(env)
    assert len(env.calls) == 1
    env.clock["now"] += timedelta(seconds=1)
    _one_more(2)

    env.coord.data["battery_soc"] = 39.5
    _cycle(env)
    assert len(env.calls) == 2
    env.coord.data["battery_soc"] = 39.0
    _one_more(3)

    env.price["value"] = 0.11
    _one_more(4)

    # controller.invalidate_predictive_plan() stamps this.
    env.controller._dp_config_dirty_at = 1234.5
    _one_more(5)


def test_reused_decision_leaves_the_last_decision_data_alone():
    env = _engine(charging=False)
    _cycle(env)
    marker = {"should_charge": False, "marker": True}
    env.controller._last_decision_data = marker
    env.controller._realtime_eval_date = None

    _cycle(env)

    assert env.controller._last_decision_data is marker
    assert env.controller._realtime_eval_date is None


def test_manual_batteries_do_not_count_for_the_soc_trigger():
    env = _engine(charging=False)
    manual = _Coord()
    manual.battery_manual_mode_enabled = True
    env.controller.coordinators.append(manual)
    _cycle(env)

    manual.data["battery_soc"] = 90.0
    _cycle(env)

    assert len(env.calls) == 1


def test_should_charge_decision_is_not_cached():
    env = _engine(charging=False, should_charge=True)
    _cycle(env)
    assert env.controller.grid_charging_active is True
    assert env.engine._rt_not_needed is None

    # Price rises, then turns cheap again: a fresh decision, no stale reuse.
    env.price["value"] = 0.25
    _cycle(env)
    env.price["value"] = 0.10
    _cycle(env)

    assert len(env.calls) == 2


# ---------------------------------------------------------------------------
# A completed charge does not end the cheap period's evaluation
# ---------------------------------------------------------------------------

def _complete_on_next_call(env):
    async def _complete():
        env.handled.append(env.clock["now"])
        env.controller.grid_charging_active = False
        env.controller._grid_charging_initialized = False

    env.controller._handle_predictive_grid_charging = _complete


def test_completed_charge_clears_the_realtime_flag_without_a_shortfall():
    env = _engine(charging=True)
    _complete_on_next_call(env)

    _cycle(env)

    assert env.controller.grid_charging_active is False
    assert env.controller._realtime_price_charging is False
    assert env.controller._predictive_charge_target_soc is None
    assert env.controller._last_decision_data is None


def test_charge_restarts_in_the_same_cheap_period_after_completing():
    env = _engine(charging=True, should_charge=True)
    _complete_on_next_call(env)
    _cycle(env)
    assert env.controller._realtime_price_charging is False

    async def _keep_charging():
        env.handled.append(env.clock["now"])

    env.controller._handle_predictive_grid_charging = _keep_charging
    env.clock["now"] += timedelta(seconds=2.5)
    _cycle(env)

    assert env.controller._realtime_price_charging is True
    assert env.controller.grid_charging_active is True
    assert len(env.calls) == 1


def test_batteries_out_of_comms_do_not_count_as_a_completed_charge():
    # _handle_predictive_grid_charging returns with the flag untouched.
    env = _engine(charging=True)

    _cycle(env)

    assert env.controller.grid_charging_active is True
    assert env.controller._realtime_price_charging is True
    assert env.handled == [NOW]
