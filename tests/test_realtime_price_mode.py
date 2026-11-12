"""Real-Time Price mode: stop paths and the per-cycle "not needed" decision.

While ``grid_charging_active`` is True the control loop leaves the batteries
to this handler alone, so every way out of a charge has to clear that flag:
a handler that just returns leaves them on their last charge command with
nothing regulating the grid import.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
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
