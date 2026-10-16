"""Indevolt user settings: written on user intent only, adopted from the device.

The three settings with an Omnibattery counterpart (min SOC 1142 -> 6105, max
charge 1138 -> 11009, max discharge 1147 -> 11011) sync both ways. These tests
opt their fake device into config writes; the autouse guard still fails on any
other flash point.
"""

from __future__ import annotations

from custom_components.omnibattery.drivers.indevolt_settings import (
    DAILY_BUDGET,
    DEBOUNCE_S,
    SYNCED_PREFIX,
)
from custom_components.omnibattery.infra.indevolt_client import SLOT_S, pacing_levels
from tests.indevolt_fake import FakeIndevoltDevice, RecordingNotifier, VirtualClock, connected_driver

MIN = "discharging_cutoff_capacity"


def _store(**synced) -> dict:
    """A battery config as the coordinator keeps it, with agreed values."""
    store: dict = {}
    for key, value in synced.items():
        store[key] = value
        store[SYNCED_PREFIX + key] = value
    return store


async def _driver(clock, device, store, **kwargs):
    kwargs.setdefault("notifier", RecordingNotifier())
    return await connected_driver(clock, device, own=False, persist=store.__setitem__,
                                  stored=store.get, **kwargs)


async def test_an_entity_change_is_written_once_after_the_debounce():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1, allow_config_writes=True)
    store = _store(min_soc=15, max_charge_power=2400, max_discharge_power=2400)
    driver = await _driver(clock, device, store)
    assert await driver.write_control(MIN, 20) is True
    assert (await driver.read_telemetry())[MIN] == 20  # shown at once
    await clock.run(DEBOUNCE_S - 1)
    assert device.config_writes == []
    # After the debounce the write waits for its turn behind the scheduler's
    # request: at most two slots.
    assert await clock.run_until(lambda: device.config_writes, 1 + 2 * SLOT_S + 1)
    assert device.config_writes == [(1142, [20])]
    await clock.run(1)  # the reply
    assert store["min_soc"] == store[SYNCED_PREFIX + "min_soc"] == 20
    await clock.run(400)  # the read-back confirms; nothing more is written
    assert device.config_writes == [(1142, [20])]
    assert (await driver.read_telemetry())[MIN] == 20
    times = device.request_times()
    assert min(b - a for a, b in zip(times, times[1:])) >= pacing_levels(SLOT_S)[0].floor_s  # shares the gate
    await driver.close()


async def test_a_change_back_before_the_last_write_reads_back_is_still_written():
    """A change back, made before the previous write reads back, is still written.

    The battery applies a setting a few seconds after accepting it, so the read
    right after a 15 -> 16 write still says 15. The change back to 15 is not
    skipped as "already 15", and the slider does not jump to the unconfirmed 16.
    """
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1, allow_config_writes=True,
                                config_apply_delay_s=8.0)
    store = _store(min_soc=15)
    driver = await _driver(clock, device, store)
    assert await driver.write_control(MIN, 16)
    assert await clock.run_until(lambda: device.config_writes, DEBOUNCE_S + 2 * SLOT_S + 1)
    await clock.run(2 * SLOT_S)  # the expedited read-back, before the battery applied it
    assert driver.scheduler.value(6105) == 15
    assert await driver.write_control(MIN, 15)
    await clock.run(DEBOUNCE_S + 3 * SLOT_S)
    assert device.config_writes == [(1142, [16]), (1142, [15])]
    await clock.run(400)
    assert device.native_min_soc == 15
    assert (await driver.read_telemetry())[MIN] == 15
    assert store["min_soc"] == store[SYNCED_PREFIX + "min_soc"] == 15
    await driver.close()


async def test_a_value_the_device_already_holds_is_not_written():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1, allow_config_writes=True)
    driver = await _driver(clock, device, _store(min_soc=15))
    assert await driver.write_control(MIN, 15)
    assert await driver.write_control("max_charge_power", 2400)
    await clock.run(60)
    assert device.config_writes == []
    await driver.close()


async def test_a_burst_of_changes_collapses_to_the_last_value():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1, allow_config_writes=True)
    driver = await _driver(clock, device, _store(max_discharge_power=2400))
    for value in (2000, 1800, 1500, 1200):
        assert await driver.write_control("max_discharge_power", value)
        await clock.run(1)
    await clock.run(DEBOUNCE_S + 3 * SLOT_S)
    assert device.config_writes == [(1147, [1200])]
    assert device.discharge_setting_w == 1200
    await driver.close()


async def test_the_daily_budget_refuses_further_writes_and_says_why():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1, allow_config_writes=True)
    notifier = RecordingNotifier()
    driver = await _driver(clock, device, _store(min_soc=15), notifier=notifier)
    for n in range(DAILY_BUDGET):
        assert await driver.write_control(MIN, 20 + n)
        await clock.run(DEBOUNCE_S + 3 * SLOT_S)
    assert len(device.config_writes) == DAILY_BUDGET
    assert await driver.write_control(MIN, 40) is False
    assert notifier.count("setting_budget", MIN) == 1
    await clock.run(DEBOUNCE_S + 3 * SLOT_S)
    assert len(device.config_writes) == DAILY_BUDGET
    await clock.run(86400)
    assert await driver.write_control(MIN, 40)  # a day later it is allowed again
    await clock.run(DEBOUNCE_S + 3 * SLOT_S)
    assert device.config_writes[-1] == (1142, [40])
    await driver.close()


