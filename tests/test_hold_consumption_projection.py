"""Surplus Hold sizes its daytime house load from the shared energy projection
(the learned profile), not from a flat spread (S6). Scenario helpers are shared
with test_curtailment_consumption_projection.py."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.control.surplus_price_hold import (
    SurplusPriceHoldManager,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.curtailment import distribute_solar_forecast
from custom_components.omnibattery.solar_forecast import SolarForecastPeriod
from tests import test_predictive_scenarios as T
from tests.test_energy_projection import _build

DAY = datetime(2026, 7, 14)
BELL = {6: .4, 7: 1.0, 8: 1.7, 9: 2.4, 10: 3.0, 11: 3.3, 12: 3.5, 13: 3.5, 14: 3.2,
        15: 2.7, 16: 2.0, 17: 1.3, 18: .8, 19: .4, 20: .15, 21: .05}


def _profile(night_kw: float) -> tuple[float, ...]:
    """`night_kw` until 06:00, 0.45 kW by day, a 3 kW evening peak 18:30-20:30."""
    return T._consumption_bins({"profile_kw": [
        {"from": "00:00", "kw": night_kw}, {"from": "06:00", "kw": 0.45},
        {"from": "18:30", "kw": 3.0}, {"from": "20:30", "kw": 0.45},
    ]})


def _scenario(now: datetime, night_kw: float, *, soc: float, remaining: float,
              negative_hours: range = range(0)):
    base = T.load_scenario(T.SCENARIO_DIR / "834-horizon-extends-with-solar-periods.json")
    zone = ZoneInfo(base.time_zone)
    scale = 25.0 / sum(BELL.values())
    periods = tuple(
        SolarForecastPeriod(
            (DAY + timedelta(days=d, hours=h)).replace(tzinfo=zone),
            (DAY + timedelta(days=d, hours=h + 1)
             if h < 21 else DAY + timedelta(days=d, hours=21, minutes=30)).replace(tzinfo=zone),
            round(v * scale, 4),
        )
        for d in (0, 1) for h, v in BELL.items()
    )
    slots = tuple(
        PriceSlot(DAY + timedelta(days=d, hours=h), DAY + timedelta(days=d, hours=h + 1),
                  -0.01 if (d == 0 and h in negative_hours) else 0.25)
        for d in (0, 1) for h in range(24)
    )
    return dataclasses.replace(
        base, now=now, sunrise_h=6.0, solar_noon_h=13.75, slots=slots,
        bins_kwh=_profile(night_kw), solar_periods=periods, solar_remaining_kwh=remaining,
        fleet=({**base.fleet[0], "soc": soc},),
        settings={**base.settings, "discharge_reserve": False, "safety_margin_kwh": 1.0},
    )


def _profile_kwh(scenario, start: datetime, end: datetime) -> float:
    """Energy of the scenario's profile between two moments of DAY."""
    total = 0.0
    for i, kwh in enumerate(scenario.bins_kwh):
        b_start = DAY + timedelta(minutes=15 * i)
        overlap = (min(end, b_start + timedelta(minutes=15)) - max(start, b_start)).total_seconds()
        if overlap > 0:
            total += kwh * overlap / 900.0
    return total


def test_hold_daylight_load_follows_the_learned_profile(monkeypatch):
    """09:00, 0.45 kW daytime / 3 kW evening: the Hold's per-slot surplus is the
    sun minus the PROFILE's load, not minus a flat ~1 kWh/h share."""
    now = DAY + timedelta(hours=9)
    scenario = _scenario(now, 0.30, soc=40, remaining=22.3639)
    ctrl, mgr = _build(scenario, monkeypatch)
    deadline = DAY + timedelta(hours=21, minutes=30)
    slots = [PriceSlot(DAY + timedelta(hours=h), DAY + timedelta(hours=h + 1), 0.25)
             for h in range(9, 21)]
    slots.append(PriceSlot(DAY + timedelta(hours=21), deadline, 0.25))
    remaining_load = mgr._controller._consumption_tracker.forecast_consumption_between(
        now, DAY + timedelta(days=1, hours=6)
    ).energy_kwh
    decision = {"remaining_solar_kwh": 22.3639, "remaining_consumption_kwh": remaining_load}

    surplus = SurplusPriceHoldManager(ctrl.hass, ctrl)._surplus_by_slot(
        mgr, slots, decision, now, deadline
    )

    _forecast, fraction_fn, _ = mgr._curtailment_forecast_model(now)
    solar = distribute_solar_forecast(slots, 22.3639, fraction_fn, normalize_future=True)
    expected = {s: max(0.0, solar[s] - _profile_kwh(scenario, s.start, s.end)) for s in slots}
    for slot in slots:
        assert surplus[slot] == pytest.approx(expected[slot], abs=0.01), slot.start
    # The 3 kW evening peak (19:00-20:00) wipes the surplus out.
    assert surplus[slots[10]] == pytest.approx(0.0, abs=0.01)
    assert sum(surplus.values()) == pytest.approx(sum(expected.values()), abs=0.05)
