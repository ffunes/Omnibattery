"""The reserve's per-slot projection: today's remaining sun and a peak-shaving hold."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.control.discharge_reserve import DischargeReserveManager
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.solar_forecast import SolarForecastPeriod

BERLIN = ZoneInfo("Europe/Berlin")
DAY = datetime(2026, 9, 4, tzinfo=BERLIN)
TOMORROW = DAY + timedelta(days=1)
LOAD_PER_QUARTER = 0.125  # a flat 0.5 kW house


def _fraction(hour: float) -> float:
    """Cumulative share of the day's sun, one full unit per elapsed day."""
    day, hod = divmod(hour, 24.0)
    return day + max(0.0, min(1.0, (hod - 8.0) / 8.0))


def _slots(start: datetime, end: datetime) -> list[PriceSlot]:
    out, cursor = [], start
    while cursor < end:
        out.append(PriceSlot(cursor, cursor + timedelta(hours=1), 0.3))
        cursor += timedelta(hours=1)
    return out


def _periods(day: datetime, first_hour: int, kwh: list[float]):
    return tuple(
        SolarForecastPeriod(
            day + timedelta(hours=first_hour + i),
            day + timedelta(hours=first_hour + i + 1),
            k,
        )
        for i, k in enumerate(kwh)
    )


def _manager(now, *, remaining_kwh, periods=(), controller_extra=None, window_start=None):
    flat = [LOAD_PER_QUARTER] * 96
    profile = SimpleNamespace(
        intervals_kwh=flat,
        intervals_by_date={DAY.date(): flat, TOMORROW.date(): flat},
        energy_kwh=sum(flat),
        source="profile",
    )
    pricing = SimpleNamespace(
        _profile_remaining_consumption=lambda s, e: profile,
        _curtailment_forecast_model=lambda _now: (remaining_kwh, _fraction, None),
        _solar_forecast_is_remaining=lambda: True,
        _solar_timeline_window=lambda _now, _tracker: (window_start, None),
    )
    controller = SimpleNamespace(
        solar_forecast_periods=periods,
        _pricing_mgr=pricing,
        max_charge_capacity=10_000.0,
        **(controller_extra or {}),
    )
    manager = DischargeReserveManager(
        SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Berlin")), controller
    )
    manager._now = lambda: now
    return manager, pricing


def test_remaining_sun_stays_on_today_s_slots():
    now = DAY + timedelta(hours=14)
    end = TOMORROW + timedelta(hours=12)
    periods = _periods(DAY, 14, [0.3, 0.2, 0.1]) + _periods(
        TOMORROW, 8, [0.5, 1.0, 1.0, 0.5]
    )
    manager, pricing = _manager(now, remaining_kwh=0.6, periods=periods)

    _consumption, solar, _fn = manager._consumption_and_solar(
        pricing, _slots(now, end), now, end
    )

    today = sum(v for slot, v in solar.items() if slot.start.date() == DAY.date())
    assert today == pytest.approx(0.6, abs=0.01)
