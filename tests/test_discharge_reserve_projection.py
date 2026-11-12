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


SUNRISE = DAY + timedelta(hours=7)


def _night(held, limit_w=3000.0):
    now = DAY + timedelta(hours=1)
    end = DAY + timedelta(hours=12)
    extra = {
        "_is_capacity_protection_soc_limited": lambda: held,
        "capacity_protection_limit": limit_w,
    }
    manager, pricing = _manager(
        now, remaining_kwh=0.0, controller_extra=extra, window_start=SUNRISE
    )
    consumption, _solar, _fn = manager._consumption_and_solar(
        pricing, _slots(now, end), now, end
    )
    return consumption


def test_a_hold_leaves_the_load_below_the_limit_to_the_grid():
    consumption = _night(held=True)
    before = [v for s, v in consumption.items() if s.end <= SUNRISE]
    after = [v for s, v in consumption.items() if s.start >= SUNRISE]
    assert before and after
    assert sum(before) == pytest.approx(0.0)
    assert all(v == pytest.approx(0.5) for v in after)


def test_a_hold_only_trims_the_load_above_the_limit():
    consumption = _night(held=True, limit_w=300.0)
    before = [v for s, v in consumption.items() if s.end <= SUNRISE]
    assert all(v == pytest.approx(0.2) for v in before)  # 0.5 - 0.3 kW over 1 h


def test_without_a_hold_the_night_load_is_untouched():
    consumption = _night(held=False)
    assert all(v == pytest.approx(0.5) for v in consumption.values())


def test_a_slot_straddling_sunrise_is_prorated():
    global SUNRISE
    SUNRISE = DAY + timedelta(hours=7, minutes=30)
    try:
        consumption = _night(held=True)
    finally:
        SUNRISE = DAY + timedelta(hours=7)
    straddling = next(v for s, v in consumption.items() if s.start.hour == 7)
    assert straddling == pytest.approx(0.25)  # only the half after sunrise
