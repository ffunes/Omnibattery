"""Read-only parity with the official Indevolt integration.

The points its dashboards and Energy dashboard entries use are published here
too, so users can remove it instead of running both against one battery. They
ride along in the reads that already go out, are never written, and the
per-battery details are read only for the batteries that are present.
"""

from __future__ import annotations

import re

import pytest

from custom_components.omnibattery.drivers.indevolt import (
    BINARY_SENSOR_DEFINITIONS,
    CONTROL_TIER,
    PACK_DETAIL_DEFINITIONS,
    SENSOR_DEFINITIONS,
)
from custom_components.omnibattery.infra.indevolt_client import MAX_POINTS
from tests.indevolt_fake import PACK_DETAIL_POINTS, FakeIndevoltDevice, VirtualClock, connected_driver

_DETAIL = re.compile(r"expansion_battery_(\d)_(soc|cycles|mos_temperature|voltage|current|serial_number)")


async def _settled(**kwargs):
    """Return a connected driver once every tier has been read, the 120 s pack details too."""
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, **kwargs)
    driver = await connected_driver(clock, device, own=False)
    await clock.run(150)
    return clock, device, driver


def _definition(key: str) -> dict:
    return next(d for d in SENSOR_DEFINITIONS + BINARY_SENSOR_DEFINITIONS if d["key"] == key)


def _read_points(device: FakeIndevoltDevice) -> set[int]:
    return {p for r in device.requests if r.endpoint == "Indevolt.GetData" for p in r.config["t"]}


async def test_the_energy_dashboard_points_are_published():
    clock, device, driver = await _settled(extra_points={"11034": 1234, "1505": 56789})
    data = await driver.read_telemetry()
    # The AC-side counters the official integration's Energy dashboard entries use.
    assert data["total_ac_input_energy"] == 1451.22
    assert data["total_ac_output_energy"] == 1078.38
    assert data["off_grid_output_energy"] == 0.28
    # The two Wh counters are published in kWh, like every other energy sensor.
    assert data["bypass_input_energy"] == pytest.approx(1.234)
    assert data["total_solar_energy"] == pytest.approx(56.789)
    assert data["daily_solar_energy"] == 0
    for key in ("ac_input_power", "ac_output_power", "bypass_power", "solar_output_power"):
        assert data[key] == 0, key
    assert data["indevolt_meter_power"] == -7
    for key in ("total_ac_input_energy", "total_ac_output_energy", "off_grid_output_energy",
                "bypass_input_energy", "daily_solar_energy", "total_solar_energy"):
        definition = _definition(key)
        assert definition["unit"] == "kWh" and definition["device_class"] == "energy", key
        assert definition["state_class"] == "total_increasing", key
        assert definition["enabled_by_default"], key
    await driver.close()


async def _hold(clock: VirtualClock, driver, seconds: float) -> dict:
    """Let time pass while the coordinator polls; return the last snapshot."""
    end = clock.now + seconds
    while clock.now < end:
        await driver.read_telemetry()
        await clock.run(2)
    return await driver.read_telemetry()


async def test_ac_input_and_output_follow_the_battery():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(800)  # charging
    data = await _hold(clock, driver, 30)
    assert (data["ac_input_power"], data["ac_output_power"]) == (800, 0)
    await driver.apply_setpoint(-500)  # discharging
    data = await _hold(clock, driver, 30)
    assert (data["ac_input_power"], data["ac_output_power"]) == (0, 500)
    await driver.close()


@pytest.mark.parametrize("reported, shown", [("00005E005301", "00:00:5e:00:53:01"), (None, None)])
async def test_the_mac_address_comes_from_the_device_identity(reported, shown):
    """Sys.GetConfig names the battery's MAC; the panel's device information shows it."""
    clock = VirtualClock()
    driver = await connected_driver(clock, FakeIndevoltDevice(clock, mac=reported))
    assert any(d["key"] == "mac_address" for d in driver.sensor_definitions)
    data = await driver.read_telemetry()
    assert data.get("mac_address") == shown
    await driver.close()


