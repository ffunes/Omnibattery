"""What an Indevolt reports about itself: operating status, forced full charge, cells.

11006 says what the device is doing on its own (deep sleep while idle, a
low-battery recovery charge, its forced full charge), 8646/8647 when it runs
that forced full charge, and the cell points the voltage of each battery's two
cells. All are read in the batched reads the driver already makes.
"""

from __future__ import annotations

import logging

import pytest

from custom_components.omnibattery.drivers import indevolt
from tests.indevolt_fake import (
    INVERTER_BATTERY_CELL_POINTS,
    PACK_CELL_POINTS,
    FakeIndevoltDevice,
    VirtualClock,
    connected_driver,
)




@pytest.mark.parametrize("raw, key", [(14, "deep_sleep"), (15, "scheduled_full_charge"),
                                      (13, "low_battery_charging"), (5, "current_limiting")])
async def test_the_operating_status_is_published_as_a_key(raw, key):
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, operating_status=raw)
    driver = await connected_driver(clock, device)
    await clock.run(70)
    assert (await driver.read_telemetry())["operating_status"] == key
    await driver.close()


async def test_an_unknown_operating_status_is_left_out():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, operating_status=99)
    driver = await connected_driver(clock, device)
    await clock.run(70)
    assert "operating_status" not in await driver.read_telemetry()
    await driver.close()


async def test_operating_status_changes_are_logged(caplog):
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, operating_status=14)
    driver = await connected_driver(clock, device)
    await clock.run(70)
    with caplog.at_level(logging.INFO, logger="custom_components.omnibattery.drivers.indevolt"):
        device.operating_status = 15
        await clock.run(70)
    assert any("deep_sleep" in r.getMessage() and "scheduled_full_charge" in r.getMessage()
               for r in caplog.records)
    await driver.close()


@pytest.mark.parametrize("interval, start, shown", [(30, 0x0C00, "12:00"), (0, 0x0000, "00:00"),
                                                   (7, 0x172D, "23:45")])
async def test_the_forced_full_charge_setting_is_shown(interval, start, shown):
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, full_charge_interval_days=interval, full_charge_start=start)
    driver = await connected_driver(clock, device, own=False)
    await clock.run(200)
    data = await driver.read_telemetry()
    assert (data["full_charge_interval"], data["full_charge_time"]) == (interval, shown)
    await driver.close()


@pytest.mark.parametrize("style", ["missing", "empty"])
@pytest.mark.parametrize("packs, highest", [(0, 3.34), (3, 3.37), (5, 3.39)])
async def test_the_highest_and_lowest_cell_voltage_cover_the_batteries_present(packs, highest, style):
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, packs=packs, absent_pack_style=style)
    driver = await connected_driver(clock, device, own=False)
    await clock.run(400)
    data = await driver.read_telemetry()
    assert data["highest_cell_voltage"] == pytest.approx(highest)
    assert data["lowest_cell_voltage"] == pytest.approx(3.34)
    # Its own names: the core's taper and hysteresis logic reads max_cell_voltage.
    assert "max_cell_voltage" not in data and "min_cell_voltage" not in data
    read = {p for r in device.requests if r.endpoint == "Indevolt.GetData" for p in r.config["t"]}
    assert set(INVERTER_BATTERY_CELL_POINTS) <= read
    for n, cells in enumerate(PACK_CELL_POINTS, start=1):
        assert (set(cells) <= read) == (n <= packs), n
    await driver.close()


def test_the_new_diagnostics_are_defined():
    keys = {d["key"]: d for d in indevolt.SENSOR_DEFINITIONS}
    assert keys["operating_status"]["category"] == "diagnostic"
    assert keys["full_charge_interval"]["unit"] == "d"
    assert keys["full_charge_time"]["data_type"] == "char"
    for key in ("highest_cell_voltage", "lowest_cell_voltage"):
        assert keys[key]["enabled_by_default"] is False and keys[key]["unit"] == "V"
