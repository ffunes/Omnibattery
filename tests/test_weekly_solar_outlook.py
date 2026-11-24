"""#876: the weekly full charge says whether today's sun alone can reach 100%."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
)

_MOD = "custom_components.omnibattery.control.weekly_full_charge"
_GATEWAY_PN = "custom_components.omnibattery.notifications.gateway.persistent_notification"
# 2026-11-15 is a Sunday, the configured weekly day.
_NOON = datetime(2026, 11, 15, 12, 0)
_BATTERY = SimpleNamespace(data={"battery_soc": 60, "battery_total_energy": 5.0})  # 2 kWh to 100%


def _setup(predictive=False, consumption_kwh=1.0):
    ctrl = SimpleNamespace(
        hass=object(),
        predictive_charging_enabled=predictive,
        weekly_full_charge_day="sun",
        _weekly_charge_status={"state": "Charging to 100%"},
        _solar_t_start=8.0,
        _charge_delay_mgr=SimpleNamespace(
            _excluded_claim_kwh=lambda _kwh: 0.0,
            _profile_forecast_between=lambda _a, _b: SimpleNamespace(energy_kwh=consumption_kwh),
        ),
        _consumption_tracker=SimpleNamespace(estimate_t_end=lambda: 17.0),
    )
    manager = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    manager._controller = ctrl
    return manager, ctrl


def _run(manager, solar_kwh, now=_NOON, source="remaining"):
    forecast = SimpleNamespace(source=source, remaining_kwh=solar_kwh)
    notify = MagicMock()
    with (
        patch(f"{_MOD}.dt_util.now", return_value=now),
        patch(f"{_MOD}.read_remaining_solar_kwh", return_value=forecast),
        patch(_GATEWAY_PN, notify),
    ):
        manager.update_solar_outlook([_BATTERY])
    return notify


def test_short_sun_without_predictive_flags_and_notifies_once():
    manager, ctrl = _setup()
    # 2.5 kWh sun - 1 kWh home = 1.5 kWh net < 2 kWh needed.
    notify = _run(manager, 2.5)
    assert ctrl._weekly_charge_status["solar_sufficient"] is False
    assert ctrl._weekly_charge_status["energy_needed_kwh"] == 2.0
    assert ctrl._weekly_charge_status["net_solar_kwh"] == 1.5
    notify.async_create.assert_called_once()
    # A moved forecast that is still short re-evaluates but does not notify again.
    assert _run(manager, 2.2).async_create.call_count == 0


def test_forecast_change_flips_the_verdict():
    manager, ctrl = _setup()
    _run(manager, 2.5)
    _run(manager, 2.7)  # within 0.25 kWh: not re-evaluated
    assert ctrl._weekly_charge_status["net_solar_kwh"] == 1.5
    _run(manager, 4.0)
    assert ctrl._weekly_charge_status["solar_sufficient"] is True


@pytest.mark.parametrize(
    ("solar_kwh", "now", "source"),
    [
        (0.0, datetime(2026, 11, 15, 0, 10), "remaining"),  # provider not published yet
        (0.0, _NOON, "fallback"),  # no forecast sensor / unavailable
    ],
)
def test_unknown_forecast_publishes_nothing(solar_kwh, now, source):
    manager, ctrl = _setup()
    notify = _run(manager, solar_kwh, now=now, source=source)
    assert "solar_sufficient" not in ctrl._weekly_charge_status
    notify.async_create.assert_not_called()


def test_predictive_on_flags_without_notifying():
    manager, ctrl = _setup(predictive=True)
    notify = _run(manager, 2.5)
    assert ctrl._weekly_charge_status["solar_sufficient"] is False
    notify.async_create.assert_not_called()


def test_clear_drops_verdict_and_dismisses_notification():
    manager, ctrl = _setup()
    _run(manager, 2.5)
    with patch(_GATEWAY_PN) as notify:
        manager._clear_solar_outlook()
    assert "solar_sufficient" not in ctrl._weekly_charge_status
    notify.async_dismiss.assert_called_once()


@pytest.mark.asyncio
async def test_evaluated_while_charge_delay_holds():
    """Without a charge attempt (sunless day, delay holding) the verdict still runs."""
    manager, ctrl = _setup()
    ctrl.weekly_full_charge_enabled = True
    ctrl._force_full_charge = False
    ctrl._weekly_charge_needs_restore = False
    ctrl.charge_delay_enabled = True
    ctrl._charge_delay_unlocked = False
    ctrl._balance_monitor_overrides_delay = lambda: False
    ctrl.coordinators = [SimpleNamespace(battery_manual_mode_enabled=False, **vars(_BATTERY))]
    manager.is_active = lambda: True
    manager.tick_bms_cutoff = lambda: None
    manager.update_solar_outlook = MagicMock()

    await manager.handle_registers()

    manager.update_solar_outlook.assert_called_once()


def test_forecast_that_recovers_dismisses_the_warning():
    manager, ctrl = _setup()
    _run(manager, 2.5)
    notify = _run(manager, 4.0)
    assert ctrl._weekly_charge_status["solar_sufficient"] is True
    notify.async_dismiss.assert_called_once()


@pytest.mark.asyncio
async def test_switching_the_feature_off_mid_run_drops_the_verdict():
    manager, ctrl = _setup()
    _run(manager, 2.5)
    ctrl.weekly_full_charge_enabled = False
    ctrl._force_full_charge = False
    ctrl._weekly_charge_needs_restore = False
    manager.tick_bms_cutoff = lambda: None

    with patch(_GATEWAY_PN) as notify:
        await manager.handle_registers()

    assert "solar_sufficient" not in ctrl._weekly_charge_status
    notify.async_dismiss.assert_called_once()


def test_sensor_publishes_the_verdict():
    from custom_components.omnibattery.sensor import WeeklyFullChargeSensor

    manager, ctrl = _setup()
    _run(manager, 2.5)
    ctrl.charge_delay_enabled = False
    sensor = WeeklyFullChargeSensor.__new__(WeeklyFullChargeSensor)
    sensor._controller = ctrl

    attrs = sensor.extra_state_attributes

    assert attrs["solar_sufficient"] is False
    assert attrs["energy_needed_kwh"] == 2.0
    assert attrs["net_solar_kwh"] == 1.5


def _real_tracker(ctrl, latitude=50.0):
    """The real tracker's solar timing, on a fixed day and place."""
    from custom_components.omnibattery.tracking.consumption_tracker import (
        ConsumptionTracker,
    )

    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._controller = ctrl
    tracker._hass = SimpleNamespace(
        config=SimpleNamespace(latitude=latitude, longitude=10.0, time_zone="Europe/Berlin")
    )
    tracker._solar_noon_cache = {}
    ctrl.coordinators = []
    ctrl._consumption_tracker = tracker
    return tracker


