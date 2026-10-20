"""The Indevolt near full: no stale Standby, and a charge limit that follows the BMS taper.

After a start from standby the device's battery state (6001, read on a slow
tier) still shows the standby from before the command for a while. The driver
publishes that reading as unknown until the battery had time to start, so the
core's BMS-cutoff detector does not take a battery that is starting for a full
one, while a battery that stays in standby still shows it.

Near full an SF2000's BMS tapers the charge on its own (its constant-voltage
phase), from 2.4kW down to about 200W while the commanded power stays at the
maximum. The core counts a charge as delivered from 10% of the command, so a
battery that takes 235W of 2400W reads as "not delivering". The driver reports
what the battery takes as its live charge limit instead, and the core commands
that.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.control import weekly_full_charge
from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
    _BMS_CUTOFF_REQUIRED_CYCLES,
)
from custom_components.omnibattery.drivers import indevolt
from custom_components.omnibattery.drivers.base import DELIVERED_AC_POWER_KEY
from custom_components.omnibattery.drivers.indevolt import START_GRACE_S, ControlState
from tests.indevolt_fake import FakeIndevoltDevice, VirtualClock, connected_driver

FULL_W = 2400


async def _core(clock, driver, seconds, *, want=FULL_W, log=None, step=1.0):
    """The core's side of the loop: each cycle it commands what the PD wants (a
    power, or a callable for a PD that keeps raising it), capped by the driver's
    live charge limit as ``_battery_power_limit`` does, and nothing once the
    battery reports 100%."""
    end = clock.now + seconds
    while clock.now < end:
        data = await driver.read_telemetry()
        limit = driver.dynamic_charge_limit_w(data)
        target = want() if callable(want) else want
        command = target if limit is None else min(target, limit)
        if data.get("battery_soc", 0) >= 100:
            command = 0
        await driver.apply_setpoint(command)
        if log is not None:
            log.append((clock.now, command, data.get(DELIVERED_AC_POWER_KEY), limit))
        await clock.run(step)


def _writes(device):
    return [v for t, v in device.writes if t == 47015]


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


async def _cutoff_counts(clock, driver, device, seconds, monkeypatch):
    # A detector that times the cutoff reads ``monotonic``: give it the test clock.
    monkeypatch.setattr(weekly_full_charge, "monotonic", lambda: clock.now, raising=False)
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


async def test_a_restart_at_99_percent_is_not_taken_for_a_bms_cutoff(monkeypatch):
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=99.0, start_delay_s=15.0, acceptance=lambda soc: 200.0)
    driver = await connected_driver(clock, device)
    await clock.run(90)  # standby, read as such
    counts = await _cutoff_counts(clock, driver, device, 60, monkeypatch)
    assert max(count for _, count in counts) < _BMS_CUTOFF_REQUIRED_CYCLES
    await driver.close()


async def test_a_battery_that_refuses_at_99_percent_is_still_a_bms_cutoff(monkeypatch):
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=99.0, acceptance=lambda soc: 0.0)
    driver = await connected_driver(clock, device)
    await clock.run(90)
    counts = await _cutoff_counts(clock, driver, device, START_GRACE_S + 30, monkeypatch)
    assert counts[-1][1] >= _BMS_CUTOFF_REQUIRED_CYCLES
    await driver.close()


# --- the charge limit follows the taper ---------------------------------------------


async def test_the_charge_limit_follows_a_battery_that_takes_less():
    clock = VirtualClock()
    takes = {"w": 600.0}
    device = FakeIndevoltDevice(clock, soc=95.0, acceptance=lambda soc: takes["w"])
    driver = await connected_driver(clock, device)
    await _core(clock, driver, indevolt.TAPER_CONFIRM_S + 15)
    limit = driver.dynamic_charge_limit_w({})
    assert limit is not None and 600 < limit <= 600 + indevolt.TAPER_HEADROOM_W
    assert device.rt_power == limit  # the core commands what the battery takes
    takes["w"] = 300.0
    await _core(clock, driver, 20)
    limit = driver.dynamic_charge_limit_w({})
    assert 300 < limit <= 300 + indevolt.TAPER_HEADROOM_W
    await driver.close()


async def test_through_a_whole_taper_the_core_never_sees_a_non_delivery():
    # The BMS takes 2400W, then tapers to 100W over 13 minutes, as a real
    # SF2000 does near full. From the moment the limit is set, every command is
    # met by more than the core's 10% bar, and the shortfall stays below the
    # 150W at which the core's anti-windup re-anchors the PD. (A large battery,
    # so the taper follows the clock and SOC stays below 100%.)
    clock = VirtualClock()
    start = clock.now + 120

    def takes(soc):
        progress = min(1.0, max(0.0, (clock.now - start) / 780))
        return FULL_W - progress * (FULL_W - 100)

    device = FakeIndevoltDevice(clock, soc=90.0, capacity_kwh=20.0, acceptance=takes)
    driver = await connected_driver(clock, device)
    log = []
    await _core(clock, driver, 120 + 780 + 60, log=log)
    engaged = next(t for t, _, _, limit in log if limit is not None)
    judged = [(c, d) for t, c, d, _ in log if t >= engaged + 10 and c >= 100]
    assert judged
    assert all(d >= 0.10 * c for c, d in judged)
    assert max(c - d for c, d in judged) <= 150
    await driver.close()


async def test_the_taper_costs_fewer_writes_than_the_pd_dips_it_replaces():
    clock = VirtualClock()
    start = clock.now + 60

    def takes(soc):
        progress = min(1.0, max(0.0, (clock.now - start) / 780))
        return FULL_W - progress * (FULL_W - 230)

    device = FakeIndevoltDevice(clock, soc=90.0, capacity_kwh=20.0, acceptance=takes)
    driver = await connected_driver(clock, device)
    await _core(clock, driver, 60)
    before = len(_writes(device))
    await _core(clock, driver, 780)
    # 2400W down to 230W in 50W steps is about 45 changes; the core's PD dips
    # caused 145 setpoint changes over the same taper without the limit.
    assert len(_writes(device)) - before <= 60
    await driver.close()


async def test_a_battery_that_takes_everything_again_gets_its_limit_back():
    clock = VirtualClock()
    takes = {"w": 600.0}
    device = FakeIndevoltDevice(clock, soc=95.0, acceptance=lambda soc: takes["w"])
    driver = await connected_driver(clock, device)
    await _core(clock, driver, indevolt.TAPER_CONFIRM_S + 15)
    assert driver.dynamic_charge_limit_w({}) is not None
    takes["w"] = 10_000.0
    await _core(clock, driver, 60)
    assert driver.dynamic_charge_limit_w({}) is None
    assert device.power_w == pytest.approx(FULL_W, abs=1)
    await driver.close()


async def test_a_reading_that_jumps_a_little_does_not_raise_the_limit():
    # A tapering battery's readings jump up by 20–30W now and then. Just below
    # a 50W step the limit sits only 51W above what the battery takes, so such
    # a jump must not count as taking all it is allowed.
    clock = VirtualClock()
    takes = {"w": 1049.0}
    device = FakeIndevoltDevice(clock, soc=95.0, capacity_kwh=20.0, acceptance=lambda soc: takes["w"])
    driver = await connected_driver(clock, device)
    await _core(clock, driver, indevolt.TAPER_CONFIRM_S + 15)
    assert driver.dynamic_charge_limit_w({}) == 1100
    log = []
    for _ in range(12):
        takes["w"] = 1074.0
        await _core(clock, driver, 5, log=log)
        takes["w"] = 1049.0
        await _core(clock, driver, 5, log=log)
    assert {limit for *_, limit in log} == {1100}
    assert max(command for _, command, _, _ in log) == 1100
    await driver.close()


async def test_a_battery_a_little_short_of_its_limit_gets_it_back():
    # A battery that takes all it is allowed may read a few watts under the
    # command; the limit still goes back up to full.
    clock = VirtualClock()
    takes = {"w": 600.0}

    def acceptance(soc):
        return takes["w"] if takes["w"] is not None else device.rt_power - 15

    device = FakeIndevoltDevice(clock, soc=95.0, acceptance=acceptance)
    driver = await connected_driver(clock, device)
    await _core(clock, driver, indevolt.TAPER_CONFIRM_S + 15)
    assert driver.dynamic_charge_limit_w({}) is not None
    takes["w"] = None
    await _core(clock, driver, 90)
    assert driver.dynamic_charge_limit_w({}) is None
    assert device.rt_power == FULL_W
    await driver.close()


async def test_a_step_up_the_battery_follows_is_not_limited():
    # Seen over HTTP a running SF2000 takes 9–15s to follow a step from 80W to
    # 2400W, its power rising all the way.
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=50.0, ramp_w_per_s=150.0)
    driver = await connected_driver(clock, device)
    await _core(clock, driver, 60, want=80)
    log = []
    await _core(clock, driver, 60, log=log)
    assert all(limit is None for *_, limit in log)
    assert device.power_w == pytest.approx(FULL_W, abs=1)
    await driver.close()


async def test_a_battery_that_lags_a_rising_command_is_not_limited():
    # The PD raises the charge a little every cycle and the battery follows a
    # few hundred watts behind, its power rising with the command.
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=30.0)
    driver = await connected_driver(clock, device)
    await _core(clock, driver, 30, want=400)
    device.ramp_w_per_s = 15.0
    start = clock.now
    log = []
    await _core(clock, driver, 60, want=lambda: 400 + 20 * (clock.now - start), log=log)
    assert max(c - d for _, c, d, _ in log if d is not None) > 200  # well past the bar
    assert all(limit is None for *_, limit in log)
    await driver.close()


async def test_a_taper_is_limited_while_the_core_still_raises_the_command():
    # A battery that takes less makes the grid export, so the PD raises the
    # charge further; its falling power marks the taper all the same.
    clock = VirtualClock()
    fall_from = {"t": None}

    def takes(soc):
        if fall_from["t"] is None:
            return 10_000.0
        return max(1000.0, 1750.0 - 75.0 * (clock.now - fall_from["t"]))

    device = FakeIndevoltDevice(clock, soc=97.0, capacity_kwh=20.0, acceptance=takes)
    driver = await connected_driver(clock, device)
    await _core(clock, driver, 30, want=1750)
    fall_from["t"] = start = clock.now
    log = []
    await _core(clock, driver, 40, want=lambda: min(FULL_W, 1750 + 20 * (clock.now - start)),
                log=log)
    engaged = next(t for t, *_, limit in log if limit is not None)
    assert engaged - start <= indevolt.TAPER_CONFIRM_S + 5  # the command still rises then
    await driver.close()


async def test_a_slow_start_from_standby_is_not_a_taper():
    # After a long standby an SF2000 shows AC power only ~16s after the command.
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=50.0, start_delay_s=16.0, ramp_w_per_s=250.0)
    driver = await connected_driver(clock, device)
    log = []
    await _core(clock, driver, 90, log=log)
    assert all(limit is None for *_, limit in log)
    assert device.power_w == pytest.approx(FULL_W, abs=1)
    await driver.close()


async def test_a_start_into_a_tapering_battery_is_limited_before_the_core_judges_it():
    # Charging resumes at 98% after the hysteresis: the battery starts ~16s
    # after the command and takes only ~960W. The core's engage grace is 30s
    # from the command; the limit must be set by then plus the few cycles the
    # core needs to exclude a battery (6 at 1s).
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=98.0, start_delay_s=16.0, acceptance=lambda soc: 960.0)
    driver = await connected_driver(clock, device)
    commanded_at = clock.now
    log = []
    await _core(clock, driver, 60, log=log)
    engaged = next(t for t, _, _, limit in log if limit is not None)
    assert engaged - commanded_at <= 30 + 5
    assert 960 < driver.dynamic_charge_limit_w({}) <= 960 + indevolt.TAPER_HEADROOM_W
    await driver.close()


async def test_a_battery_that_never_moves_is_left_to_the_core():
    # No charge at all is a stall for the core's non-delivery handling, not a
    # taper: the limit stays off, so the core keeps judging the full command.
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=60.0, acceptance=lambda soc: 0.0)
    driver = await connected_driver(clock, device)
    log = []
    await _core(clock, driver, 120, log=log)
    assert all(limit is None for *_, limit in log)
    await driver.close()


async def test_the_limit_ends_with_the_charge_and_is_zero_while_paused():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=95.0, acceptance=lambda soc: 600.0)
    driver = await connected_driver(clock, device)
    await _core(clock, driver, indevolt.TAPER_CONFIRM_S + 15)
    assert driver.dynamic_charge_limit_w({}) is not None
    await _core(clock, driver, 10, want=0)
    assert driver.dynamic_charge_limit_w({}) is None
    device.set_mode(1)
    assert await clock.run_until(lambda: driver.control_state is ControlState.PAUSED, 30)
    assert driver.dynamic_charge_limit_w({}) == 0
    await driver.close()


async def test_the_limit_is_logged_when_it_starts_and_ends(caplog):
    clock = VirtualClock()
    takes = {"w": 600.0}
    device = FakeIndevoltDevice(clock, soc=95.0, acceptance=lambda soc: takes["w"])
    driver = await connected_driver(clock, device)
    with caplog.at_level(logging.INFO, logger="custom_components.omnibattery.drivers.indevolt"):
        await _core(clock, driver, indevolt.TAPER_CONFIRM_S + 15)
        takes["w"] = 10_000.0
        await _core(clock, driver, 60)
    messages = [r.getMessage() for r in caplog.records]
    assert any("tapers" in m and "600W" in m for m in messages)
    assert any("full charge power again" in m for m in messages)
    await driver.close()
