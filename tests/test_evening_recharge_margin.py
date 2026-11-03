"""The evening re-evaluation applies the same charge ceiling as the 00:05 plan.

A grid-charge slot has to clear the arbitrage margin: the energy it stores,
after round-trip losses, must be worth more than the slot costs plus the
margin. Guaranteed-floor energy is the exception: it ignores the margin, and
only the user's own price cap limits it.

The evening path runs the real chronological planner here, with a flat
consumption profile and no solar left for the day.
"""
from __future__ import annotations

import asyncio
import datetime as datetime_module
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.tracking.consumption_profile import (
    ConsumptionForecast,
)

_NOW = datetime(2026, 9, 16, 16, 0)
_LOAD_KW = 0.4
_CAPACITY_KWH = 10.0


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(datetime_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(pricing_engine.calculations, "datetime", _FixedDateTime)


def _prices(overrides=None):
    """Hourly prices from 16:00 to 07:00: 0.50 until 21:00, then 0.45.

    The 0.50 peak caps what stored energy is worth, so with the default
    efficiency (0.85) and a 0.04 margin no slot above about 0.385 qualifies.
    ``overrides`` maps an hour of the day to a different price.
    """
    slots = []
    for offset in range(15):
        start = _NOW + timedelta(hours=offset)
        price = 0.50 if start.hour < 21 and start.day == _NOW.day else 0.45
        price = (overrides or {}).get(start.hour, price)
        slots.append(PriceSlot(start, start + timedelta(hours=1), price))
    return slots


def _manager(*, prices, floor=None, **controller_overrides):
    async def _avg():
        return _LOAD_KW * 24

    def _forecast_between(start, end, *, fallback="legacy_daily"):
        hours = (end - start).total_seconds() / 3600.0
        return ConsumptionForecast(
            _LOAD_KW * hours, [_LOAD_KW * 0.25] * 96, "profile", True
        )

    tracker = SimpleNamespace(
        consumption_profile=SimpleNamespace(),
        forecast_consumption_between=_forecast_between,
        get_dynamic_base_consumption=_avg,
        solar_profile=SimpleNamespace(_days={}, get_snapshot=lambda **_k: None),
        calculate_sunrise=lambda for_date=None: 7.0,
        calculate_solar_noon=lambda: 13.0,
    )
    controller = SimpleNamespace(
        coordinators=[
            SimpleNamespace(
                data={"battery_soc": 20.0, "battery_total_energy": _CAPACITY_KWH},
                min_soc=10,
                max_soc=100,
            )
        ],
        config_entry=SimpleNamespace(data={}, options={}),
        _consumption_tracker=tracker,
        _last_decision_data={},
        _dynamic_pricing_schedule=None,
        _dynamic_pricing_evaluated_date=None,
        _dp_last_eval_soc=None,
        _dp_last_eval_excluded_claim_kwh=None,
        _external_loads=None,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        dp_price_discharge_control=False,
        solar_profile_mode="off",
        _predictive_safety_margin_kwh=0.0,
        _predictive_min_soc_floor=floor or 0.0,
        _predictive_min_soc_floor_enabled=floor is not None,
        charge_delay_enabled=False,
        _solar_t_start=None,
        _is_battery_manual_owned=lambda _c: False,
        _charge_ceiling_soc=lambda _c: 100.0,
        max_contracted_power=3000.0,
        max_charge_capacity=3000.0,
        max_price_threshold=None,
        min_arbitrage_margin=0.04,
        round_trip_efficiency=DEFAULT_ROUND_TRIP_EFFICIENCY,
    )
    for name, value in controller_overrides.items():
        setattr(controller, name, value)

    manager = PricingManager(
        SimpleNamespace(
            config=SimpleNamespace(time_zone="UTC"),
            states=SimpleNamespace(get=lambda _e: None),
        ),
        controller,
    )

    async def _noop(*_args, **_kwargs):
        return None

    async def _remaining(*, now=None, horizon_end=None):
        # 1 kWh usable against 6 kWh of load before sunrise: the battery
        # runs empty around 18:30. A battery at 20% sits under a 30% floor's
        # band, so the floor is what asks for the charge.
        return {
            "should_charge": True,
            "floor_active": floor is not None,
            "remaining_consumption_kwh": _LOAD_KW * 15,
            "consumption_rate_kwh_h": _LOAD_KW,
            "avg_consumption_kwh": _LOAD_KW * 15,
            "excluded_demand_claim_kwh": 0.0,
        }

    manager._maybe_refresh_service_prices = _noop
    manager._send_evening_recharge_notification = _noop
    manager._store_chronological_diagnostics = lambda _d: None
    manager._remaining_solar_today_kwh = lambda _now: 0.0
    manager._evaluate_remaining_grid_charging = _remaining
    manager._parse_price_data = lambda horizon_end=None, **_kwargs: list(prices)
    manager.get_future_price_slots = lambda horizon_end=None: list(prices)
    return manager


def _booked(manager):
    asyncio.run(manager._evaluate_evening_recharge())
    schedule = manager._controller._dynamic_pricing_schedule
    if schedule is None:
        return []
    return [(slot.start.hour, slot.price) for slot in schedule.selected_slots]


def test_evening_plan_books_nothing_when_no_slot_clears_the_margin():
    # Every slot costs 0.45 or more, while a stored kWh is worth at most
    # 0.50 x 0.85 - 0.04 = 0.385.
    assert _booked(_manager(prices=_prices())) == []


def test_evening_plan_books_only_the_slots_that_clear_the_margin():
    booked = _booked(_manager(prices=_prices({17: 0.20})))

    assert booked == [(17, 0.20)]


def test_evening_plan_without_a_margin_charges_for_the_deficit():
    booked = _booked(_manager(prices=_prices(), min_arbitrage_margin=None))

    assert booked
    assert all(price >= 0.45 for _hour, price in booked)


def test_evening_fallback_without_a_consumption_profile_applies_the_margin():
    manager = _manager(prices=_prices())
    manager._controller._consumption_tracker.consumption_profile = None

    assert _booked(manager) == []


def test_guaranteed_floor_energy_ignores_the_margin():
    # The same prices book nothing for an ordinary deficit, but still charge
    # a battery that sits under its guaranteed floor.
    assert _booked(_manager(prices=_prices())) == []

    booked = _booked(_manager(prices=_prices(), floor=30.0))

    assert booked
    assert all(price >= 0.45 for _hour, price in booked)


def test_guaranteed_floor_energy_keeps_the_manual_price_cap():
    booked = _booked(
        _manager(
            prices=_prices({22: 0.44, 23: 0.44}),
            floor=30.0,
            max_price_threshold=0.44,
        )
    )

    assert booked
    assert all(price <= 0.44 for _hour, price in booked)
