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
from datetime import datetime, timedelta
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
        predictive_charging_overridden=False,
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


def test_legacy_predictive_override_skips_solar_forecast_lookup():
    """A legacy paused entry must not evaluate its unset forecast sensor (#217)."""
    ctrl = SimpleNamespace(
        predictive_charging_enabled=True,
        predictive_charging_overridden=True,
        solar_forecast_sensor=None,
    )

    result = _run(ctrl)

    assert result["should_charge"] is False
    assert result["reason"] == "Predictive charging disabled"


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
    calls = {"activate": 0, "handle": 0, "notify": 0}

    async def _activate():
        calls["activate"] += 1
        return {"should_charge": True, "energy_deficit_kwh": 1.0}

    async def _handle():
        calls["handle"] += 1

    async def _notify(**_kwargs):
        calls["notify"] += 1

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
    engine._send_predictive_charging_notification = _notify
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
    assert calls["notify"] == 1


def test_no_re_evaluation_while_already_charging():
    # Already charging for the floor → the not-grid_charging_active guard stops
    # the floor trigger from re-evaluating every cycle during the charge ramp.
    engine, ctrl, calls = _make_engine(
        soc=12.0, floor=20.0, grid_charging_active=True, last_evaluation_soc=13.9
    )
    asyncio.run(engine.handle_time_slot_predictive_charging())
    assert calls["activate"] == 0
    assert calls["handle"] == 1


def _make_initial_slot_engine(*, forecast_configured, forecast_state, should_charge=True):
    """Build a deterministic first-evaluation Time Slot harness."""
    calls = {"activate": 0, "notify": 0, "handle": 0, "sensor_reads": 0}
    clock = {"now": datetime(2026, 8, 21, 1, 0)}
    state = {"value": forecast_state}

    async def _activate():
        calls["activate"] += 1
        forecast_kwh = None
        if state["value"] not in {"unavailable", "unknown", "invalid"}:
            forecast_kwh = 1.5
        return {
            "should_charge": should_charge,
            "solar_forecast_kwh": forecast_kwh,
        }

    async def _notify(**_kwargs):
        calls["notify"] += 1

    async def _handle():
        calls["handle"] += 1

    def _get_state(_entity_id):
        calls["sensor_reads"] += 1
        return SimpleNamespace(
            state=state["value"],
            attributes={"unit_of_measurement": "kWh"},
        )

    async def _async_call(_domain, _service, _data):
        pass

    controller = SimpleNamespace(
        charging_time_slots=["slot"],
        predictive_charging_overridden=False,
        grid_charging_active=False,
        last_evaluation_soc=None,
        _predictive_min_soc_floor=0.0,
        _predictive_min_soc_floor_enabled=False,
        coordinators=[_Coord(50.0, 10.0)],
        max_contracted_power=5000,
        _grid_charging_initialized=False,
        first_execution=False,
        _handle_predictive_grid_charging=_handle,
        _slot_entry_time=None,
        _last_decision_data=None,
        _active_time_slot_quota_kwh=None,
        _forecast_grace_s=300.0,
        _check_time_window=lambda: True,
        solar_forecast_remaining_sensor=(
            "sensor.solar_remaining" if forecast_configured else None
        ),
        solar_forecast_sensor=None,
        hass=SimpleNamespace(
            states=SimpleNamespace(get=_get_state),
            services=SimpleNamespace(async_call=_async_call),
        ),
    )
    engine = PricingManager(hass=controller.hass, controller=controller)
    engine._current_horizon_grid_charging_decision = _activate
    engine._send_predictive_charging_notification = _notify
    engine._apply_time_slot_chronological_plan = (
        lambda decision_data, *, now: decision_data
    )
    engine._now = lambda: clock["now"]
    return engine, controller, calls, state, clock


def test_initial_evaluation_with_valid_forecast_does_not_wait():
    engine, controller, calls, _state, _clock = _make_initial_slot_engine(
        forecast_configured=True,
        forecast_state="1.5",
    )

    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls["activate"] == 1
    assert calls["notify"] == 1
    assert calls["sensor_reads"] == 1
    assert controller.last_evaluation_soc == 50.0


def test_initial_evaluation_without_forecast_does_not_wait_or_read_sensor():
    engine, controller, calls, _state, _clock = _make_initial_slot_engine(
        forecast_configured=False,
        forecast_state="unavailable",
    )

    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls["activate"] == 1
    assert calls["notify"] == 1
    assert calls["sensor_reads"] == 0
    assert controller.last_evaluation_soc == 50.0


def test_initial_evaluation_retries_when_configured_forecast_is_unavailable():
    """Do not publish a one-shot safe-mode notification during a forecast blip."""
    engine, controller, calls, state, clock = _make_initial_slot_engine(
        forecast_configured=True,
        forecast_state="unavailable",
    )

    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls["activate"] == 0
    assert calls["notify"] == 0
    assert controller.last_evaluation_soc is None
    assert controller.grid_charging_active is False

    clock["now"] += timedelta(seconds=120)
    state["value"] = "1.5"
    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls["activate"] == 1
    assert calls["notify"] == 1
    assert controller.last_evaluation_soc == 50.0
    assert controller.grid_charging_active is True


