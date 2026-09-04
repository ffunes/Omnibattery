"""Tests for weekly full charge grid-import orchestration."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import (
    WEEKLY_GRID_MODE_IMMEDIATE,
    WEEKLY_GRID_MODE_SOLAR_FIRST,
)
from custom_components.omnibattery.control.weekly_grid_charge import WeeklyGridChargeManager


def _battery(name: str, soc: int, *, manual: bool = False):
    class _Coord:
        __slots__ = ("name", "data", "battery_manual_mode_enabled", "max_soc")

        def __init__(self):
            self.name = name
            self.data = {"battery_soc": soc, "battery_power": 0}
            self.battery_manual_mode_enabled = manual
            self.max_soc = 100

    return _Coord()


def _weekly_mgr(is_active: bool = True, is_full=None):
    mgr = MagicMock()
    mgr.is_active.return_value = is_active
    mgr.is_battery_full.side_effect = (
        is_full if callable(is_full) else (lambda _c: bool(is_full))
    )
    return mgr


def _controller(**overrides):
    base = {
        "weekly_full_charge_enabled": True,
        "weekly_full_charge_grid_enabled": True,
        "weekly_full_charge_grid_mode": WEEKLY_GRID_MODE_IMMEDIATE,
        "weekly_full_charge_complete": False,
        "coordinators": [_battery("b1", 80)],
        "_weekly_charge_mgr": _weekly_mgr(),
        "_charge_delay_mgr": SimpleNamespace(_should_delay_charge=lambda _t: False),
        "_weekly_charge_status": {},
        "grid_charging_active": False,
        "_grid_charge_owner": None,
        "_predictive_charge_target_soc": None,
        "_grid_charging_initialized": False,
        "_is_backup_function_active": lambda _c: False,
        "_is_operation_allowed": lambda is_charging=True: True,
        "_handle_predictive_grid_charging": AsyncMock(),
        "_stop_grid_charge_session": MagicMock(),
    }
    base.update(overrides)
    ctrl = SimpleNamespace(**base)
    ctrl._weekly_grid_charge_mgr = WeeklyGridChargeManager(ctrl)
    return ctrl


@pytest.mark.asyncio
async def test_grid_disabled_does_not_start_session():
    ctrl = _controller(weekly_full_charge_grid_enabled=False)
    owned = await ctrl._weekly_grid_charge_mgr.handle()
    assert owned is False
    assert ctrl.grid_charging_active is False


@pytest.mark.asyncio
async def test_immediate_mode_starts_weekly_grid_session():
    ctrl = _controller()
    owned = await ctrl._weekly_grid_charge_mgr.handle()
    assert owned is True
    assert ctrl._grid_charge_owner == "weekly"
    assert ctrl.grid_charging_active is True
    assert ctrl._predictive_charge_target_soc[ctrl.coordinators[0]] == 100.0
    assert ctrl._weekly_charge_status["state"] == "Grid charging"
    ctrl._handle_predictive_grid_charging.assert_awaited_once()


@pytest.mark.asyncio
async def test_solar_first_waits_while_forecast_says_sun_is_enough():
    ctrl = _controller(
        weekly_full_charge_grid_mode=WEEKLY_GRID_MODE_SOLAR_FIRST,
        _charge_delay_mgr=SimpleNamespace(_should_delay_charge=lambda _t: True),
    )
    owned = await ctrl._weekly_grid_charge_mgr.handle()
    assert owned is False
    assert ctrl.grid_charging_active is False


@pytest.mark.asyncio
async def test_solar_first_starts_when_forecast_insufficient():
    ctrl = _controller(
        weekly_full_charge_grid_mode=WEEKLY_GRID_MODE_SOLAR_FIRST,
        _charge_delay_mgr=SimpleNamespace(_should_delay_charge=lambda _t: False),
    )
    owned = await ctrl._weekly_grid_charge_mgr.handle()
    assert owned is True
    assert ctrl._grid_charge_owner == "weekly"


def test_weekly_grid_owner_overrides_predictive_ceiling():
    battery = _battery("b1", 70)
    ctrl = SimpleNamespace(
        _grid_charge_owner="weekly",
        grid_charging_active=True,
        _predictive_charge_target_soc={battery: 30.0},
        max_soc=90,
    )
    ceiling, source = ChargeDischargeController._effective_charge_max_soc(
        ctrl, battery, weekly_100_unlocked=True
    )
    assert (ceiling, source) == (100, "weekly_full_charge")


def test_predictive_ceiling_stays_authoritative_without_weekly_grid_owner():
    battery = _battery("b1", 20)
    battery.max_soc = 95
    ctrl = SimpleNamespace(
        _grid_charge_owner=None,
        grid_charging_active=True,
        _predictive_charge_target_soc={battery: 30.0},
    )
    ceiling, source = ChargeDischargeController._effective_charge_max_soc(
        ctrl, battery, weekly_100_unlocked=True
    )
    assert (ceiling, source) == (30.0, "predictive_target")


def test_clear_session_resets_weekly_owner():
    ctrl = _controller(
        _grid_charge_owner="weekly",
        grid_charging_active=True,
        _weekly_charge_status={"state": "Grid charging"},
    )
    ctrl._weekly_grid_charge_mgr.clear_session()
    ctrl._stop_grid_charge_session.assert_called_once_with(owner="weekly")
    assert ctrl._weekly_charge_status["state"] == "Charging to 100%"


def test_weekly_grid_power_cap_unchanged_at_100_percent():
    ctrl = SimpleNamespace(
        _grid_charge_owner="weekly",
        weekly_full_charge_grid_power_pct=100.0,
    )
    assert ChargeDischargeController._weekly_grid_max_charge_power(ctrl, 3000.0) == 3000.0


def test_weekly_grid_power_cap_scales_when_owner_is_weekly():
    ctrl = SimpleNamespace(
        _grid_charge_owner="weekly",
        weekly_full_charge_grid_power_pct=50.0,
    )
    assert ChargeDischargeController._weekly_grid_max_charge_power(ctrl, 3000.0) == 1500.0


def test_weekly_grid_power_cap_ignored_without_weekly_owner():
    ctrl = SimpleNamespace(
        _grid_charge_owner=None,
        weekly_full_charge_grid_power_pct=50.0,
    )
    assert ChargeDischargeController._weekly_grid_max_charge_power(ctrl, 3000.0) == 3000.0


def test_weekly_grid_power_cap_respects_minimum_floor():
    ctrl = SimpleNamespace(
        _grid_charge_owner="weekly",
        weekly_full_charge_grid_power_pct=10.0,
    )
    assert ChargeDischargeController._weekly_grid_max_charge_power(
        ctrl, 3000.0, minimum_charge_power=500.0
    ) == 500.0
