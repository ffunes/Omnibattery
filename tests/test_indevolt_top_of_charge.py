"""An Indevolt that starts from standby is not taken for a full battery.

After a start from standby the device's battery state (6001, read on a slow
tier) still shows the standby from before the command for a while. The driver
publishes that reading as unknown until the battery had time to start, so the
core's BMS-cutoff detector does not take a battery that is starting for a full
one, while a battery that stays in standby still shows it.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
    _BMS_CUTOFF_REQUIRED_CYCLES,
)
from custom_components.omnibattery.drivers.indevolt import START_GRACE_S
from tests.indevolt_fake import FakeIndevoltDevice, VirtualClock, connected_driver


# --- no stale Standby after a start ---------------------------------------------------


async def test_a_standby_read_before_the_battery_could_start_is_unknown():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=70.0, start_delay_s=15.0)
    driver = await connected_driver(clock, device)
    await clock.run(90)  # idle long enough for the echo tier to show standby
    await driver.apply_setpoint(800)
    seen = []
    for _ in range(int(START_GRACE_S) + 15):
        seen.append((clock.now, (await driver.read_telemetry()).get("inverter_state", "absent")))
        await clock.run(1)
    accepted = next(r.done_at for r in device.set_data_requests if r.config["v"][:2] == [1, 800])
    within = [state for t, state in seen if accepted <= t < accepted + START_GRACE_S]
    assert within and 1 not in within  # never "Standby" while it starts
    assert seen[-1][1] == 2  # read again once the grace is over: charging
    await driver.close()


async def test_a_battery_that_stays_in_standby_shows_it_after_the_grace():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=99.0, acceptance=lambda soc: 0.0)
    driver = await connected_driver(clock, device)
    await clock.run(90)
    await driver.apply_setpoint(800)
    await clock.run(START_GRACE_S + 2 * driver.scheduler.read_interval_s + 1)
    assert (await driver.read_telemetry())["inverter_state"] == 1
    await driver.close()


async def test_standby_while_already_charging_is_shown_at_once():
    # A power change while the battery runs is no start: a dropout shows.
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=70.0)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(800)
    await clock.run(60)
    await driver.apply_setpoint(900)
    await clock.run(5)
    device.action_state = 0  # the BMS stops it on its own
    await clock.run(START_GRACE_S + 5)
    device.action_state = 0
    assert await clock.run_until(
        lambda: driver._decode().get("inverter_state") == 1, START_GRACE_S + 10)
    await driver.close()


class _Coordinator(SimpleNamespace):
    """What the BMS-cutoff detector reads from a coordinator, fed by the driver.

    Merges each snapshot like the real coordinator: a key the driver leaves out
    keeps its last value, an explicit None replaces it.
    """

    __hash__ = object.__hash__


async def _cutoff_counts(clock, driver, device, seconds):
    coord = _Coordinator(name="Indevolt", brand="indevolt", battery_version=None, data={},
                         commanded_charge_power=0, battery_manual_mode_enabled=False)
    mgr = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    mgr._controller = SimpleNamespace(coordinators=[coord], weekly_full_charge_enabled=False)
    mgr._bms_cutoff_counts = {}
    mgr._already_complete_logged = False
    mgr.is_active = lambda: False
    coord.data.update(await driver.read_telemetry())
    await driver.apply_setpoint(800)
    coord.commanded_charge_power = 800
    counts = []
    end = clock.now + seconds
    while clock.now < end:
        coord.data.update(await driver.read_telemetry())
        mgr.tick_bms_cutoff()
        counts.append((clock.now, mgr._bms_cutoff_counts.get("Indevolt", 0)))
        await clock.run(0.67)  # meter events and 2s safety ticks together
    return counts


async def test_a_restart_at_99_percent_is_not_taken_for_a_bms_cutoff():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=99.0, start_delay_s=15.0, acceptance=lambda soc: 200.0)
    driver = await connected_driver(clock, device)
    await clock.run(90)  # standby, read as such
    counts = await _cutoff_counts(clock, driver, device, 60)
    assert max(count for _, count in counts) < _BMS_CUTOFF_REQUIRED_CYCLES
    await driver.close()


async def test_a_battery_that_refuses_at_99_percent_is_still_a_bms_cutoff():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=99.0, acceptance=lambda soc: 0.0)
    driver = await connected_driver(clock, device)
    await clock.run(90)
    counts = await _cutoff_counts(clock, driver, device, START_GRACE_S + 30)
    assert counts[-1][1] >= _BMS_CUTOFF_REQUIRED_CYCLES
    await driver.close()