async def test_the_indevolt_meter_is_not_presented_as_the_grid_meter():
    # 11016 trails the P1 meter by 10-20 s: its own name, and off by default,
    # so nobody mistakes it for the grid sensor Omnibattery regulates on.
    assert _definition("indevolt_meter_power")["enabled_by_default"] is False
    assert _definition("indevolt_meter_connected")["enabled_by_default"] is False
    clock, device, driver = await _settled()
    assert (await driver.read_telemetry())["indevolt_meter_connected"] is True
    device.extra_points["7120"] = 1001
    await clock.run(70)
    assert (await driver.read_telemetry())["indevolt_meter_connected"] is False
    await driver.close()


@pytest.mark.parametrize("status_family", [1000, 0])
async def test_the_diagnostics_decode_like_the_device_reports_them(status_family):
    clock, device, driver = await _settled(status_family=status_family)
    data = await driver.read_telemetry()
    assert data["realtime_command"] == "standby"
    assert data["device_mode"] == "standalone"  # 606 arrives as the string "1002"
    assert data["feed_in_limit"] == 2400
    assert data["grid_charging_allowed"] == 1001
    assert (data["grid_voltage"], data["grid_frequency"]) == (240.1, 49.99)
    # 9999 means "does not apply" (here: not charging), so it is left out.
    assert "remaining_charging_time" not in data
    assert data["remaining_discharging_time"] == 374
    pv = (data["pv1_voltage"], data["pv4_voltage"], data["pv1_current"], data["pv1_power"])
    assert pv == (0.8, 0.9, 0, 0)
    assert data["inverter_battery_soc"] == 31
    assert data["inverter_battery_cycles"] == 178
    assert data["inverter_battery_mos_temperature"] == 35
    assert (data["inverter_battery_voltage"], data["inverter_battery_current"]) == (6.55, 11.3)
    assert data["inverter_battery_serial_number"] == "022509270193"
    await driver.close()


@pytest.mark.parametrize("style", ["missing", "empty"])
@pytest.mark.parametrize("packs", [0, 3, 5])
async def test_pack_details_follow_the_packs_that_are_present(packs, style):
    clock, device, driver = await _settled(packs=packs, absent_pack_style=style)
    data = await driver.read_telemetry()
    present = list(range(1, packs + 1))
    assert sorted({int(m[1]) for k in data if (m := _DETAIL.fullmatch(k))}) == present
    for n in present:
        assert data[f"expansion_battery_{n}_soc"] == 30 + n
        assert data[f"expansion_battery_{n}_cycles"] == 170 + n
        assert data[f"expansion_battery_{n}_serial_number"] == f"PACK{n}SN"
    defined = {int(m[1]) for d in driver.sensor_definitions if (m := _DETAIL.fullmatch(d["key"]))}
    assert sorted(defined) == present
    grouped = {k for g in driver.read_groups for k in g.keys}
    assert {d["key"] for n in present for d in PACK_DETAIL_DEFINITIONS[n - 1]} <= grouped
    # The details of an absent battery are never asked for.
    read = _read_points(device)
    for n in range(packs + 1, len(PACK_DETAIL_POINTS) + 1):
        assert not read & set(PACK_DETAIL_POINTS[n - 1]), n
    for n in present:
        assert set(PACK_DETAIL_POINTS[n - 1]) <= read, n
    await driver.close()


def test_the_battery_details_ship_disabled():
    for definitions in PACK_DETAIL_DEFINITIONS:
        assert [d["enabled_by_default"] for d in definitions] == [False] * len(definitions)


async def test_the_parity_points_add_no_requests():
    # A full battery (five expansion packs): everything rides along in the one
    # read per slot, no batch exceeds the limit, and no point is missing.
    clock, device, driver = await _settled(packs=5)
    start, before = clock.now, len(device.requests)
    await clock.run(600)
    reads = [r for r in device.requests[before:] if r.endpoint == "Indevolt.GetData"]
    assert len(reads) <= 600 / driver.scheduler.read_interval_s + 1
    assert all(len(r.config["t"]) <= MAX_POINTS for r in reads)
    assert all(set(CONTROL_TIER.points) <= set(r.config["t"]) for r in reads)
    assert driver.scheduler.max_batch == MAX_POINTS
    assert device.config_writes == []  # read-only: nothing but the realtime handover STOP
    assert clock.now - start == 600
    await driver.close()


async def test_only_ride_along_tiers_can_change_their_points():
    clock, device, driver = await _settled()
    with pytest.raises(ValueError):
        driver.scheduler.set_tier_points("control", (6002,))
    with pytest.raises(KeyError):
        driver.scheduler.set_tier_points("nope", ())
    await driver.close()
