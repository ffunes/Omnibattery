"""Tests for the guaranteed-minimum-SOC floor in ``_should_activate_grid_charging`` (#417).

A solar-positive day computes zero (negative) deficit, so the predictive
charger would charge nothing overnight and the battery hits the hardware floor
in the morning before solar ramps up. The floor forces a charge sized to reach
the configured SOC regardless of the daily balance.

The method only touches a handful of attributes, so it is exercised unbound on
a stub controller (no Home Assistant runtime needed).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing.engine import PricingManager


class _Coord:
    def __init__(self, soc, capacity_kwh, min_soc=12, max_soc=95):
        self.min_soc = min_soc
        self.max_soc = max_soc
        self.data = {"battery_soc": soc, "battery_total_energy": capacity_kwh}


async def _noop():
    pass


def _consumption(value):
    async def _f():
        return value
    return _f


def _ctrl(coords, floor, *, solar="50.0", consumption=2.0):
    # solar far exceeds consumption → natural deficit is negative (no charge).
    return SimpleNamespace(
        predictive_charging_enabled=True,
        coordinators=list(coords),
        _predictive_safety_margin_kwh=0.0,
        _predictive_grid_charge_margin_pct=0.0,
        _predictive_min_soc_floor=floor,
        _predictive_min_soc_floor_enabled=floor > 0,
        _daily_consumption_history=[],
        solar_forecast_sensor="sensor.solar",
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _e: SimpleNamespace(state=solar))),
        _consumption_tracker=SimpleNamespace(get_dynamic_base_consumption=_consumption(consumption)),
    )


def _run(ctrl):
    return asyncio.run(ChargeDischargeController._should_activate_grid_charging(ctrl))


def test_floor_forces_charge_on_solar_positive_day():
    # 10 kWh battery at 15%, floor 30%, hysteresis 5% → trigger at 25%.
    # 15% < 25% → fires; charges to floor (30%), deficit = (30-15)% * 10 = 1.5 kWh.
    result = _run(_ctrl([_Coord(15.0, 10.0)], floor=30.0))
    assert result["should_charge"] is True
    assert abs(result["energy_deficit_kwh"] - 1.5) < 0.05
    assert "Guaranteed minimum" in result["reason"]


def test_floor_disabled_does_not_charge():
    # Same balanced day, floor off → no charge.
    result = _run(_ctrl([_Coord(15.0, 10.0)], floor=0.0))
    assert result["should_charge"] is False


def test_soc_above_floor_no_effect():
    # SOC already above the floor → floor contributes nothing.
    result = _run(_ctrl([_Coord(40.0, 10.0)], floor=30.0))
    assert result["should_charge"] is False


def test_soc_in_hysteresis_band_no_charge():
    # SOC between (floor - margin) and floor: hysteresis band — should NOT re-trigger.
    # floor=30%, margin=5% → band is [25%, 30%]; SOC=27% is inside, no charge.
    result = _run(_ctrl([_Coord(27.0, 10.0)], floor=30.0))
    assert result["should_charge"] is False


# --- handle_time_slot_predictive_charging: floor re-evaluation trigger ---------
# Regression for the self-disarm bug: once last_evaluation_soc drifts below
# (floor - margin), a 30% drop can never fire again, so the floor must trigger
# its own re-evaluation while SOC is below (floor - margin) and we're not already
# grid charging.


def _make_engine(soc, floor, *, grid_charging_active, last_evaluation_soc):
    calls = {"activate": 0, "handle": 0}

    async def _activate():
        calls["activate"] += 1
        return {"should_charge": True, "energy_deficit_kwh": 1.0}

    async def _handle():
        calls["handle"] += 1

    controller = SimpleNamespace(
        charging_time_slots=["slot"],
        predictive_charging_overridden=False,
        grid_charging_active=grid_charging_active,
        last_evaluation_soc=last_evaluation_soc,
        _predictive_min_soc_floor=floor,
        _predictive_min_soc_floor_enabled=floor > 0,
        coordinators=[_Coord(soc, 10.0)],
        max_contracted_power=5000,
        _grid_charging_initialized=False,
        first_execution=False,
        _slot_entry_time=None,
        _last_decision_data=None,
        _check_time_window=lambda: True,
        _should_activate_grid_charging=_activate,
        _handle_predictive_grid_charging=_handle,
    )
    engine = PricingManager(hass=SimpleNamespace(), controller=controller)
    return engine, controller, calls


def test_floor_below_re_evaluates_when_clamped():
    # last_evaluation_soc clamped just above the hysteresis threshold (the self-disarm
    # scenario): |12 - 13.9| < 30 so the swing threshold can't fire, but SOC is below
    # (floor - margin = 15%) → a re-evaluation must be forced and charging activated.
    engine, ctrl, calls = _make_engine(
        soc=12.0, floor=20.0, grid_charging_active=False, last_evaluation_soc=13.9
    )
    asyncio.run(engine.handle_time_slot_predictive_charging())
    assert calls["activate"] == 1
    assert ctrl.grid_charging_active is True


def test_no_re_evaluation_while_already_charging():
    # Already charging for the floor → the not-grid_charging_active guard stops
    # the floor trigger from re-evaluating every cycle during the charge ramp.
    engine, ctrl, calls = _make_engine(
        soc=12.0, floor=20.0, grid_charging_active=True, last_evaluation_soc=13.9
    )
    asyncio.run(engine.handle_time_slot_predictive_charging())
    assert calls["activate"] == 0
    assert calls["handle"] == 1


def test_above_floor_no_swing_no_re_evaluation():
    # SOC above the floor and no 30% swing → nothing triggers a re-evaluation.
    engine, ctrl, calls = _make_engine(
        soc=49.0, floor=20.0, grid_charging_active=False, last_evaluation_soc=50.0
    )
    asyncio.run(engine.handle_time_slot_predictive_charging())
    assert calls["activate"] == 0
    assert ctrl.grid_charging_active is False


# --- floor_recovered: stop condition when SOC climbs back to the floor -----------
# Without this, floor_crossed starts charging but nothing ever stops it on a
# solar-positive day (no 30% SOC drop triggers a re-eval while SOC is rising).


def _make_engine_recovered(soc, floor, *, last_evaluation_soc):
    """Grid charging IS active (floor_crossed already fired); SOC has climbed back."""
    calls = {"activate": 0}

    async def _activate():
        calls["activate"] += 1
        # Solar-positive day: re-eval at floor finds no deficit → stop charging.
        return {"should_charge": False, "energy_deficit_kwh": 0.0}

    controller = SimpleNamespace(
        charging_time_slots=["slot"],
        predictive_charging_overridden=False,
        grid_charging_active=True,          # already charging
        last_evaluation_soc=last_evaluation_soc,
        _predictive_min_soc_floor=floor,
        _predictive_min_soc_floor_enabled=floor > 0,
        coordinators=[_Coord(soc, 10.0)],
        max_contracted_power=5000,
        _grid_charging_initialized=False,
        first_execution=False,
        _slot_entry_time=None,
        _last_decision_data=None,
        _check_time_window=lambda: True,
        _should_activate_grid_charging=_activate,
        _handle_predictive_grid_charging=_noop,
    )
    engine = PricingManager(hass=SimpleNamespace(), controller=controller)
    return engine, controller, calls


def test_floor_recovered_stops_charging():
    # Battery was at 12% (floor_crossed fired, charging started, last_eval_soc=12%).
    # SOC has now climbed to 20% (the floor). floor_recovered must fire a re-eval
    # which finds no deficit → grid_charging_active becomes False.
    engine, ctrl, calls = _make_engine_recovered(soc=20.0, floor=20.0, last_evaluation_soc=12.0)
    asyncio.run(engine.handle_time_slot_predictive_charging())
    assert calls["activate"] == 1
    assert ctrl.grid_charging_active is False


def test_floor_recovered_does_not_fire_below_floor():
    # SOC at 18% (below floor=20%) while charging → floor_recovered must NOT fire,
    # charging continues.
    engine, ctrl, calls = _make_engine_recovered(soc=18.0, floor=20.0, last_evaluation_soc=12.0)
    asyncio.run(engine.handle_time_slot_predictive_charging())
    assert calls["activate"] == 0
    assert ctrl.grid_charging_active is True


def test_floor_recovered_does_not_fire_twice():
    # After floor_recovered fires and last_eval_soc is updated to the floor (20%),
    # a second cycle at the same SOC must NOT re-evaluate again.
    engine, ctrl, calls = _make_engine_recovered(soc=20.0, floor=20.0, last_evaluation_soc=20.0)
    asyncio.run(engine.handle_time_slot_predictive_charging())
    assert calls["activate"] == 0


# --- slot-exit cleanup resets last_evaluation_soc even when charging never ran ----
# Regression: on a solar-sufficient day the initial eval sets last_evaluation_soc
# but grid_charging_active/_grid_charging_initialized stay False. The exit cleanup
# was gated on those flags, so last_evaluation_soc kept its value and the NEXT
# day's slot was not treated as an initial eval → its notification never fired.


def _make_engine_out_of_window(*, grid_charging_active, last_evaluation_soc):
    dismissed = {"n": 0}

    async def _async_call(domain, service, data):
        if service == "dismiss":
            dismissed["n"] += 1

    controller = SimpleNamespace(
        charging_time_slots=["slot"],
        predictive_charging_overridden=False,
        grid_charging_active=grid_charging_active,
        last_evaluation_soc=last_evaluation_soc,
        _grid_charging_initialized=False,
        error_integral=1.0,
        previous_error=1.0,
        sign_changes=3,
        _slot_entry_time=object(),
        _check_time_window=lambda: False,   # out of window → else branch
    )
    engine = PricingManager(
        hass=SimpleNamespace(services=SimpleNamespace(async_call=_async_call)),
        controller=controller,
    )
    return engine, controller, dismissed


def test_slot_exit_resets_eval_soc_after_no_charge_day():
    # Not-needed day: last_evaluation_soc set by initial eval, charging never ran.
    engine, ctrl, dismissed = _make_engine_out_of_window(
        grid_charging_active=False, last_evaluation_soc=42.0
    )
    asyncio.run(engine.handle_time_slot_predictive_charging())
    assert ctrl.last_evaluation_soc is None   # reset → next day is a fresh initial eval
    assert dismissed["n"] == 1                 # lingering "Not required" notification cleared


def test_slot_exit_noop_when_nothing_to_clean():
    # Fully idle outside a slot (no prior eval): cleanup must not run every cycle.
    engine, ctrl, dismissed = _make_engine_out_of_window(
        grid_charging_active=False, last_evaluation_soc=None
    )
    asyncio.run(engine.handle_time_slot_predictive_charging())
    assert dismissed["n"] == 0


if __name__ == "__main__":
    test_floor_forces_charge_on_solar_positive_day()
    test_floor_disabled_does_not_charge()
    test_soc_above_floor_no_effect()
    test_soc_in_hysteresis_band_no_charge()
    test_floor_below_re_evaluates_when_clamped()
    test_no_re_evaluation_while_already_charging()
    test_above_floor_no_swing_no_re_evaluation()
    test_floor_recovered_stops_charging()
    test_floor_recovered_does_not_fire_below_floor()
    test_floor_recovered_does_not_fire_twice()
    test_slot_exit_resets_eval_soc_after_no_charge_day()
    test_slot_exit_noop_when_nothing_to_clean()
    print("ok")
