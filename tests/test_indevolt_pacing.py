"""Pacing, writes on demand, coalescing and batch fallback of the Indevolt scheduler.

All on a virtual clock against the fake device: spacing is exact, so the
assertions use the real constants without tolerance fudging.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.omnibattery.drivers.indevolt import CONTROL_TIER, RAMP_S, ControlState
from custom_components.omnibattery.infra import indevolt_client as ic
from custom_components.omnibattery.infra.indevolt_client import (
    MAXIMUM_LEVEL,
    MIN_REPLY_GAP_S,
    MIN_SLOT_S,
    MIN_SPACING_S,
    PACING_LADDER,
    REQUEST_SPEED_SLOT_S,
    SLOT_S,
    IndevoltClient,
    IndevoltScheduler,
    PacingLevel,
    Tier,
    pacing_levels,
    slot_for_request_speed,
)
from tests.indevolt_fake import FakeIndevoltDevice, VirtualClock, connected_driver


def _gaps(times: list[float]) -> list[float]:
    return [round(b - a, 6) for a, b in zip(times, times[1:])]


def _floor(slot_s: float) -> float:
    return pacing_levels(slot_s)[0].floor_s


def _assert_paced(requests, slot_s: float = SLOT_S) -> None:
    """The pacing contract of a speed: its floor between any two requests
    (from the previous reply at Maximum, otherwise from its start), its read
    interval between two reads and its write interval between two writes."""
    level = pacing_levels(slot_s)[0]
    for previous, current in zip(requests, requests[1:]):
        since = previous.done_at if level.from_reply else previous.at
        assert round(current.at - since, 6) >= level.floor_s
    for endpoint, interval in (("Indevolt.GetData", level.read_s), ("Indevolt.SetData", level.write_s)):
        times = [r.at for r in requests if r.endpoint == endpoint]
        if len(times) > 1:
            assert min(_gaps(times)) >= interval


async def _coordinator_like_load(driver, clock: VirtualClock, seconds: float, setpoints=None):
    """Poll read_telemetry every second and push a setpoint every 1.5 s."""
    end = clock.now + seconds
    step = 0
    while clock.now < end:
        await driver.read_telemetry()
        if setpoints is not None and step % 3 == 0:
            await driver.apply_setpoint(setpoints(step))
        step += 1
        await clock.run(0.5)


async def test_requests_stay_paced_under_heavy_core_polling():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    await _coordinator_like_load(driver, clock, 600, setpoints=lambda i: -300 - 50 * (i % 7))
    _assert_paced(device.requests)
    # The core polls ~1200 times; the device sees at most one read and one
    # write per slot.
    assert len(device.requests) <= 2 * 600 / SLOT_S + 10
    assert device.max_in_flight == 1
    await driver.close()


async def test_a_one_second_meter_does_not_raise_the_request_rate():
    # A 1 s P1 meter makes the core cycle every second (its default minimum
    # cycle interval), and under a pulsing load every cycle asks for something
    # new. The device must still see one request per slot, whatever the meter.
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    start = len(device.requests)
    seconds = 1800
    for second in range(seconds):
        await driver.read_telemetry()
        await driver.apply_setpoint(-400 if second % 2 else -900)
        await clock.run(1.0)
    requests = device.requests[start:]
    _assert_paced(requests)
    # At most one read and one write per slot.
    assert len(requests) <= 2 * seconds / SLOT_S + 1
    assert device.max_in_flight == 1
    # A write is pending all the time, yet the control reads keep their share.
    reads = [r for r in requests if r.endpoint == "Indevolt.GetData"]
    assert len(reads) >= len(requests) // (ic.MAX_CONSECUTIVE_WRITES + 1) - 1
    assert driver.control_state is ControlState.OWNED
    await driver.close()


async def test_at_maximum_a_one_second_meter_gets_a_write_every_second():
    # A 1s meter under a pulsing load: the core asks for something new every
    # second. Maximum sends every one of them; the reads keep their 2s rhythm
    # by following a write's reply instead of taking a write's second.
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    maximum = REQUEST_SPEED_SLOT_S["maximum"]
    driver = await connected_driver(clock, device, slot_s=maximum)
    start = len(device.requests)
    seconds = 120
    for second in range(seconds):
        await driver.read_telemetry()
        await driver.apply_setpoint(-400 if second % 2 else -900)
        await clock.run(1.0)
    requests = device.requests[start:]
    _assert_paced(requests, maximum)
    writes = [r.at for r in requests if r.endpoint == "Indevolt.SetData"]
    reads = [r.at for r in requests if r.endpoint == "Indevolt.GetData"]
    assert len(writes) >= seconds - 1
    assert set(_gaps(writes)) == {MAXIMUM_LEVEL.write_s}
    assert max(_gaps(reads)) < MAXIMUM_LEVEL.read_s + MAXIMUM_LEVEL.write_s
    # Reads follow a write within the second, below Indevolt's 1s minimum...
    assert any(0 < r - w < MIN_SPACING_S for r in reads for w in writes)
    # ...but at most one request in flight, and never more than a write per
    # second plus a read per two seconds.
    assert device.max_in_flight == 1
    assert len(requests) <= seconds * 1.5 + 2
    assert driver.control_state is ControlState.OWNED
    await driver.close()


async def test_maximum_steps_back_to_the_one_second_floor_and_returns():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device, slot_s=REQUEST_SPEED_SLOT_S["maximum"])
    assert driver.scheduler.floor_from_reply
    device.latency_s = 1.5  # every read is slow
    assert await clock.run_until(lambda: driver.scheduler.backoff_level >= 1, 30, step=0.1)
    stepped_at = clock.now
    # The first step back is Fast: Indevolt's 1s floor from the previous start.
    assert (driver.scheduler.interval_s, driver.scheduler.floor_s) == (2.0, MIN_SPACING_S)
    assert not driver.scheduler.floor_from_reply
    await clock.run(30)
    after = [r.at for r in device.requests if r.at > stepped_at]
    assert min(_gaps(after)) >= MIN_SPACING_S
    # Recovered: back down the ladder to Maximum, never below it.
    device.latency_s = 0.05
    await clock.run(ic.CLEAN_PERIOD_S * 6 + 60)
    assert driver.scheduler.backoff_level == 0
    assert driver.scheduler.floor_from_reply and driver.scheduler.interval_s == MIN_SLOT_S
    await driver.close()


async def test_a_write_waits_at_most_one_slot():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    for offset in (0.3, 1.7, 2.5, 4.2, 4.95):
        await clock.run(offset)
        issued = clock.now
        before = len(device.writes)
        result = await driver.apply_setpoint(-500 - int(offset * 100))
        assert result.ok
        await clock.run_until(lambda: len(device.writes) > before, SLOT_S + 1, step=0.05)
        sent = device.set_data_requests[-1].at
        assert sent - issued <= SLOT_S + 1e-6
        await clock.run(SLOT_S)
    await driver.close()


async def test_a_write_goes_out_at_the_floor_not_at_the_next_read():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    await clock.run(30)  # idle: reads only, the last write is long ago
    last_read = device.requests[-1]
    assert last_read.endpoint == "Indevolt.GetData"
    await clock.run(0.2)
    issued = clock.now
    await driver.apply_setpoint(-650)
    assert await clock.run_until(lambda: device.requests[-1].endpoint == "Indevolt.SetData", SLOT_S, step=0.05)
    sent = device.requests[-1].at
    # Not the next read slot (SLOT_S after the read), but the floor.
    assert sent == pytest.approx(last_read.at + _floor(SLOT_S))
    assert sent - issued < SLOT_S - 1
    # The read rhythm carries on after the write, at least the floor later.
    await clock.run(2 * SLOT_S)
    _assert_paced(device.requests)
    await driver.close()


def test_each_request_speed_maps_to_its_slot_and_anything_else_to_the_default():
    assert {speed: slot_for_request_speed(speed) for speed in REQUEST_SPEED_SLOT_S} == {
        "maximum": 1.0, "fast": 2.0, "normal": 3.0, "conservative": 5.0}
    assert SLOT_S == REQUEST_SPEED_SLOT_S[ic.DEFAULT_REQUEST_SPEED] == 5.0
    for unknown in (None, "", "warp", "very_fast", 2, ["fast"]):
        assert slot_for_request_speed(unknown) == SLOT_S
    assert min(REQUEST_SPEED_SLOT_S.values()) >= MIN_SLOT_S


def test_the_floors_follow_indevolt_and_the_ladder_ends_at_ten_seconds():
    # 1s is Indevolt's documented minimum between requests; Conservative keeps 2s.
    # Only Maximum goes below it, with its floor running from the previous reply.
    assert {speed: (_floor(slot), pacing_levels(slot)[0].from_reply)
            for speed, slot in REQUEST_SPEED_SLOT_S.items()} == {
        "maximum": (MIN_REPLY_GAP_S, True), "fast": (1.0, False), "normal": (1.0, False),
        "conservative": (2.0, False)}
    assert MIN_SPACING_S == 1.0
    assert [level.read_s for level in PACING_LADDER] == [2.0, 3.0, 5.0, 10.0]
    assert all(level.write_s == level.read_s and not level.from_reply for level in PACING_LADDER)
    assert pacing_levels(3.0) == [PacingLevel(3.0, 3.0, 1.0), PacingLevel(5.0, 5.0, 2.0),
                                  PacingLevel(10.0, 10.0, 2.0)]
    assert pacing_levels(10.0) == [PacingLevel(10.0, 10.0, 2.0)]
    # A slot between two rungs takes the next rung's floor.
    assert pacing_levels(4.0) == [PacingLevel(4.0, 4.0, 2.0), PacingLevel(5.0, 5.0, 2.0),
                                  PacingLevel(10.0, 10.0, 2.0)]


def test_maximum_writes_every_second_reads_at_two_and_steps_back_to_fast():
    assert MIN_SLOT_S == REQUEST_SPEED_SLOT_S["maximum"] == MAXIMUM_LEVEL.write_s == 1.0
    assert (MAXIMUM_LEVEL.read_s, MAXIMUM_LEVEL.floor_s, MAXIMUM_LEVEL.from_reply) == (
        2.0, MIN_REPLY_GAP_S, True)
    # Its first step back is Fast, with Indevolt's 1s floor again.
    assert pacing_levels(1.0) == [MAXIMUM_LEVEL, *PACING_LADDER]
    assert pacing_levels(1.0)[1] == pacing_levels(REQUEST_SPEED_SLOT_S["fast"])[0]


@pytest.mark.parametrize("slot_s", sorted(REQUEST_SPEED_SLOT_S.values()))
async def test_every_request_speed_keeps_the_pacing_contract(slot_s):
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device, slot_s=slot_s)
    level = pacing_levels(slot_s)[0]
    assert driver.capabilities.actuator_latency_s == level.write_s + RAMP_S
    assert driver.capabilities.readback_latency_s == level.write_s + level.read_s + RAMP_S
    await _coordinator_like_load(driver, clock, 300, setpoints=lambda i: -300 - 50 * (i % 7))
    requests = device.requests
    _assert_paced(requests, slot_s)
    for endpoint in ("Indevolt.GetData", "Indevolt.SetData"):
        assert len([r for r in requests if r.endpoint == endpoint]) > 1
    assert device.max_in_flight == 1
    # Without setpoint changes the reads keep exactly the chosen rhythm.
    quiet_from = clock.now + level.read_s
    await _coordinator_like_load(driver, clock, 120)
    reads = [r.at for r in device.requests if r.endpoint == "Indevolt.GetData" and r.at >= quiet_from]
    assert set(_gaps(reads)) == {level.read_s}
    speed = next(k for k, v in REQUEST_SPEED_SLOT_S.items() if v == slot_s)
    assert (await driver.read_telemetry())["request_speed"] == speed
    await driver.close()


@pytest.mark.parametrize("slot_s", sorted(REQUEST_SPEED_SLOT_S.values()))
async def test_every_request_speed_backs_off_and_names_the_slower_speed(slot_s):
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device, slot_s=slot_s)
    device.timeouts = 10_000
    await clock.run(600)
    await driver.read_telemetry()
    # Up the ladder to its last rung, never beyond.
    assert driver.scheduler.interval_s == PACING_LADDER[-1].write_s
    assert driver._notifier.count("throttled") == 1
    # Only a speed faster than Normal points the user at Normal.
    assert driver._notifier.suggest_slower == [slot_s < REQUEST_SPEED_SLOT_S["conservative"]]
    await driver.close()


async def test_a_slot_below_the_fastest_speed_is_raised_to_maximum():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    assert IndevoltClient("192.0.2.2", slot_s=0.5).slot_s == MIN_SLOT_S
    assert IndevoltClient("192.0.2.2", slot_s=float("nan")).slot_s == SLOT_S
    client = IndevoltClient("192.0.2.2", slot_s=0.1, transport=device, clock=clock, sleep=clock.sleep)
    scheduler = IndevoltScheduler(client, [CONTROL_TIER], clock=clock)
    assert scheduler.interval_s == MIN_SLOT_S and scheduler.read_interval_s == MAXIMUM_LEVEL.read_s
    scheduler.start()
    await clock.run(60)
    await scheduler.close()
    gaps = _gaps(device.request_times())
    assert gaps and set(gaps) == {MAXIMUM_LEVEL.read_s}  # reads only, at Maximum's pace


async def test_an_exchange_spacing_never_undercuts_the_floor_and_others_wait_a_slot():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    client = IndevoltClient("192.0.2.3", slot_s=3, transport=device, clock=clock, sleep=clock.sleep)

    async def calls():
        for _ in range(2):
            await client.exchange(lambda: ("Indevolt.GetData", {"t": [6002]}), interval_s=0.01)
        # A request that names no spacing (probe, config channel) waits a slot.
        await client.get_data([6002])

    task = asyncio.ensure_future(calls())
    await clock.run(20)
    await task
    # A spacing below the hard floor is raised to it.
    assert _gaps(device.request_times()) == [MIN_SPACING_S, 3.0]


async def test_a_gap_from_the_reply_never_undercuts_its_floor_and_others_wait_a_slot():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    client = IndevoltClient("192.0.2.6", slot_s=3, transport=device, clock=clock, sleep=clock.sleep)

    async def calls():
        for _ in range(2):
            await client.exchange(lambda: ("Indevolt.GetData", {"t": [6002]}),
                                  interval_s=0.01, from_reply=True)
        # Nothing sent: the next request is still spaced from the last reply.
        assert await client.exchange(lambda: None, interval_s=0.01, from_reply=True) is None
        await client.exchange(lambda: ("Indevolt.GetData", {"t": [6002]}),
                              interval_s=0.01, from_reply=True)
        # A request that names no spacing (probe, config channel) still waits
        # a full slot from the previous start.
        await client.get_data([6002])

    task = asyncio.ensure_future(calls())
    await clock.run(20)
    await task
    reply_gap = round(device.latency_s + MIN_REPLY_GAP_S, 6)
    assert _gaps(device.request_times()) == [reply_gap, reply_gap, 3.0]


async def test_a_pick_that_sends_nothing_does_not_count_as_a_request():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    client = IndevoltClient("192.0.2.4", slot_s=3, transport=device, clock=clock, sleep=clock.sleep)

    async def calls():
        await client.exchange(lambda: ("Indevolt.GetData", {"t": [6002]}))
        await clock.sleep(2.5)
        assert await client.exchange(lambda: None, interval_s=MIN_SLOT_S) is None
        await client.exchange(lambda: ("Indevolt.GetData", {"t": [6002]}), interval_s=MIN_SLOT_S)

    task = asyncio.ensure_future(calls())
    await clock.run(20)
    await task
    # Spaced from the first real request (its reply took latency_s), not from
    # the empty pick, which would have pushed it to 2.5 + floor.
    assert _gaps(device.request_times()) == [pytest.approx(device.latency_s + 2.5)]


async def test_no_request_starts_while_one_is_in_flight():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, latency_s=7.0)  # slower than a slot
    driver = await connected_driver(clock, device, own=False)
    await clock.run(120)
    assert device.max_in_flight == 1
    starts = device.request_times()
    # Each start comes after the previous reply (7 s), never overlapping.
    assert min(_gaps(starts)) >= 7.0
    await driver.close()


async def test_spacing_survives_reload_and_a_concurrent_probe():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    first = await connected_driver(clock, device)
    await clock.run(12.5)
    # A config-flow probe while the driver runs shares the same gate.
    probe = asyncio.ensure_future(
        type(first).probe("192.0.2.1", transport=device, clock=clock, sleep=clock.sleep)
    )
    await clock.run(1)
    # Reload: the old driver closes mid-slot, a new one starts at once.
    await first.close()
    second = await connected_driver(clock, device)
    await clock.run_until(probe.done, 30)
    assert probe.result() is not None
    await clock.run(30)
    assert device.max_in_flight == 1
    assert min(_gaps(device.request_times())) >= _floor(SLOT_S)
    await second.close()


async def test_newest_setpoint_wins_and_identical_writes_are_dropped():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    await clock.run(SLOT_S - 0.5)  # just after a read slot
    start = len(device.writes)
    for power in (-400, -500, -600):
        await driver.apply_setpoint(power)
    await clock.run(SLOT_S + 0.5)
    assert device.writes[start:] == [(47015, [2, 600, 55])]
    # The same setpoint again: same tuple, already accepted, so nothing is sent.
    for _ in range(5):
        await driver.apply_setpoint(-600)
        await clock.run(SLOT_S)
    assert device.writes[start:] == [(47015, [2, 600, 55])]
    # A change back to the accepted tuple cancels a pending different one.
    await driver.apply_setpoint(-700)
    await driver.apply_setpoint(-600)
    await clock.run(2 * SLOT_S)
    assert device.writes[start:] == [(47015, [2, 600, 55])]
    await driver.close()


async def test_writes_cannot_starve_the_control_reads():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    power = 300
    for _ in range(40):  # a new setpoint every slot
        power += 37
        await driver.apply_setpoint(-power)
        await clock.run(SLOT_S)
    reads = [r for r in device.requests[-40:] if r.endpoint == "Indevolt.GetData"]
    assert len(reads) >= 40 // (ic.MAX_CONSECUTIVE_WRITES + 1) - 1
    assert driver.control_state is ControlState.OWNED
    await driver.close()


async def test_incomplete_batches_fall_back_and_recover():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, batch_limit=2)
    driver = await connected_driver(clock, device, own=False)
    await clock.run(60)
    scheduler = driver.scheduler
    assert scheduler.max_batch <= 2
    # Control keeps working on two-point batches.
    assert await clock.run_until(lambda: driver.control_state is ControlState.OWNED, 120)
    snapshot = await driver.read_telemetry()
    assert {"battery_soc", "battery_power", "ac_power", "energy_mode"} <= set(snapshot)
    # Slower tiers still get their turn.
    assert "total_charging_energy" in snapshot or await clock.run_until(
        lambda: scheduler.value(6006) is not None, 300
    )
    # The device is fixed (e.g. firmware update): batches grow back.
    device.batch_limit = None
    await clock.run(ic.BATCH_RECOVER_AFTER * SLOT_S * 8)
    assert scheduler.max_batch == ic.MAX_POINTS
    await driver.close()


async def test_a_point_the_device_never_reports_is_skipped():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, missing_points={9068})
    client = IndevoltClient("192.0.2.4", transport=device, clock=clock, sleep=clock.sleep)
    scheduler = IndevoltScheduler(client, [Tier("control", (6002,)), Tier("slow", (9012, 9068), 30)],
                                  clock=clock)
    scheduler.start()
    await clock.run(600)
    await scheduler.close()
    assert 9068 in scheduler.stats()["unsupported_points"]
    # Not a truncated batch, so the batch size stays put.
    assert scheduler.max_batch == ic.MAX_POINTS
    recent = device.requests[-20:]
    assert all(9068 not in (r.config or {}).get("t", []) for r in recent)
    assert scheduler.value(6002) is not None


async def test_one_isolated_timeout_does_not_pause_control():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(-400)
    await clock.run(30)
    device.timeouts = 1
    for _ in range(60):
        await clock.run(1)
        await driver.read_telemetry()
        assert driver.control_state is ControlState.OWNED
        # One bad reply is jitter: the pace does not change.
        assert driver.scheduler.backoff_level == 0
    await driver.close()


async def test_persistent_failures_back_off_and_notify():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    notifier = driver._notifier
    device.timeouts = 10_000
    await clock.run(600)
    await driver.read_telemetry()
    assert driver.scheduler.backoff_level == len(pacing_levels(SLOT_S)) - 1
    gaps = _gaps(device.request_times()[-5:])
    assert min(gaps) >= PACING_LADDER[-1].read_s
    assert notifier.count("throttled") == 1
    assert driver.control_state is ControlState.PAUSED
    assert driver.paused_reason == "telemetry_stale"
    # Recovery: requests speed up again, the notification clears, and control
    # comes back through a fresh STOP handover.
    device.timeouts = 0
    writes_before = len(device.writes)
    assert await clock.run_until(lambda: driver.control_state is ControlState.OWNED, 300)
    await clock.run(600)
    await driver.read_telemetry()
    assert notifier.count("throttle_cleared") == 1
    assert device.writes[writes_before][1] == [0, 0, 0]
    await driver.close()


async def test_a_closed_client_stops_the_scheduler_instead_of_spinning():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    client = IndevoltClient("192.0.2.5", transport=device, clock=clock, sleep=clock.sleep)
    scheduler = IndevoltScheduler(client, [CONTROL_TIER], clock=clock)
    scheduler.start()
    await clock.run(12)
    await client.close()
    await clock.run(30)
    assert scheduler._task is None or scheduler._task.done()
    await scheduler.close()


def _scheduler_at(slot_s: float = SLOT_S):
    clock = VirtualClock()
    client = IndevoltClient("192.0.2.9", slot_s=slot_s, transport=FakeIndevoltDevice(clock),
                            clock=clock, sleep=clock.sleep)
    return clock, IndevoltScheduler(client, [CONTROL_TIER], clock=clock)


def test_two_bad_replies_step_up_one_rung_at_most_every_ten_seconds():
    clock, scheduler = _scheduler_at(2.0)
    scheduler._record_health(False)
    assert scheduler.interval_s == 2.0  # one bad reply is jitter
    clock.now += 5
    scheduler._record_health(False)
    assert (scheduler.interval_s, scheduler.floor_s) == (3.0, 1.0)
    # More trouble right away: no second step within ic.STEP_UP_SPACING_S.
    for _ in range(4):
        clock.now += 1
        scheduler._record_health(False)
    assert scheduler.interval_s == 3.0
    clock.now += ic.STEP_UP_SPACING_S
    scheduler._record_health(False)
    assert (scheduler.interval_s, scheduler.floor_s) == (5.0, 2.0)
    # Bad replies further apart than the window never add up.
    clock2, slow = _scheduler_at(2.0)
    for _ in range(5):
        slow._record_health(False)
        clock2.now += ic.TROUBLE_WINDOW_S + 1
    assert slow.backoff_level == 0


def test_after_a_clean_period_one_rung_down_never_below_the_users_speed():
    clock, scheduler = _scheduler_at(3.0)
    for _ in range(3):  # up to the last rung: 3 -> 5 -> 10
        scheduler._record_health(False)
        scheduler._record_health(False)
        clock.now += ic.STEP_UP_SPACING_S
    assert scheduler.interval_s == 10.0
    assert scheduler.throttled_for_s > 0
    seen = []
    for _ in range(200):
        clock.now += 1
        scheduler._record_health(True)
        seen.append(scheduler.interval_s)
    # 10 -> 5 -> 3, each after ic.CLEAN_PERIOD_S without a bad reply, then stays.
    assert seen.index(5.0) + 1 >= ic.CLEAN_PERIOD_S - ic.STEP_UP_SPACING_S
    assert seen.index(3.0) - seen.index(5.0) == ic.CLEAN_PERIOD_S
    assert set(seen[seen.index(3.0):]) == {3.0}
    assert scheduler.backoff_level == 0 and scheduler.throttled_for_s == 0.0


def test_a_slow_read_counts_as_bad_but_an_equally_slow_write_does_not():
    clock, scheduler = _scheduler_at(2.0)
    client = scheduler._client
    for payload in ((2, 500, 55), (2, 600, 55)):
        scheduler._current = ("write", payload)
        client.last_latency_s = 1.5  # slower than a read may be, fine for a write
        scheduler._after_success(("Indevolt.SetData", {}), {"result": True})
    assert scheduler.backoff_level == 0
    for _ in range(2):
        scheduler._current = ("read", [6002])
        client.last_latency_s = ic.READ_SLOW_S + 0.1
        scheduler._after_success(("Indevolt.GetData", {}), {"6002": 50})
    assert scheduler.backoff_level == 1
    clock2, writes = _scheduler_at(2.0)
    for power in (500, 600):
        writes._current = ("write", (2, power, 55))
        writes._client.last_latency_s = ic.WRITE_SLOW_S + 0.1
        writes._after_success(("Indevolt.SetData", {}), {"result": True})
    assert writes.backoff_level == 1


async def test_a_slow_battery_climbs_the_ladder_and_comes_back():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device, slot_s=2.0)
    device.latency_s = 1.5  # every read is slow
    await clock.run(60)
    assert driver.scheduler.interval_s == 10.0
    device.latency_s = 0.05
    await clock.run(ic.CLEAN_PERIOD_S * 3 + 3 * 10.0)
    assert driver.scheduler.interval_s == 2.0
    assert driver.control_state is ControlState.OWNED
    await driver.close()
