"""The evening re-evaluation applies the same charge ceiling as the 00:05 plan.

A grid-charge slot has to clear the arbitrage margin: the energy it stores,
after round-trip losses, must be worth more than the slot costs plus the
margin. Guaranteed-floor energy is the exception: it ignores the margin, and
only the user's own price cap limits it. The exception covers the floor
energy alone, never the ordinary deficit around it (#724).

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
    monkeypatch.setattr(pricing_engine, "datetime", _FixedDateTime)
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


def _manager(*, prices, floor=None, load_kw=_LOAD_KW, **controller_overrides):
    async def _avg():
        return load_kw * 24

    def _forecast_between(start, end, *, fallback="legacy_daily"):
        hours = (end - start).total_seconds() / 3600.0
        return ConsumptionForecast(
            load_kw * hours, [load_kw * 0.25] * 96, "profile", True
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
        # 1 kWh usable (20% SOC, 10% cutoff) and no solar left. With the
        # default load that is 6 kWh before sunrise, so the battery runs
        # empty around 18:30. A floor asks for the energy up to it; it is the binding
        # constraint only when it exceeds the ordinary deficit, as in the
        # controller's own balance.
        consumption = load_kw * 15
        floor_deficit = max(0.0, ((floor or 0.0) - 20.0) / 100.0 * _CAPACITY_KWH)
        return {
            "should_charge": True,
            "floor_active": floor_deficit > max(0.0, consumption - 1.0),
            "floor_deficit_kwh": floor_deficit,
            "remaining_consumption_kwh": consumption,
            "consumption_rate_kwh_h": load_kw,
            "avg_consumption_kwh": consumption,
            "total_available_kwh": 1.0,
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
    # A 40% floor asks for 2 kWh against a 0.5 kWh ordinary deficit.
    assert _booked(_manager(prices=_prices(), load_kw=0.1)) == []

    booked = _booked(_manager(prices=_prices(), floor=40.0, load_kw=0.1))

    assert booked
    assert all(price >= 0.45 for _hour, price in booked)


def test_guaranteed_floor_energy_keeps_the_manual_price_cap():
    booked = _booked(
        _manager(
            prices=_prices({22: 0.44, 23: 0.44}),
            floor=40.0,
            load_kw=0.1,
            max_price_threshold=0.44,
        )
    )

    assert booked
    assert all(price <= 0.44 for _hour, price in booked)


# ----------------------------------------------------------------------
# A merged schedule reports the merged plan (#724, 3 Nov)
# ----------------------------------------------------------------------

from custom_components.omnibattery.pricing import DynamicPricingSchedule  # noqa: E402


def _earlier_schedule(slot, **fields):
    """The 13:37 plan of #724: one cheap 15-minute slot, armed."""
    return DynamicPricingSchedule(
        hours_needed=0.25,
        selected_slots=[slot],
        average_price=slot.price,
        estimated_cost=slot.price * 2.5 * 0.25,
        total_available_slots=40,
        evaluation_time=_NOW - timedelta(hours=2, minutes=23),
        energy_deficit_kwh=0.6,
        charging_needed=True,
        deficit_charging_needed=True,
        slot_purposes={slot: "deficit"},
        **fields,
    )


def _merged(**fields):
    early = PriceSlot(_NOW + timedelta(hours=11), _NOW + timedelta(hours=11, minutes=15), 0.30)
    manager = _manager(prices=_prices(), min_arbitrage_margin=None)
    manager._controller._dynamic_pricing_schedule = _earlier_schedule(early, **fields)
    asyncio.run(manager._evaluate_evening_recharge())
    return early, manager._controller._dynamic_pricing_schedule


def test_a_merged_schedule_reports_the_merged_price_cost_and_time():
    early, schedule = _merged()

    assert len(schedule.selected_slots) > 1
    prices = [slot.price for slot in schedule.selected_slots]
    assert schedule.average_price == pytest.approx(sum(prices) / len(prices))
    assert schedule.estimated_cost == pytest.approx(
        sum(
            slot.price * 3.0 * (slot.end - slot.start).total_seconds() / 3600.0
            for slot in schedule.selected_slots
        )
    )
    assert schedule.evaluation_time == _NOW


def test_a_merged_schedule_weighs_the_new_slots_by_their_planned_energy():
    """With per-slot energy on the earlier plan, the evening slots bring theirs."""
    early = PriceSlot(_NOW + timedelta(hours=11), _NOW + timedelta(hours=11, minutes=15), 0.30)
    _early, schedule = _merged(slot_cost_energy_kwh={early: 0.6})

    assert all(slot in schedule.slot_cost_energy_kwh for slot in schedule.selected_slots)
    assert schedule.estimated_cost > 0.6 * 0.30
    assert schedule.evaluation_time == _NOW


# ----------------------------------------------------------------------
# A new evening schedule prices the energy it plans (#715)
# ----------------------------------------------------------------------

from custom_components.omnibattery.const import CHARGE_EFFICIENCY  # noqa: E402


def _full_power_cost(schedule):
    return sum(
        slot.price * 3.0 * (slot.end - slot.start).total_seconds() / 3600.0
        for slot in schedule.selected_slots
    )