def test_initial_evaluation_retries_when_forecast_still_reads_the_previous_day():
    """A midnight slot must not invent a deficit from the exhausted remainder.

    A 00:00 slot opens a few hundred milliseconds before the provider publishes
    the new day, so its remaining-today sensor still reads 0 kWh. Evaluating it
    produced a phantom full-day deficit and a "STARTED" notification for a
    charge that the next evaluation immediately cancelled.
    """
    engine, controller, calls, state, _clock = _make_initial_slot_engine(
        forecast_configured=True,
        forecast_state="0",
    )

    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls["activate"] == 0
    assert calls["notify"] == 0
    assert controller.grid_charging_active is False

    state["value"] = "40.9"
    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls["activate"] == 1
    assert calls["notify"] == 1


def test_unavailable_forecast_uses_conservative_fallback_after_grace():
    engine, controller, calls, _state, clock = _make_initial_slot_engine(
        forecast_configured=True,
        forecast_state="unavailable",
    )

    asyncio.run(engine.handle_time_slot_predictive_charging())
    clock["now"] += timedelta(seconds=301)
    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls["activate"] == 1
    assert calls["notify"] == 1
    assert controller.last_evaluation_soc == 50.0
    assert controller._last_decision_data["solar_forecast_fallback"] is True


def test_second_time_slot_evaluates_immediately_with_valid_forecast():
    engine, controller, calls, _state, clock = _make_initial_slot_engine(
        forecast_configured=True,
        forecast_state="1.5",
        should_charge=False,
    )
    in_window = {"value": True}
    controller._check_time_window = lambda: in_window["value"]
    engine._ensure_time_slot_chronological_preview = (
        lambda **_kwargs: _noop()
    )
    engine._record_predictive_shortfall = lambda _mode: 0.0

    asyncio.run(engine.handle_time_slot_predictive_charging())
    assert calls["activate"] == 1

    in_window["value"] = False
    clock["now"] += timedelta(minutes=10)
    asyncio.run(engine.handle_time_slot_predictive_charging())
    assert controller.last_evaluation_soc is None

    in_window["value"] = True
    clock["now"] += timedelta(minutes=10)
    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls["activate"] == 2
    assert calls["notify"] == 2
    assert controller.last_evaluation_soc == 50.0


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
    calls = {"activate": 0, "notify": 0}

    async def _activate():
        calls["activate"] += 1
        # Solar-positive day: re-eval at floor finds no deficit → stop charging.
        return {"should_charge": False, "energy_deficit_kwh": 0.0}

    async def _notify(**_kwargs):
        calls["notify"] += 1

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
    engine._send_predictive_charging_notification = _notify
    return engine, controller, calls


def test_floor_recovered_stops_charging():
    # Battery was at 12% (floor_crossed fired, charging started, last_eval_soc=12%).
    # SOC has now climbed to 20% (the floor). floor_recovered must fire a re-eval
    # which finds no deficit → grid_charging_active becomes False.
    engine, ctrl, calls = _make_engine_recovered(soc=20.0, floor=20.0, last_evaluation_soc=12.0)
    asyncio.run(engine.handle_time_slot_predictive_charging())
    assert calls["activate"] == 1
    assert ctrl.grid_charging_active is False


def test_floor_recovered_stops_when_the_current_window_has_no_quota():
    """A floor re-evaluation must not retain a stale grid-charge command."""
    engine, ctrl, calls = _make_engine_recovered(
        soc=20.0, floor=20.0, last_evaluation_soc=12.0
    )
    engine._apply_time_slot_chronological_plan = lambda data, *, now: {
        **data,
        "active_slot_energy_target_kwh": 0.0,
        "should_charge": False,
    }

    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls["activate"] == 1
    assert ctrl.grid_charging_active is False


def test_reversed_decision_replaces_the_slot_notification():
    """A re-evaluation that stops charging must report "Not required".

    Otherwise the slot-entry "STARTED" notification stays on screen for the rest
    of the slot while nothing is charging.
    """
    engine, ctrl, calls = _make_engine_recovered(
        soc=20.0, floor=20.0, last_evaluation_soc=12.0
    )
    asyncio.run(engine.handle_time_slot_predictive_charging())
    assert ctrl.grid_charging_active is False
    assert calls["notify"] == 1


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

    async def _skip_preview(*, now):
        return None

    # These tests isolate slot-exit cleanup; preview evaluation has its own
    # coverage in test_chronological_pricing.py.
    engine._ensure_time_slot_chronological_preview = _skip_preview
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
    test_reversed_decision_replaces_the_slot_notification()
    test_initial_evaluation_retries_when_forecast_still_reads_the_previous_day()
    test_floor_recovered_does_not_fire_below_floor()
    test_floor_recovered_does_not_fire_twice()
    test_slot_exit_resets_eval_soc_after_no_charge_day()
    test_slot_exit_noop_when_nothing_to_clean()
    print("ok")
