"""Monthly cadence, forecast selection, persistence and early-price triggers."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.control.monthly_full_charge import (
    MonthlyFullChargeSchedule, parse_forecast, score_days,
)
from custom_components.omnibattery.control.weekly_full_charge import WeeklyFullChargeManager

TZ = ZoneInfo("Europe/Amsterdam")


def now(day=24, month=11, year=2026, hour=0):
    return datetime(year, month, day, hour, tzinfo=TZ)


def schedule(**settings):
    battery = SimpleNamespace(name="Battery", device_key="stable", is_available=True, data={"battery_soc": 50})
    ctrl = SimpleNamespace(
        config_entry=SimpleNamespace(data={"full_charge_schedule": "monthly", **settings}),
        coordinators=[battery], weekly_full_charge_day="sun",
        weekly_full_charge_enabled=True, weekly_full_charge_complete=False,
        weekly_full_charge_registers_written=False, _weekly_charge_needs_restore=False,
        _weekly_charge_status={}, _force_full_charge=False,
        predictive_charging_enabled=True, predictive_charging_mode="dynamic_pricing",
        _pricing_mgr=SimpleNamespace(_get_current_price=lambda: 0.10),
        _charge_delay_unlocked=False, _solar_t_start=None,
        _weekly_charge_saved_max_soc={},
    )
    manager = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    manager._controller = ctrl
    manager._hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: None))
    manager._create_background_task = lambda coro, _: coro.close()
    manager.is_battery_full = lambda b: b.data is not None and b.data["battery_soc"] == 100
    manager._store = SimpleNamespace(async_save=AsyncMock(), async_load=AsyncMock())
    return manager.monthly


@pytest.mark.parametrize("year,month,last_sunday", [(2026, 11, 29), (2026, 2, 22), (2028, 2, 27), (2026, 12, 27)])
def test_simple_monthly_cadence(year, month, last_sunday):
    s = schedule()
    assert not s.is_active(now(1, month, year))
    assert not s.is_active(now(last_sunday - 1, month, year))
    assert s.is_active(now(last_sunday, month, year))
    assert s.reason == "monthly_cadence"
    assert s.controller._dp_config_dirty
    s.refresh_if_due(now(last_sunday, month, year))
    assert s._refresh_task is None


def test_simple_missed_day_catches_up():
    assert schedule().is_active(now(30))


def test_smart_five_days_and_month_boundary():
    s = schedule(monthly_full_charge_source="jeroen_nl")
    s.forecast_received_at = now()
    s.prices = {now().date() + timedelta(days=i): value for i, value in enumerate([.3, .2, .1, .4, .5, -.1])}
    assert s.target_day(now()) == now(26).date()
    assert not s.is_active(now())
    s.forecast_received_at = now(29)
    s.prices = {now(29).date(): .3, now(30).date(): .2, now(1, 12).date(): -.2}
    assert s.target_day(now(29)) == now(30).date()


def test_stale_forecast_waits_then_deadline_falls_back():
    s = schedule(monthly_full_charge_source="jeroen_nl")
    s.forecast_received_at = now(20)
    s.prices = {now().date(): -.5}
    assert not s.is_active(now())
    assert s.is_active(now(30))
    assert s.reason == "month_end_fallback"


@pytest.mark.parametrize("price,enabled,expected", [(0.09, True, True), (.10, True, True), (.11, True, False), (float("nan"), True, False), (None, True, False), (-.1, False, False)])
def test_optional_actual_price_trigger(price, enabled, expected):
    s = schedule(monthly_full_charge_price_enabled=enabled, monthly_full_charge_price=.10)
    s.controller._pricing_mgr._get_current_price = lambda: price
    assert s.is_active(now(3)) == expected


def test_natural_full_counts_and_survives_restart():
    s = schedule()
    s.controller.coordinators[0].data["battery_soc"] = 100
    s.observe_full(now(4))
    restored = schedule()
    restored.load(s.dump())
    assert restored.complete(now(25))
    assert not restored.is_active(now(29))
    assert restored.is_active(now(27, 12))


def test_disconnected_cached_full_does_not_satisfy_new_month():
    s = schedule()
    battery = s.controller.coordinators[0]
    battery.data["battery_soc"] = 100
    s.observe_full(now(30))
    battery.is_available = False
    s.observe_full(now(1, 12))
    assert not s.complete(now(1, 12))
    assert s.last_full == {"stable": "2026-11-30"}
    battery.is_available = True
    battery.data["battery_soc"] = 50
    s.observe_full(now(2, 12))
    assert not s.complete(now(2, 12))
    battery.data["battery_soc"] = 100
    s.observe_full(now(3, 12))
    assert s.complete(now(3, 12))


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_fails", [False, True])
async def test_monthly_history_completes_run_and_restores_limits(restore_fails):
    s = schedule()
    ctrl = s.controller
    batteries = []
    for name, soc in [("earlier", 50), ("later", 100)]:
        battery = MagicMock()
        battery.name = battery.device_key = name
        battery.data = {"battery_soc": soc}
        battery.is_available = True
        battery.battery_manual_mode_enabled = False
        battery.enable_charge_hysteresis = True
        battery.max_soc = 90
        battery.capabilities.hardware_soc_cutoff = True
        battery.set_charge_cutoff = AsyncMock(side_effect=[False, True] if restore_fails else [True])
        batteries.append(battery)
    ctrl.coordinators = batteries
    ctrl._is_backup_function_active = lambda _: False
    ctrl.charge_delay_enabled = True  # Completion must not wait for the next charging window.
    ctrl._balance_monitor_overrides_delay = lambda: False
    ctrl._weekly_charge_saved_max_soc = {b.name: 90 for b in batteries}
    ctrl.weekly_full_charge_registers_written = True
    s.last_full = {"earlier": "2026-11-04"}
    s.run_date = now(29).date()
    s.manager._bms_cutoff_counts = {}
    s.manager.tick_bms_cutoff = MagicMock()
    with patch("custom_components.omnibattery.control.weekly_full_charge.dt_util.now", return_value=now(29)):
        await s.manager.handle_registers()
        assert ctrl.weekly_full_charge_complete
        assert not s.is_active(now(29))
        assert ctrl._weekly_charge_status["completion_reason"] == "monthly_history_complete"
        assert ctrl._weekly_charge_needs_restore == restore_fails
        for battery in batteries:
            battery.set_charge_cutoff.assert_awaited_once_with(90)
            assert battery._hysteresis_active
        if restore_fails:
            await s.manager.handle_registers()
            assert not ctrl._weekly_charge_needs_restore
            assert all(b.set_charge_cutoff.await_count == 2 for b in batteries)
    assert not ctrl._weekly_charge_saved_max_soc


def test_started_cycle_does_not_follow_price_changes():
    s = schedule(monthly_full_charge_price_enabled=True, monthly_full_charge_price=.10)
    assert s.is_active(now(3))
    s.controller._pricing_mgr._get_current_price = lambda: .5
    assert s.is_active(now(4))
    s.controller.weekly_full_charge_registers_written = True
    assert not s.is_active(now(1, 12))
    assert s.controller._weekly_charge_needs_restore


def test_unavailable_battery_does_not_count_as_completed():
    s = schedule()
    s.last_full = {"stable": "2026-11-04"}
    s.controller.coordinators.append(SimpleNamespace(name="Missing", data=None))
    assert not s.complete(now())


def slots_for_day(day, values):
    return {day + timedelta(hours=i): (day + timedelta(hours=i+1), value) for i, value in enumerate(values)}


def test_cost_uses_charge_window_instead_of_daily_average():
    start = now()
    slots = slots_for_day(start, [-.1, -.1] + [.5] * 22)
    slots.update(slots_for_day(start + timedelta(days=1), [.1] * 24))
    result = score_days(slots, start, 2)
    assert result[start.date()] == pytest.approx(-.1)
    assert min(result, key=result.get) == start.date()
    # Already elapsed cheap hours cannot win.
    result = score_days(slots, start + timedelta(hours=3), 2)
    assert min(result, key=result.get) == (start + timedelta(days=1)).date()


def test_partial_and_overlapping_forecasts_are_rejected():
    assert score_days(slots_for_day(now(), [.1]*4), now(), 2) == {}
    slots = slots_for_day(now(), [.1]*24)
    slots[now()+timedelta(minutes=30)] = (now()+timedelta(hours=1), -.1)
    assert score_days(slots, now(), 2) == {}


@pytest.mark.parametrize("native_datetime", [False, True])
@pytest.mark.parametrize("attribute", ["raw_forecast", "raw_today", "raw_tomorrow"])
def test_sensor_adapter_and_unavailable_state(native_datetime, attribute):
    s = schedule(monthly_full_charge_source="sensor", monthly_full_charge_forecast_sensor="sensor.forecast")
    state = SimpleNamespace(state="0.1", last_updated=now(), attributes={
        "unit_of_measurement": "EUR/kWh", attribute: [
            {"start": start if native_datetime else start.isoformat(),
             "end": end if native_datetime else end.isoformat(), "value": value}
            for start, (end, value) in slots_for_day(now(), [.1]*24).items()
        ]})
    s.manager._hass.states.get = lambda _: state
    assert s.target_day(now()) == now().date()
    s.manager._hass.states.get = lambda _: None
    assert s.target_day(now()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute", ["raw_forecast", "raw_today", "raw_tomorrow"])
async def test_forecast_sensor_discovery_and_selection(attribute):
    from custom_components.omnibattery.select import FullChargeForecastSensorSelect

    entry = SimpleNamespace(data={}, entry_id="test")
    hass = SimpleNamespace(
        data={"omnibattery": {"test": {}}},
        states=SimpleNamespace(async_all=lambda _: [
            SimpleNamespace(entity_id="sensor.forecast", attributes={attribute: []}),
            SimpleNamespace(entity_id="sensor.unrelated", attributes={}),
        ]),
        config_entries=SimpleNamespace(
            async_update_entry=lambda e, data: setattr(e, "data", data)),
    )
    entity = FullChargeForecastSensorSelect(hass, entry)
    entity.async_write_ha_state = MagicMock()
    assert entity.options == ["none", "sensor.forecast"]
    await entity.async_select_option("sensor.forecast")
    assert entity.current_option == "sensor.forecast"


def test_jeroen_parser_uses_publication_time_and_actual_prices():
    payload = {"unit": "EUR/kWh", "timezone": "Europe/Amsterdam", "forecast_made_at_utc": now().isoformat(),
               "columns": ["time_utc", "known_market_price", "forecast_p50"],
               "rows": [[(now()+timedelta(minutes=i*15)).isoformat(), 0, .5] for i in range(96)]}
    reply = {"result": {"structuredContent": payload}}
    assert parse_forecast(reply, now()) == {now().date(): 0}
    with pytest.raises(ValueError, match="stale"):
        parse_forecast(reply, now(27))


@pytest.mark.asyncio
async def test_storage_resumes_monthly_run_on_different_day():
    s = schedule()
    assert s.is_active(now(29))
    s.controller.weekly_full_charge_registers_written = True
    await s.manager.save_state()
    saved = s.manager._store.async_save.call_args.args[0]
    restored = schedule()
    restored.manager._store.async_load.return_value = saved
    with patch("custom_components.omnibattery.control.weekly_full_charge.dt_util.now", return_value=now(30)):
        await restored.manager.load_state()
    assert restored.controller.weekly_full_charge_registers_written
    assert restored.is_active(now(30))
