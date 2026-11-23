"""Anker: the app's backup reserve is a discharge floor (#939).

With backup_soc_enable (60003) set, the firmware stops discharging at
backup_reserve_soc (60002), not at the discharge limit (60001). The driver
publishes it as hardware_reserve_soc, which the control layer already treats
as a floor (see test_huawei_backup_floor).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery.control.pack_soc import effective_min_soc
from custom_components.omnibattery.drivers.anker import SENSOR_DEFINITIONS

from .test_anker_driver import _driver, _fake_client


async def _telemetry(holding_60000):
    client = _fake_client()
    client.async_read_holding_block = AsyncMock(
        side_effect=lambda start, count: holding_60000 if start == 60000 else None
    )
    drv = _driver(client=client)
    return drv, await drv.read_telemetry()


@pytest.mark.asyncio
async def test_enabled_reserve_is_published_and_raises_the_floor():
    _, data = await _telemetry([100, 20, 50, 1])
    assert data["hardware_reserve_soc"] == 50
    coordinator = SimpleNamespace(min_soc=20, data=data)
    assert effective_min_soc(coordinator) == 50


@pytest.mark.asyncio
async def test_disabled_reserve_publishes_zero():
    # 0, not absent: the coordinator merges snapshots, so a missing key would
    # keep the old 50 % floor after the user switches the reserve off.
    _, data = await _telemetry([100, 20, 50, 0])
    assert data["hardware_reserve_soc"] == 0
    assert effective_min_soc(SimpleNamespace(min_soc=20, data=data)) == 20


@pytest.mark.asyncio
async def test_firmware_without_the_registers_reads_as_no_reserve():
    _, data = await _telemetry([100, 20, 0, 0])
    assert data["hardware_reserve_soc"] == 0


@pytest.mark.asyncio
async def test_failed_read_publishes_nothing():
    _, data = await _telemetry(None)
    assert "hardware_reserve_soc" not in data


def test_reserve_is_polled_even_with_its_sensor_disabled():
    drv = _driver()
    for key in ("backup_reserve_soc", "backup_soc_enable"):
        assert key in drv.control_dependency_keys
        assert any(key in g.keys for g in drv.read_groups)


def test_reserve_is_a_visible_diagnostic_sensor():
    sensor = next(s for s in SENSOR_DEFINITIONS if s["key"] == "hardware_reserve_soc")
    assert sensor["unit"] == "%"
    assert sensor["category"] == "diagnostic"
    assert sensor["enabled_by_default"] is True
