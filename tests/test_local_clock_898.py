"""#898: "now" is Home Assistant local time, not the process (OS) clock.

Home Assistant never sets the process time zone, so a Docker/Core install
without TZ runs the OS clock on UTC while price slots are naive HA local time.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.control import weekly_full_charge as weekly_module
from custom_components.omnibattery.control.weekly_full_charge import WeeklyFullChargeManager
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing import engine as engine_module
from custom_components.omnibattery.pricing.calculations import parse_pvpc_prices
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.tracking import consumption_tracker as tracker_module
from custom_components.omnibattery.tracking.consumption_tracker import ConsumptionTracker


@pytest.fixture
def brussels_with_utc_os_clock(monkeypatch):
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("Europe/Brussels"))
    # 15:10 in Brussels (UTC+1 in November) is 14:10 on the UTC OS clock.
    instant = datetime(2026, 11, 18, 14, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: instant)
    monkeypatch.setattr(
        dt_util, "now", lambda time_zone=None: instant.astimezone(time_zone or dt_util.DEFAULT_TIME_ZONE)
    )

    class _OsClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.replace(tzinfo=None) if tz is None else instant.astimezone(tz)

    monkeypatch.setattr(engine_module, "datetime", _OsClock)
    yield
    dt_util.set_default_time_zone(previous)


def test_current_slot_follows_the_home_assistant_zone(brussels_with_utc_os_clock):
    slot_14 = PriceSlot(datetime(2026, 11, 18, 14, 0), datetime(2026, 11, 18, 14, 15), 0.10)
    slot_15 = PriceSlot(datetime(2026, 11, 18, 15, 0), datetime(2026, 11, 18, 15, 15), 0.20)

    def manager_with(slot):
        ctrl = SimpleNamespace(
            _dynamic_pricing_schedule=SimpleNamespace(selected_slots=[slot])
        )
        return PricingManager(SimpleNamespace(), ctrl)

    manager = manager_with(slot_15)
    assert manager._now() == datetime(2026, 11, 18, 15, 10)
    assert manager.is_in_dynamic_pricing_slot() is True
    assert manager_with(slot_14).is_in_dynamic_pricing_slot() is False


# ----------------------------------------------------------------------
# "Today" is Home Assistant's date too
# ----------------------------------------------------------------------

HA_TODAY = date(2026, 11, 19)
OS_TODAY = date(2026, 11, 18)


class _OsDate(date):
    """The date a UTC OS clock still shows in the first hour of the Brussels day."""

    @classmethod
    def today(cls):
        return OS_TODAY


@pytest.fixture
def brussels_just_after_midnight(monkeypatch):
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("Europe/Brussels"))
    # 00:30 on the 19th in Brussels is 23:30 on the 18th on the UTC OS clock.
    instant = datetime(2026, 11, 18, 23, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: instant)
    monkeypatch.setattr(
        dt_util, "now", lambda time_zone=None: instant.astimezone(time_zone or dt_util.DEFAULT_TIME_ZONE)
    )
    monkeypatch.setattr(tracker_module, "date", _OsDate)
    monkeypatch.setattr(weekly_module, "date", _OsDate)
    yield
    dt_util.set_default_time_zone(previous)


def test_pvpc_hours_are_dated_in_the_home_assistant_zone(brussels_just_after_midnight):
    slots = parse_pvpc_prices({"price_00h": 0.10, "price_23h": 0.12, "price_next_day_00h": 0.20})

    assert [slot.start for slot in slots] == [
        datetime(2026, 11, 19, 0, 0),
        datetime(2026, 11, 19, 23, 0),
        datetime(2026, 11, 20, 0, 0),
    ]
    # Half past midnight, the day's hours are still ahead.
    now = datetime(2026, 11, 19, 0, 30)
    assert len(PricingManager._filter_future_slots(slots, None, now=now)) == 2


def _tracker(**controller):
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._controller = SimpleNamespace(**controller)
    return tracker


def test_daily_totals_reset_at_home_assistant_midnight(brussels_just_after_midnight):
    tracker = _tracker(
        _daily_solar_energy_date=OS_TODAY,
        _daily_solar_energy_kwh=7.5,
        _daily_home_energy_date=OS_TODAY,
        _daily_home_energy_kwh=12.0,
        _daily_grid_energy_date=OS_TODAY,
        _daily_grid_import_energy_kwh=3.0,
        _daily_grid_export_energy_kwh=1.0,
        _daily_solar_forecast_initial_date=OS_TODAY,
        _daily_solar_forecast_initial_kwh=9.0,
    )

    tracker.handle_daily_energy_reset()

    ctrl = tracker._controller
    assert ctrl._daily_solar_energy_date == HA_TODAY
    assert ctrl._daily_home_energy_date == HA_TODAY
    assert ctrl._daily_grid_energy_date == HA_TODAY
    assert ctrl._daily_solar_energy_kwh == 0.0
    assert ctrl._daily_home_energy_kwh == 0.0
    assert ctrl._daily_grid_import_energy_kwh == 0.0
    assert ctrl._daily_solar_forecast_initial_kwh is None


def test_household_accumulator_resets_at_home_assistant_midnight(brussels_just_after_midnight):
    tracker = _tracker(_household_accumulator_date=OS_TODAY, _household_energy_accumulator=11.0)

    tracker.handle_accumulator_daily_reset()

    assert tracker._controller._household_accumulator_date == HA_TODAY
    assert tracker._controller._household_energy_accumulator == 0.0


def test_history_days_count_back_from_the_home_assistant_date(brussels_just_after_midnight):
    assert _tracker()._recent_history_days(2) == [date(2026, 11, 18), date(2026, 11, 17)]


@pytest.mark.asyncio
async def test_weekly_state_is_saved_under_the_home_assistant_date(brussels_just_after_midnight):
    manager = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    manager._controller = SimpleNamespace(
        weekly_full_charge_enabled=True,
        weekly_full_charge_complete=True,
        weekly_full_charge_registers_written=True,
        _weekly_charge_status={"state": "Complete"},
        _weekly_charge_needs_restore=False,
        _weekly_charge_saved_max_soc={},
        _charge_delay_unlocked=False,
        _solar_t_start=None,
    )
    manager._store = SimpleNamespace(async_save=AsyncMock())

    await manager.save_state()

    assert manager._store.async_save.await_args.args[0]["date"] == HA_TODAY.isoformat()


@pytest.mark.asyncio
async def test_weekly_state_of_the_home_assistant_day_is_restored(brussels_just_after_midnight):
    # Thursday the 19th is the weekly day; the run was saved a moment ago.
    controller = SimpleNamespace(
        weekly_full_charge_enabled=True,
        weekly_full_charge_day="thu",
        weekly_full_charge_complete=False,
        weekly_full_charge_registers_written=False,
        _weekly_charge_status={"state": "Idle"},
        _weekly_charge_needs_restore=False,
        _weekly_charge_saved_max_soc={},
        _charge_delay_unlocked=False,
        _solar_t_start=None,
    )
    manager = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    manager._controller = controller
    manager._store = SimpleNamespace(
        async_load=AsyncMock(
            return_value={"date": HA_TODAY.isoformat(), "complete": True, "registers_written": True}
        )
    )

    await manager.load_state()

    assert controller.weekly_full_charge_complete is True
