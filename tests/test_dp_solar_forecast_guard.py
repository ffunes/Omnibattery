"""The daily evaluation must not plan on a forecast sensor that reads zero.

A provider that has not published the new day yet reports 0 kWh remaining
minutes after midnight. Planning on that books a full day of grid charging the
sun would have covered, and no later re-evaluation can withdraw those slots.
The evaluation therefore defers and reuses the price-data retry ladder.

The ladder re-invokes the evaluation with ``REMAINING``, so these tests drive
both horizons: a guard that only understood ``DAILY`` would defer once and then
plan on the very same zero.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    SOLAR_FORECAST_DAILY_RETRY_LIMIT,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing import engine as engine_module
from custom_components.omnibattery.pricing.engine import (
    DynamicPricingEvaluationHorizon,
    PricingManager,
)

_MIDNIGHT_RUN = datetime(2026, 9, 4, 0, 5)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """Pin the evaluation clock; the guard is gated on the first hour."""

    class _FixedDatetime(datetime):
        _value = _MIDNIGHT_RUN

        @classmethod
        def now(cls, tz=None):
            return cls._value

    monkeypatch.setattr(engine_module, "datetime", _FixedDatetime)
    return _FixedDatetime


def _decision():
    return {
        "should_charge": True,
        "avg_soc": 30.0,
        "avg_consumption_kwh": 8.0,
        "energy_deficit_kwh": 3.0,
        "planned_grid_charge_kwh": 3.0,
        "solar_forecast_kwh": 0.0,
        "usable_energy_kwh": 2.0,
        "total_available_kwh": 2.0,
        "days_in_history": 30,
    }


def _controller(*, remaining_sensor="sensor.solar_remaining", retry_count=0):
    async def should_activate(**_kwargs):
        return _decision()

    return SimpleNamespace(
        coordinators=[],
        predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        predictive_charging_overridden=False,
        negative_price_charging_enabled=False,
        smart_predischarge_enabled=False,
        solar_forecast_remaining_sensor=remaining_sensor,
        max_contracted_power=4000,
        max_charge_capacity=4000,
        max_price_threshold=None,
        min_arbitrage_margin=None,
        round_trip_efficiency=DEFAULT_ROUND_TRIP_EFFICIENCY,
        price_integration_type="nordpool",
        price_sensor="sensor.price",
        _should_activate_grid_charging=should_activate,
        _consumption_tracker=None,
        _last_decision_data=None,
        _last_chronological_diagnostics=None,
        _dynamic_pricing_schedule=None,
        _dynamic_pricing_evaluated_date=None,
        _dp_eval_retry_count=retry_count,
        _dp_last_eval_soc=None,
        _dp_last_eval_solar_remaining_kwh=None,
        _dp_last_eval_solar_produced_kwh=None,
        _daily_solar_energy_kwh=0.0,
        _daily_solar_energy_date=None,
        _dp_arbitrage_ceiling=None,
        _dp_daily_avg_price=None,
        _dp_pre_evaluated_slots={},
        _dp_pre_evaluated_purposes={},
        _dp_completed_slots=set(),
        _current_price_slot_active=False,
        _active_dynamic_slot_purpose=None,
        _predictive_charge_target_soc=None,
        _external_loads=None,
        grid_charging_active=False,
        _grid_charging_initialized=False,
    )


async def _noop(*_args, **_kwargs):
    return None


def _slots():
    start = _MIDNIGHT_RUN + timedelta(hours=8)
    return [
        PriceSlot(
            start=start + timedelta(hours=index),
            end=start + timedelta(hours=index + 1),
            price=0.10,
        )
        for index in range(6)
    ]


def _run(ctrl, reading, horizon=DynamicPricingEvaluationHorizon.DAILY):
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._maybe_refresh_service_prices = _noop
    manager._parse_price_data = lambda horizon_end=None: _slots()
    manager._send_dynamic_pricing_notification = _noop
    manager._read_remaining_solar_reading = lambda _now: reading
    manager._remaining_solar_today_kwh = lambda _now: reading or 0.0
    asyncio.run(manager._evaluate_dynamic_pricing(horizon=horizon))
    return manager


def test_zero_forecast_defers_the_daily_plan():
    ctrl = _controller()
    _run(ctrl, 0.0)

    assert ctrl._dp_eval_retry_count == 1
    # The date stays unset so the control loop comes back within the hour.
    assert ctrl._dynamic_pricing_evaluated_date is None
    assert ctrl._dynamic_pricing_schedule is None


def test_zero_forecast_defers_the_ladder_retry_too():
    # The retry ladder re-invokes with REMAINING. A horizon-gated guard would
    # plan the full day of grid charging on the second call.
    ctrl = _controller(retry_count=1)
    _run(ctrl, 0.0, horizon=DynamicPricingEvaluationHorizon.REMAINING)

    assert ctrl._dp_eval_retry_count == 2
    assert ctrl._dynamic_pricing_evaluated_date is None


def test_last_ladder_attempt_plans_anyway():
    # A genuinely broken sensor must not leave the battery empty all day.
    ctrl = _controller(retry_count=SOLAR_FORECAST_DAILY_RETRY_LIMIT)
    _run(ctrl, 0.0, horizon=DynamicPricingEvaluationHorizon.REMAINING)

    assert ctrl._dynamic_pricing_evaluated_date is not None


def test_daily_run_clears_a_stale_retry_counter():
    # Yesterday never completed an evaluation, so the control handler's midnight
    # reset never ran. A stale count must not disable this morning's ladder.
    ctrl = _controller(retry_count=SOLAR_FORECAST_DAILY_RETRY_LIMIT)
    _run(ctrl, 0.0)

    assert ctrl._dp_eval_retry_count == 1
    assert ctrl._dynamic_pricing_evaluated_date is None


def test_unavailable_forecast_plans_normally():
    # None is "the sensor said nothing", not "no sun today". Holding the day on
    # a dropout would be the same failure in reverse.
    ctrl = _controller()
    _run(ctrl, None)

    assert ctrl._dp_eval_retry_count == 0
    assert ctrl._dynamic_pricing_evaluated_date is not None


def test_real_forecast_plans_normally():
    ctrl = _controller()
    _run(ctrl, 12.5)

    assert ctrl._dp_eval_retry_count == 0
    assert ctrl._dynamic_pricing_evaluated_date is not None


def test_zero_forecast_without_configured_sensor_plans_normally():
    # Without a remaining-today sensor the legacy path owns the balance; a zero
    # there is not evidence of an unpublished provider day.
    ctrl = _controller(remaining_sensor=None)
    _run(ctrl, 0.0)

    assert ctrl._dp_eval_retry_count == 0
    assert ctrl._dynamic_pricing_evaluated_date is not None


def test_zero_forecast_outside_the_first_hour_plans_normally(_frozen_clock):
    # A manual rebuild at midday is never held back: by then a zero is the
    # provider's real answer for the rest of the day.
    _frozen_clock._value = datetime(2026, 9, 4, 12, 0)
    ctrl = _controller()
    _run(ctrl, 0.0, horizon=DynamicPricingEvaluationHorizon.REMAINING)

    assert ctrl._dp_eval_retry_count == 0
    assert ctrl._dynamic_pricing_evaluated_date is not None
