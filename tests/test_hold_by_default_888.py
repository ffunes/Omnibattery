"""#888 through the engine: Dynamic Pricing holds without any switch.

The plan's tests 1-3, run as Dynamic Pricing runs them (the engine builds the
request, the controller receives the holds), not on a hand-made request: before
#888 the engine let the optimizer hold only with the Discharge Reserve switch
on, so none of these publish a hold there.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.optimizer import optimize_charge
from custom_components.omnibattery.solar_forecast import SolarForecastPeriod
from tests import test_predictive_scenarios as h

TZ = "Europe/Madrid"
Q = timedelta(minutes=15)


def _scenario(name, *, now, prices, bins, fleet, efficiency, margin, periods=(), remaining=0.0,
              sunrise=8.0):
    """``prices``: hour -> price for every quarter of the days covered."""
    days = sorted({now.date(), (now + timedelta(days=1)).date()})
    slots = tuple(
        PriceSlot(start, start + Q, prices(start))
        for day in days
        for q in range(96)
        for start in [datetime.combine(day, datetime.min.time()) + q * Q]
    )
    return h.Scenario(
        name=name, level="engine", now=now, time_zone=TZ, sunrise_h=sunrise,
        solar_noon_h=13.25, slots=slots, bins_kwh=tuple(bins), solar_remaining_kwh=remaining,
        solar_periods=tuple(periods), fleet=tuple(fleet),
        settings=dict(max_price_threshold=1.0, min_arbitrage_margin=margin,
                      round_trip_efficiency=efficiency, peak_shaving=None,
                      contracted_power_w=5750, guaranteed_floor_soc=None, safety_margin_pct=0.0),
        expected={},
    )


def _run(scenario, monkeypatch):
    box = {}
    real = h._controller

    def controller(s):
        box["c"] = real(s)
        return box["c"]

    monkeypatch.setattr(h, "_controller", controller)
    outcome = h._run_engine(scenario, monkeypatch)
    assert outcome.planner == "optimizer"
    return outcome, box["c"]._dp_plan_holds


def _sun(day, kwh):
    zone = ZoneInfo(TZ)
    hours = range(9, 18)
    weights = [min(hr - 8, 18 - hr) for hr in hours]
    return [
        SolarForecastPeriod(
            (day + timedelta(hours=hr)).replace(tzinfo=zone),
            (day + timedelta(hours=hr + 1)).replace(tzinfo=zone),
            kwh * w / sum(weights),
        )
        for hr, w in zip(hours, weights)
    ]


def test_anorakophobia_s_night_is_held_for_the_dearer_day(monkeypatch):
    """Quarter-hour prices, 13.3 kWh holding 4.1 kWh at 20:30, margin 0.05,
    round trip 0.82, night 0.40-0.44 and a dearer day (0.49-0.53). No charge
    pays; the battery is held through the night and spent in the day. The
    shape, not their 7.60 EUR: their load and curve are unknown."""
    now = datetime(2026, 11, 10, 20, 30)

    def price(t):
        q = (t.hour * 4 + t.minute // 15) % 10
        if t.hour >= 20 and t.date() == now.date() or t.hour < 7:
            return 0.40 + 0.004 * ((q * 7) % 10)
        return 0.49 + 0.004 * ((q * 3) % 10)

    bins = [0.35 * 0.25] * 28 + [0.9 * 0.25] * 12 + [0.4 * 0.25] * 32 + [1.0 * 0.25] * 24
    fleet = [dict(capacity_kwh=13.3, max_charge_w=2500, max_discharge_w=2500,
                  soc=10 + 4.1 / 13.3 * 100, min_soc=10, max_soc=100)]
    outcome, holds = _run(_scenario("anorakophobia", now=now, prices=price, bins=bins, fleet=fleet,
                                    efficiency=0.82, margin=0.05), monkeypatch)
    assert not outcome.booked
    assert holds is not None
    held = {s.start for s in holds}
    dawn = now.replace(hour=0, minute=0) + timedelta(days=1, hours=7)
    night = [s.start for s in outcome.request.price_slots if now <= s.start < dawn]
    assert len(night) == 42 and all(t in held for t in night)
    assert outcome.plan.hold_starts == 1  # one run, not split by the price zigzag
    energy = dict(outcome.plan.trajectory)
    assert energy[datetime(2026, 11, 11, 7)] == pytest.approx(outcome.request.usable_initial_kwh, abs=1e-6)


def test_a_2_0td_valley_lets_the_grid_carry_the_house_with_one_charge(monkeypatch):
    """#888's announcement: 10.4 kWh at 70 %, a cloudy day on 2.0TD prices. In
    the valley the grid carries the house (the battery is held) and the plan
    charges once."""
    now = datetime(2026, 11, 10, 0, 5)

    def price(t):
        return 0.08 if t.hour < 8 else (0.25 if 10 <= t.hour < 14 or 18 <= t.hour < 22 else 0.15)

    bins = [0.3 * 0.25] * 28 + [0.8 * 0.25] * 8 + [0.5 * 0.25] * 36 + [1.4 * 0.25] * 16 + [0.4 * 0.25] * 8
    fleet = [dict(capacity_kwh=10.4, max_charge_w=2500, max_discharge_w=2500,
                  soc=70.0, min_soc=10, max_soc=100)]
    day = datetime(2026, 11, 10)
    periods = _sun(day, 2.0) + _sun(day + timedelta(days=1), 2.0)
    outcome, holds = _run(_scenario("2.0TD", now=now, prices=price, bins=bins, fleet=fleet,
                                    efficiency=0.85, margin=None, periods=periods, remaining=2.0),
                          monkeypatch)
    assert holds is not None
    # Tonight's valley (tomorrow's prices are out, so tomorrow's valley charges too).
    valley = sorted(s.start for s in outcome.request.price_slots
                    if s.start.date() == day.date() and s.price == 0.08)
    held = {s.start for s in holds}
    booked = sorted(slot.start for slot, _kwh in outcome.booked if slot.start.date() == day.date())
    assert booked and all(b - a == Q for a, b in zip(booked, booked[1:]))  # one charge
    assert all(t in held or t in booked for t in valley)


def test_a_day_with_sun_to_spare_holds_nothing(monkeypatch):
    """The sun refills the battery anyway: the plan publishes no hold (and
    publishes them: an empty set, not the absent decision of before #888)."""
    now = datetime(2026, 11, 10, 0, 5)

    def price(t):
        return 0.20 if t.hour < 9 else (0.25 if t.hour < 18 else 0.30)

    bins = [0.1] * 96
    fleet = [dict(capacity_kwh=10.0, max_charge_w=2500, max_discharge_w=2500,
                  soc=50.0, min_soc=10, max_soc=100)]
    day = datetime(2026, 11, 10)
    periods = _sun(day, 30.0) + _sun(day + timedelta(days=1), 30.0)
    outcome, holds = _run(_scenario("sunny", now=now, prices=price, bins=bins, fleet=fleet,
                                    efficiency=0.85, margin=None, periods=periods, remaining=30.0),
                          monkeypatch)
    assert holds == frozenset()
    plain = optimize_charge(dataclasses.replace(outcome.request, hold_allowed=False)).plan
    assert [(a.slot.start, a.planned_battery_kwh) for a in outcome.plan.allocations] == [
        (a.slot.start, a.planned_battery_kwh) for a in plain.allocations]
    assert outcome.plan.optimizer_cost == pytest.approx(plain.optimizer_cost, abs=1e-9)
