"""Time Slot / Real-Time Price windows running through midnight.

Both modes size their charge only until local midnight: the night is meant to
be planned by "the next window". A window that runs continuously through
midnight (e.g. a 22:00-07:00 night tariff) never starts again, so before this
fix the 22:00 decision - "no deficit until 00:00" - stayed in force all night
and the battery reached sunrise empty. The decision is now redone once the
new day starts (from 00:05, like the daily Dynamic Pricing evaluation).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.pricing.engine import PricingManager

DAY = date(2026, 11, 9)
EVENING = datetime(2026, 11, 9, 22, 0)


class _Coord:  # hashable: target maps are keyed by coordinator
    name = "battery"
    data = {"battery_soc": 40.0, "battery_total_energy": 10.0}


async def _noop(*_args, **_kwargs):
    return None


def _time_slot_engine(now, *, eval_date=DAY, should_charge=True):
    coord = _Coord()
    calls = []

    async def _decision():
        calls.append(now)
        return {"should_charge": should_charge}

    def _apply(decision_data, *, now):
        return decision_data

    controller = SimpleNamespace(
        charging_time_slots=["slot"],
        predictive_charging_overridden=False,
        # 22:00 entry decided "nothing needed until midnight".
        grid_charging_active=False,
        _grid_charging_initialized=False,
        _predictive_charge_target_soc=None,
        _active_time_slot_quota_kwh=None,
        last_evaluation_soc=40.0,  # SOC unchanged: no SOC-drop trigger
        _time_slot_eval_date=eval_date,
        _predictive_min_soc_floor=0.0,
        _predictive_min_soc_floor_enabled=False,
        coordinators=[coord],
        max_contracted_power=6900,
        first_execution=False,
        _slot_entry_time=EVENING,
        _last_decision_data=None,
        _check_time_window=lambda: True,
        solar_forecast_remaining_sensor=None,
        solar_forecast_sensor=None,
        _handle_predictive_grid_charging=_noop,
        hass=SimpleNamespace(),
    )
    engine = PricingManager(hass=controller.hass, controller=controller)
    engine._current_horizon_grid_charging_decision = _decision
    engine._apply_time_slot_chronological_plan = _apply
    engine._send_predictive_charging_notification = _noop
    engine._refresh_solar_forecast_reference = lambda _now: None
    engine._is_solar_forecast_reeval = lambda _now: False
    engine._now = lambda: now
    return engine, controller, calls


def test_overnight_window_is_re_evaluated_after_midnight():
    now = datetime(2026, 11, 10, 0, 5)
    engine, controller, calls = _time_slot_engine(now)

    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls == [now]
    assert controller.grid_charging_active is True
    assert controller._time_slot_eval_date == now.date()
    # The forecast grace clock restarts for the new day's read.
    assert controller._slot_entry_time == now


def test_new_day_re_evaluation_runs_once():
    now = datetime(2026, 11, 10, 0, 5)
    engine, controller, calls = _time_slot_engine(now)

    asyncio.run(engine.handle_time_slot_predictive_charging())
    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls == [now]


def test_no_re_evaluation_between_midnight_and_0005():
    engine, controller, calls = _time_slot_engine(datetime(2026, 11, 10, 0, 2))

    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls == []
    assert controller._time_slot_eval_date == DAY


def test_no_re_evaluation_on_the_same_day():
    engine, _controller, calls = _time_slot_engine(datetime(2026, 11, 9, 23, 30))

    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls == []


def test_initial_evaluation_records_its_date():
    now = datetime(2026, 11, 9, 22, 0)
    engine, controller, calls = _time_slot_engine(now, eval_date=None)
    controller.last_evaluation_soc = None
    controller._slot_entry_time = None

    asyncio.run(engine.handle_time_slot_predictive_charging())

    assert calls == [now]
    assert controller._time_slot_eval_date == DAY


@pytest.mark.parametrize(
    ("eval_date", "now", "expected"),
    [
        (None, datetime(2026, 11, 10, 1, 0), False),
        (DAY, datetime(2026, 11, 9, 23, 59), False),
        (DAY, datetime(2026, 11, 10, 0, 4, 59), False),
        (DAY, datetime(2026, 11, 10, 0, 5), True),
        (DAY, datetime(2026, 11, 11, 12, 0), True),
    ],
)
def test_is_new_day_since(eval_date, now, expected):
    assert PricingManager._is_new_day_since(eval_date, now) is expected


def test_is_new_day_since_ignores_the_initial_evaluation():
    assert (
        PricingManager._is_new_day_since(
            DAY, datetime(2026, 11, 10, 3, 0), is_initial_eval=True
        )
        is False
    )


def _realtime_engine(now, *, should_charge):
    coord = _Coord()
    calls = []
    handled = []

    async def _decision():
        calls.append(now)
        return {"should_charge": should_charge}

    async def _handle():
        handled.append(now)

    controller = SimpleNamespace(
        price_sensor="sensor.price",
        average_price_sensor=None,
        max_price_threshold=0.20,
        predictive_charging_overridden=False,
        # Cheap run started at 23:00, sized until midnight.
        _realtime_price_charging=True,
        _realtime_eval_date=DAY,
        grid_charging_active=True,
        _grid_charging_initialized=True,
        _predictive_charge_target_soc={coord: 55.0},
        _last_decision_data=None,
        previous_power=0,
        previous_error=0,
        _is_operation_allowed=lambda is_charging=True: True,
        _handle_predictive_grid_charging=_handle,
    )
    engine = PricingManager(hass=SimpleNamespace(), controller=controller)
    engine._get_current_price = lambda: 0.10  # still cheap
    engine._current_horizon_grid_charging_decision = _decision
    engine._reset_predictive_demand_runtime = lambda: None
    engine._now = lambda: now
    return engine, controller, calls, handled


def test_realtime_run_through_midnight_resizes_its_target():
    now = datetime(2026, 11, 10, 0, 5)
    engine, controller, calls, handled = _realtime_engine(now, should_charge=True)

    asyncio.run(engine.handle_realtime_price_predictive_charging())

    assert calls == [now]
    assert controller._realtime_eval_date == now.date()
    assert controller._predictive_charge_target_soc is None  # recomputed
    assert controller._realtime_price_charging is True
    assert handled == [now]


def test_realtime_run_stops_when_the_new_day_needs_nothing():
    now = datetime(2026, 11, 10, 0, 5)
    engine, controller, calls, handled = _realtime_engine(now, should_charge=False)

    asyncio.run(engine.handle_realtime_price_predictive_charging())

    assert calls == [now]
    assert controller._realtime_price_charging is False
    assert controller.grid_charging_active is False
    assert handled == []


def test_realtime_run_on_the_same_day_is_not_re_evaluated():
    now = datetime(2026, 11, 9, 23, 30)
    engine, controller, calls, handled = _realtime_engine(now, should_charge=True)

    asyncio.run(engine.handle_realtime_price_predictive_charging())

    assert calls == []
    assert controller._predictive_charge_target_soc is not None
    assert handled == [now]