def _run_with_real_tracker(manager, solar_kwh, now):
    with patch(
        "custom_components.omnibattery.tracking.consumption_tracker.local_now",
        return_value=now,
    ):
        return _run(manager, solar_kwh, now=now)


def test_no_solar_start_yet_uses_the_sunrise():
    """Before the solar start is detected (all day with Charge Delay off) it must not raise."""
    manager, ctrl = _setup()
    ctrl._solar_t_start = None
    tracker = _real_tracker(ctrl)
    windows = []
    ctrl._charge_delay_mgr._profile_forecast_between = lambda start_h, end_h: (
        windows.append((start_h, end_h)) or SimpleNamespace(energy_kwh=1.0)
    )
    now = datetime(2026, 11, 15, 0, 10)
    sunset_h = 2 * tracker.calculate_solar_noon(now.date()) - tracker.calculate_sunrise(now.date())

    _run_with_real_tracker(manager, 2.5, now)

    assert ctrl._weekly_charge_status["solar_sufficient"] is False
    assert ctrl._weekly_charge_status["net_solar_kwh"] == 1.5
    assert windows == [(pytest.approx(10 / 60), pytest.approx(sunset_h))]
    assert 16.0 < sunset_h < 17.0  # mid-November at 50° N


def test_no_solar_start_and_no_sunrise_waits_without_caching():
    """Polar night / no location: no verdict, and the same forecast is read again later."""
    manager, ctrl = _setup()
    ctrl._solar_t_start = None
    _real_tracker(ctrl, latitude=None)

    notify = _run_with_real_tracker(manager, 2.5, _NOON)

    assert "solar_sufficient" not in ctrl._weekly_charge_status
    notify.async_create.assert_not_called()

    ctrl._solar_t_start = 8.0  # detected in the meantime
    _run_with_real_tracker(manager, 2.5, _NOON)
    assert ctrl._weekly_charge_status["solar_sufficient"] is False
