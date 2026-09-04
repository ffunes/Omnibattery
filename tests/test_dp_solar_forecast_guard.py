"""The 00:05 evaluation must not plan on a forecast sensor that reads zero.

A provider that has not published the new day yet reports 0 kWh remaining
minutes after midnight. Planning on that books a full day of grid charging the
sun would have covered, and no later re-evaluation can withdraw those slots.
The daily run therefore reuses the price-data retry ladder instead.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from custom_components.omnibattery.const import (
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    SOLAR_FORECAST_DAILY_RETRY_LIMIT,
)
from custom_components.omnibattery.pricing.engine import (
    DynamicPricingEvaluationHorizon,
    PricingManager,
)
from custom_components.omnibattery.pricing import PriceSlot


def _decision(solar_forecast_kwh):
    return {
        "should_charge": True,
        "avg_soc": 30.0,
        "avg_consumption_kwh": 8.0,
        "energy_deficit_kwh": 3.0,
        "planned_grid_charge_kwh": 3.0,
        "solar_forecast_kwh": solar_forecast_kwh,
        "usable_energy_kwh": 2.0,
        "total_available_kwh": 2.0,
        "days_in_history": 30,
    }


def _controller(solar_forecast_kwh, *, remaining_sensor="sensor.solar_remaining"):
    async def should_activate(**_kwargs):
        return _decision(solar_forecast_kwh)

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
        _dp_eval_retry_count=0,
        _dp_last_eval_soc=None,
        _dp_last_eval_solar_remaining_kwh=None,
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
    start = datetime.now() + timedelta(hours=2)
    return [
        PriceSlot(
            start=start + timedelta(hours=index),
            end=start + timedelta(hours=index + 1),
            price=0.10,
        )
        for index in range(6)
    ]


def _run_daily(ctrl):
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._maybe_refresh_service_prices = _noop
    manager._parse_price_data = lambda horizon_end=None: _slots()
    manager._send_dynamic_pricing_notification = _noop
    manager._remaining_solar_today_kwh = lambda _now: 0.0
    asyncio.run(
        manager._evaluate_dynamic_pricing(
            horizon=DynamicPricingEvaluationHorizon.DAILY,
        )
    )
    return manager


def test_zero_forecast_defers_the_daily_plan():
    ctrl = _controller(0.0)
    _run_daily(ctrl)

    assert ctrl._dp_eval_retry_count == 1
    # The date stays unset so the control loop comes back within the hour.
    assert ctrl._dynamic_pricing_evaluated_date is None
    assert ctrl._dynamic_pricing_schedule is None


def test_missing_forecast_plans_normally():
    # No reported figure at all means the balance never used the remaining
    # sensor. Deferring on that would stall a day for the wrong reason.
    ctrl = _controller(None)
    _run_daily(ctrl)

    assert ctrl._dp_eval_retry_count == 0
    assert ctrl._dynamic_pricing_evaluated_date is not None


def test_exhausted_retries_plan_anyway():
    # Four deferrals is where a genuinely broken sensor stops blocking the day:
    # booking the slots beats running dry.
    ctrl = _controller(0.0)
    ctrl._dp_eval_retry_count = SOLAR_FORECAST_DAILY_RETRY_LIMIT
    _run_daily(ctrl)

    # A completed evaluation clears the ladder, so the day is planned and the
    # counter is back to zero rather than stuck at the limit.
    assert ctrl._dynamic_pricing_evaluated_date is not None
    assert ctrl._dp_eval_retry_count == 0


def test_real_forecast_plans_normally():
    ctrl = _controller(12.5)
    _run_daily(ctrl)

    assert ctrl._dp_eval_retry_count == 0
    assert ctrl._dynamic_pricing_evaluated_date is not None


def test_zero_forecast_without_configured_sensor_plans_normally():
    # Without a remaining-today sensor the legacy path owns the balance; a zero
    # there is not evidence of an unpublished provider day.
    ctrl = _controller(0.0, remaining_sensor=None)
    _run_daily(ctrl)

    assert ctrl._dp_eval_retry_count == 0
    assert ctrl._dynamic_pricing_evaluated_date is not None
