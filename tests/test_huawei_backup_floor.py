"""Huawei: the backup power SOC is a discharge floor.

The inverter holds the backup power SOC (register 47102) back for its backup
output and delivers nothing below it. Without treating it as a floor the control
layer keeps commanding a battery that sits between its own minimum and the
reserve, the inverter delivers nothing, and the battery is written off as
non-delivering.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control.pack_soc import (
    effective_min_soc,
    hardware_reserve_soc,
)
from custom_components.omnibattery.drivers.huawei import (
    SENSOR_DEFINITIONS,
    HuaweiSolarDriver,
)

from .test_huawei_driver import _LIVE_BLOCKS, _fake_client


def _coordinator(min_soc=20, **data):
    return SimpleNamespace(min_soc=min_soc, max_soc=100, data=data or None)


def _driver(blocks=None):
    return HuaweiSolarDriver(
        MagicMock(), "1.2.3.4", port=502, slave_id=4,
        battery_device_id="dev", client=_fake_client(blocks),
    )


# ---------------------------------------------------------------- register
async def _telemetry(blocks):
    return await _driver(blocks).read_telemetry()


@pytest.mark.asyncio
async def test_driver_publishes_the_backup_power_soc():
    data = await _telemetry({**_LIVE_BLOCKS, 47102: [350]})
    assert data["hardware_reserve_soc"] == 35.0


@pytest.mark.asyncio
async def test_driver_omits_the_key_when_the_register_is_unsupported():
    # A model without a backup box fails the read; the key is absent, not zero.
    data = await _telemetry(dict(_LIVE_BLOCKS))
    assert "hardware_reserve_soc" not in data


@pytest.mark.asyncio
async def test_driver_rejects_the_invalid_marker():
    data = await _telemetry({**_LIVE_BLOCKS, 47102: [0xFFFF]})
    assert "hardware_reserve_soc" not in data


def test_backup_power_soc_stays_a_visible_diagnostic_sensor():
    sensor = next(s for s in SENSOR_DEFINITIONS if s["key"] == "hardware_reserve_soc")
    assert sensor["unit"] == "%"
    assert sensor["category"] == "diagnostic"
    assert sensor["enabled_by_default"] is True


def test_the_reserve_is_read_even_with_its_sensor_disabled():
    driver = _driver()
    assert "hardware_reserve_soc" in driver.control_dependency_keys
    assert any("hardware_reserve_soc" in g.keys for g in driver.read_groups)


# ---------------------------------------------------------------- helpers
def test_no_reserve_leaves_min_soc_alone():
    assert effective_min_soc(_coordinator(20, battery_soc=33)) == 20
    assert effective_min_soc(SimpleNamespace(min_soc=20, data=None)) == 20


def test_reserve_above_min_soc_raises_the_floor():
    assert effective_min_soc(_coordinator(20, hardware_reserve_soc=35.0)) == 35.0


def test_reserve_below_min_soc_does_not_lower_the_floor():
    assert effective_min_soc(_coordinator(50, hardware_reserve_soc=35.0)) == 50


def test_reserve_is_not_added_to_min_soc():
    assert effective_min_soc(_coordinator(20, hardware_reserve_soc=35.0)) != 55


@pytest.mark.parametrize("bad", [None, "35", True, 0, -1, 101, float("nan")])
def test_implausible_reserve_is_ignored(bad):
    assert hardware_reserve_soc(_coordinator(20, hardware_reserve_soc=bad)) == 0.0


# ---------------------------------------------------------------- floor users
def _ctrl(slot=None):
    return SimpleNamespace(
        _get_active_slot=lambda c, kind: slot,
        _slot_battery_limits=lambda s, c: s["limits"],
        capacity_protection_enabled=False,
    )


def test_effective_discharge_floor_follows_the_reserve():
    c = _coordinator(20, hardware_reserve_soc=35.0)
    assert ChargeDischargeController._effective_discharge_min_soc(_ctrl(), c) == (
        35.0, "hardware_reserve",
    )


def test_effective_discharge_floor_keeps_the_user_minimum_when_higher():
    c = _coordinator(40, hardware_reserve_soc=35.0)
    assert ChargeDischargeController._effective_discharge_min_soc(_ctrl(), c) == (
        40, "min_soc",
    )


def test_a_lower_slot_override_cannot_go_below_the_reserve():
    slot = {"soc_override_enabled": True, "limits": {"soc_min": 15}}
    c = _coordinator(20, hardware_reserve_soc=35.0)
    assert ChargeDischargeController._effective_discharge_min_soc(_ctrl(slot), c) == (
        35.0, "hardware_reserve",
    )


def test_planning_floor_includes_the_reserve():
    c = _coordinator(20, hardware_reserve_soc=35.0)
    assert ChargeDischargeController._planning_floor_soc(_ctrl(), c) == 35.0


# ---------------------------------------------------------------- plan floor
def test_the_plan_sizes_its_charge_against_the_reserve():
    # ``floor_of`` in the chronological plan: with the reserve above the minimum
    # the battery is empty at 35 %, so the same SOC leaves a larger deficit and
    # runs dry earlier than when the minimum alone (12 %) is the floor.
    from datetime import datetime

    from .test_min_soc_floor import (
        _FLOOR_BASE_KW,
        _FLOOR_TZ,
        _floor_manager,
    )

    def target(reserve):
        manager = _floor_manager(45.0)
        coordinator = manager._controller.coordinators[0]
        if reserve is not None:
            coordinator.data["hardware_reserve_soc"] = reserve
        decision = manager._apply_time_slot_chronological_plan(
            {
                "should_charge": False,
                "avg_consumption_kwh": _FLOOR_BASE_KW * 24,
                "energy_deficit_kwh": 0.0,
                "planned_grid_charge_kwh": 0.0,
                "excluded_demand_claim_kwh": 0.0,
            },
            now=datetime(2026, 9, 11, 1, 0, tzinfo=_FLOOR_TZ),
        )
        return decision["energy_deficit_kwh"]

    assert target(35.0) > target(None)
    # A reserve below the minimum changes nothing.
    assert target(5.0) == target(None)