async def test_a_refused_value_saved_by_the_entity_is_not_written_after_a_restart():
    """The core's number entity stores the value even when the write was refused.
    The sync puts the agreed value back, so the next setup does not write it."""
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1, allow_config_writes=True, reject_config=True)
    store = _store(min_soc=15, max_charge_power=2400, max_discharge_power=2400)
    driver = await _driver(clock, device, store)
    assert await driver.write_control(MIN, 30)
    store["min_soc"] = 30  # what MarstekVenusNumber persists after write_control
    await clock.run(400)
    assert store["min_soc"] == 15
    await driver.close()
    device.reject_config = False
    attempts = len(device.config_writes)  # the one rejected attempt
    driver = await _driver(clock, device, store)
    await driver.apply_config(max_soc_pct=100, min_soc_pct=store["min_soc"],
                              max_charge_power_w=2400, max_discharge_power_w=2400)
    await clock.run(60)
    assert len(device.config_writes) == attempts == 1
    await driver.close()


async def test_a_change_made_in_the_indevolt_app_is_adopted_without_a_write():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1, allow_config_writes=True)
    store = _store(min_soc=15, max_charge_power=2400, max_discharge_power=2400)
    driver = await _driver(clock, device, store)
    device.native_min_soc = 25
    device.charge_setting_w = 1500
    await clock.run(400)  # the limits tier comes round
    data = await driver.read_telemetry()
    assert data[MIN] == 25 and data["max_charge_power"] == 1500
    assert store["min_soc"] == 25 and store["max_charge_power"] == 1500
    assert store[SYNCED_PREFIX + "min_soc"] == 25
    assert device.config_writes == []
    await driver.close()


async def test_first_start_adopts_the_device_and_writes_nothing():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1, allow_config_writes=True)
    store = {"min_soc": 10, "max_charge_power": 1000, "max_discharge_power": 1000}
    driver = await _driver(clock, device, store)
    await driver.apply_config(max_soc_pct=100, min_soc_pct=10, max_charge_power_w=1000,
                              max_discharge_power_w=1000)
    await clock.run(60)
    assert device.config_writes == []
    assert store["min_soc"] == 15 and store["max_charge_power"] == 2400
    assert store[SYNCED_PREFIX + "max_discharge_power"] == 2400
    await driver.close()


async def test_an_options_flow_change_is_written_at_setup():
    """Saving the options reloads the entry; setup passes the new values."""
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1, allow_config_writes=True)
    store = _store(min_soc=15, max_charge_power=2400, max_discharge_power=2400)
    store.update(min_soc=20, max_discharge_power=1800)  # what the options flow saved
    driver = await _driver(clock, device, store)
    await driver.apply_config(max_soc_pct=90, min_soc_pct=20, max_charge_power_w=2400,
                              max_discharge_power_w=1800)
    await clock.run(DEBOUNCE_S + 5 * SLOT_S)
    assert sorted(device.config_writes) == [(1142, [20]), (1147, [1800])]
    assert store[SYNCED_PREFIX + "min_soc"] == 20
    await driver.close()


async def test_a_restart_after_an_app_change_keeps_the_app_value():
    """The config still says 15, but so does the agreed value: the device's 25 wins."""
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1, allow_config_writes=True, native_min_soc=25)
    store = _store(min_soc=15)
    driver = await _driver(clock, device, store)
    await driver.apply_config(max_soc_pct=100, min_soc_pct=15, max_charge_power_w=2400,
                              max_discharge_power_w=2400)
    await clock.run(60)
    assert device.config_writes == []
    assert store["min_soc"] == 25
    await driver.close()


async def test_a_rejected_write_is_reported_and_the_device_value_returns():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1, allow_config_writes=True, reject_config=True)
    notifier = RecordingNotifier()
    store = _store(min_soc=15)
    driver = await _driver(clock, device, store, notifier=notifier)
    assert await driver.write_control(MIN, 30)
    await clock.run(DEBOUNCE_S + 3 * SLOT_S)
    assert notifier.count("setting_failed", MIN, 30) == 1
    assert (await driver.read_telemetry())[MIN] == 15
    assert store["min_soc"] == 15
    await driver.close()


async def test_out_of_range_and_unknown_settings_are_refused():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1, allow_config_writes=True)
    driver = await _driver(clock, device, _store())
    assert await driver.write_control(MIN, 3) is False
    assert await driver.write_control("max_charge_power", 50) is False
    assert await driver.write_control("feedin_power_limit", 100) is False
    await clock.run(60)
    assert device.config_writes == []
    await driver.close()


async def test_numbers_use_the_hardware_ceilings_and_the_settings_drive_the_clamp():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, limit_w=3000, discharge_setting_w=1000, feedin_w=100)
    driver = await connected_driver(clock, device)
    numbers = {d["key"]: d for d in driver.number_definitions}
    assert set(numbers) == {MIN, "max_charge_power", "max_discharge_power"}
    assert numbers["max_charge_power"]["max"] == numbers["max_discharge_power"]["max"] == 3000
    data = await driver.read_telemetry()
    assert data["max_discharge_power"] == 1000 and data["max_charge_power"] == 3000
    # The device's own setting caps discharge; the 100 W feed-in limit does not.
    assert (await driver.apply_setpoint(-2500)).net_power_w == -1000
    await driver.close()