def test_a_new_evening_schedule_weighs_its_slots_by_their_planned_energy():
    manager = _manager(prices=_prices(), min_arbitrage_margin=None)
    asyncio.run(manager._evaluate_evening_recharge())
    schedule = manager._controller._dynamic_pricing_schedule
    energy = schedule.slot_cost_energy_kwh

    assert schedule.selected_slots
    assert set(energy) == set(schedule.selected_slots)
    battery_cost = sum(slot.price * kwh for slot, kwh in energy.items())
    assert schedule.estimated_cost == pytest.approx(battery_cost / CHARGE_EFFICIENCY)
    assert schedule.average_price == pytest.approx(battery_cost / sum(energy.values()))
    # Every slot at full power for its whole hour priced energy never planned.
    assert schedule.estimated_cost < _full_power_cost(schedule)


def test_a_new_evening_schedule_without_a_profile_assumes_full_power():
    manager = _manager(prices=_prices(), min_arbitrage_margin=None)
    manager._controller._consumption_tracker.consumption_profile = None
    asyncio.run(manager._evaluate_evening_recharge())
    schedule = manager._controller._dynamic_pricing_schedule

    assert schedule.selected_slots
    assert schedule.slot_cost_energy_kwh == {}
    assert schedule.estimated_cost == pytest.approx(_full_power_cost(schedule))


# ----------------------------------------------------------------------
# A re-plan without new slots drops the old pre-slot decisions
# (REVIEW_FINDINGS.md, 9 Nov)
# ----------------------------------------------------------------------

from custom_components.omnibattery.pricing import (  # noqa: E402
    SLOT_PURPOSE_COMBINED,
    SLOT_PURPOSE_DEFICIT,
    SLOT_PURPOSE_NEGATIVE_PRICE,
)


def _kept_only(prices, slot, purpose, cached_purpose):
    """Evening pass over an armed schedule holding ``slot`` only, with an old
    pre-slot decision cached for it and nothing charged in it yet."""
    manager = _manager(prices=prices)
    controller = manager._controller
    controller._dynamic_pricing_schedule = DynamicPricingSchedule(
        hours_needed=1.0,
        selected_slots=[slot],
        average_price=slot.price,
        estimated_cost=slot.price * 0.5 / CHARGE_EFFICIENCY,
        total_available_slots=15,
        evaluation_time=_NOW - timedelta(hours=16),
        energy_deficit_kwh=0.5,
        charging_needed=True,
        deficit_charging_needed=True,
        slot_purposes={slot: purpose},
        slot_energy_targets_kwh={slot: 0.5},
        slot_cost_energy_kwh={slot: 0.5},
        chronological_planning_active=True,
    )
    controller._dp_pre_evaluated_slots = {slot.start: False}
    controller._dp_pre_evaluated_purposes = {slot.start: cached_purpose}
    asyncio.run(manager._evaluate_evening_recharge())
    return manager, controller._dynamic_pricing_schedule


def test_a_kept_slot_the_replan_needs_is_not_held_by_an_old_veto():
    """The early return kept the cached veto, which overrides the schedule; the
    pre-slot gate never re-runs for a decided slot, so the quota
    the new plan gave the slot was never charged."""
    prices = _prices({22: 0.10})  # the only slot that clears the margin
    slot = next(s for s in prices if s.start.hour == 22)

    manager, schedule = _kept_only(prices, slot, SLOT_PURPOSE_DEFICIT, None)

    assert schedule.selected_slots == [slot]  # no new slot: the early return
    assert schedule.slot_energy_targets_kwh[slot] > 0.5  # re-planned quota
    assert manager._controller._dp_pre_evaluated_slots == {}
    assert manager._controller._dp_pre_evaluated_purposes == {}
    assert manager._effective_slot_purpose(slot) == SLOT_PURPOSE_DEFICIT
    # The summary describes the re-planned quota, not the 00:05 one.
    assert schedule.slot_cost_energy_kwh[slot] == schedule.slot_energy_targets_kwh[slot]
    assert schedule.estimated_cost == pytest.approx(
        slot.price * schedule.slot_energy_targets_kwh[slot] / CHARGE_EFFICIENCY
    )
    assert schedule.evaluation_time == _NOW


def test_a_cached_combined_purpose_cannot_outlive_the_replan():
    """The re-plan books no deficit in the slot, so it keeps only its
    negative-price purpose; the old cached ``combined`` must not restore the
    deficit charge."""
    prices = _prices()  # nothing clears the margin: the plan books nothing
    slot = next(s for s in prices if s.start.hour == 22)

    manager, schedule = _kept_only(
        prices, slot, SLOT_PURPOSE_COMBINED, SLOT_PURPOSE_COMBINED
    )

    assert schedule.slot_purposes[slot] == SLOT_PURPOSE_NEGATIVE_PRICE
    assert slot not in schedule.slot_energy_targets_kwh
    assert schedule.deficit_charging_needed is False
    assert manager._controller._dp_pre_evaluated_purposes == {}
