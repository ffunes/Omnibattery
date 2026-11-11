"""Huawei: off-grid is a status, not a wattage; and the EMMA names itself.

A SUN2000 reports "Off-grid" as a status bit. The Marstek-style threshold on the
backup port's load would hand a battery that carries a small load through a
power cut back to the PD controller, so the status decides outright, and the
threshold control is not offered at all.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.drivers import DriverCapabilities
from custom_components.omnibattery.drivers.huawei import HuaweiSolarDriver

from .test_huawei_driver import _LIVE_BLOCKS, _fake_client


# ---------------------------------------------------------------- off-grid
class _Coord:
    name = "Huawei"
    backup_offgrid_threshold = 50

    def __init__(self, data):
        self.data = data

    def readings_from_same_poll(self, *_):
        return True


def _active(data, controller=None):
    controller = controller or SimpleNamespace(_backup_cooldown_until={})
    return ChargeDischargeController._is_backup_function_active(controller, _Coord(data))


def test_off_grid_counts_even_with_a_load_below_the_threshold():
    # 30 W on the backup port during an outage: the old rule let the PD
    # controller drive a battery that was carrying the house.
    assert _active({"backup_function": "Off-grid", "ac_offgrid_power": 30}) is True


def test_off_grid_starts_the_cooldown():
    controller = SimpleNamespace(_backup_cooldown_until={})
    coord = _Coord({"backup_function": "Off-grid", "ac_offgrid_power": 0})
    assert ChargeDischargeController._is_backup_function_active(controller, coord) is True
    assert coord in controller._backup_cooldown_until


def test_ready_on_grid_is_not_active():
    assert _active({"backup_function": "Ready", "ac_offgrid_power": 0}) is False


def test_a_disabled_backup_function_is_not_active():
    assert _active({"backup_function": "Disabled", "ac_offgrid_power": 0}) is False


def test_huawei_does_not_offer_the_wattage_threshold():
    driver = HuaweiSolarDriver(
        MagicMock(), "1.2.3.4", port=502, slave_id=4,
        battery_device_id="dev", client=_fake_client(),
    )
    assert driver.capabilities.uses_backup_offgrid_threshold is False
    assert DriverCapabilities.__dataclass_fields__[
        "uses_backup_offgrid_threshold"
    ].default is True


# ---------------------------------------------------------------- EMMA
def _words(text, count):
    raw = text.encode("ascii").ljust(count * 2, b"\x00")
    return [(raw[i] << 8) | raw[i + 1] for i in range(0, count * 2, 2)]


_EMMA_BLOCKS = {
    30015: _words("NS24A1211290", 10),
    30222: _words("EMMA-A02", 20),
    30035: _words("V100R001C00SPC100", 15),
    31657: [0, 0],
}


def _driver(meter=None):
    return HuaweiSolarDriver(
        MagicMock(), "1.2.3.4", port=502, slave_id=4,
        battery_device_id="dev", client=_fake_client(),
        meter_client=meter,
    )


@pytest.mark.asyncio
async def test_the_emma_names_itself():
    driver = _driver(_fake_client(_EMMA_BLOCKS))
    data = await driver.read_telemetry(
        ["emma_serial_number", "emma_model", "emma_firmware_version"]
    )
    assert data["emma_serial_number"] == "NS24A1211290"
    assert data["emma_model"] == "EMMA-A02"
    assert data["emma_firmware_version"] == "V100R001C00SPC100"


@pytest.mark.asyncio
async def test_an_emma_that_does_not_answer_leaves_the_keys_out():
    driver = _driver(_fake_client({31657: [0, 0]}))
    data = await driver.read_telemetry(["emma_model"])
    assert "emma_model" not in data


@pytest.mark.asyncio
async def test_without_an_emma_nothing_is_read_or_offered():
    driver = _driver(None)
    data = await driver.read_telemetry()
    assert not any(key.startswith("emma_") for key in data)
    keys = {d["key"] for d in driver.sensor_definitions}
    assert not any(key.startswith("emma_") for key in keys)


def test_with_an_emma_the_identity_is_a_slow_group():
    driver = _driver(_fake_client(_EMMA_BLOCKS))
    keys = {d["key"] for d in driver.sensor_definitions}
    assert {"emma_serial_number", "emma_model", "emma_firmware_version"} <= keys
    slow = [g for g in driver.read_groups if "emma_model" in g.keys]
    assert slow and slow[0].scan_interval == "very_low"
