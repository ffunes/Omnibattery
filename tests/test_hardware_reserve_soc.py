"""A hardware reserve (Huawei backup power SOC) is a discharge floor.

The inverter holds the backup power SOC back for the backup output and will not
discharge into the house below it. Without treating it as a floor the control
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


# ---------------------------------------------------------------- driver
async def _telemetry(blocks):
    driver = HuaweiSolarDriver(
        MagicMock(), "1.2.3.4", port=502, slave_id=4,
        battery_device_id="dev", client=_fake_client(blocks),
    )
    return await driver.read_telemetry()


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


def test_backup_power_soc_has_a_sensor_definition():
    sensor = next(s for s in SENSOR_DEFINITIONS if s["key"] == "hardware_reserve_soc")
    assert sensor["unit"] == "%"


# ---------------------------------------------------------------- helpers
def test_no_reserve_leaves_min_soc_alone():
    assert effective_min_soc(_coordinator(20, battery_soc=33)) == 20
    assert effective_min_soc(SimpleNamespace(min_soc=20, data=None)) == 20


def test_reserve_above_min_soc_raises_the_floor():
    assert effective_min_soc(_coordinator(20, hardware_reserve_soc=35.0)) == 35.0


def test_reserve_below_min_soc_does_not_lower_the_floor():
    assert effective_min_soc(_coordinator(50, hardware_reserve_soc=35.0)) == 50


def test_reserve_is_not_added_to_min_soc():
    # The inverter reserves the backup SOC itself; the discharge cutoff does not
    # stack on top of it.
    assert effective_min_soc(_coordinator(20, hardware_reserve_soc=35.0)) != 55


@pytest.mark.parametrize("bad", [None, "35", True, 0, -1, 101, float("nan")])
def test_implausible_reserve_is_ignored(bad):
    assert hardware_reserve_soc(_coordinator(20, hardware_reserve_soc=bad)) == 0.0


# ---------------------------------------------------------------- control
def _ctrl(slot=None):
    return SimpleNamespace(
        _get_active_slot=lambda c, kind: slot,
        _slot_battery_limits=lambda s, c: s["limits"],
        capacity_protection_enabled=False,
    )


def test_effective_discharge_floor_follows_the_reserve():
    c = _coordinator(20, hardware_reserve_soc=35.0)
    floor, source = ChargeDischargeController._effective_discharge_min_soc(_ctrl(), c)
    assert (floor, source) == (35.0, "hardware_reserve")


def test_effective_discharge_floor_keeps_the_user_minimum_when_higher():
    c = _coordinator(40, hardware_reserve_soc=35.0)
    assert ChargeDischargeController._effective_discharge_min_soc(_ctrl(), c) == (
        40, "min_soc",
    )


def test_a_lower_slot_override_cannot_go_below_the_reserve():
    slot = {"soc_override_enabled": True, "limits": {"soc_min": 15}}
    c = _coordinator(20, hardware_reserve_soc=35.0)
    floor, source = ChargeDischargeController._effective_discharge_min_soc(_ctrl(slot), c)
    assert (floor, source) == (35.0, "hardware_reserve")


def test_planning_floor_includes_the_reserve():
    c = _coordinator(20, hardware_reserve_soc=35.0)
    assert ChargeDischargeController._planning_floor_soc(_ctrl(), c) == 35.0
