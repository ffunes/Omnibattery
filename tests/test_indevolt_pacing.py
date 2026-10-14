"""Pacing, write priority, coalescing and batch fallback of the Indevolt scheduler.

All on a virtual clock against the fake device: spacing is exact, so the
assertions use the real constants without tolerance fudging.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.omnibattery.drivers.indevolt import CONTROL_TIER, ControlState
from custom_components.omnibattery.infra import indevolt_client as ic
from custom_components.omnibattery.infra.indevolt_client import (
    MIN_SLOT_S,
    SLOT_S,
    IndevoltClient,
    IndevoltScheduler,
    Tier,
)
from tests.indevolt_fake import FakeIndevoltDevice, VirtualClock, connected_driver


def _gaps(times: list[float]) -> list[float]:
    return [round(b - a, 6) for a, b in zip(times, times[1:])]


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


async def test_requests_never_closer_than_one_slot_under_heavy_core_polling():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    await _coordinator_like_load(driver, clock, 600, setpoints=lambda i: -300 - 50 * (i % 7))
    gaps = _gaps(device.request_times())
    assert min(gaps) >= SLOT_S
    # The core polled ~1200 times; the device saw one request per slot.
    assert len(device.requests) <= 600 / SLOT_S + 10
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
    gaps = _gaps([r.at for r in requests])
    assert min(gaps) >= SLOT_S
    assert len(requests) <= seconds / SLOT_S + 1
    assert device.max_in_flight == 1
    # A write is pending all the time, yet the control reads keep their share.
    reads = [r for r in requests if r.endpoint == "Indevolt.GetData"]
    assert len(reads) >= len(requests) // (ic.MAX_CONSECUTIVE_WRITES + 1) - 1
    assert driver.control_state is ControlState.OWNED
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


async def test_a_slot_below_the_floor_is_raised_to_two_seconds():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    assert IndevoltClient("192.0.2.2", slot_s=0.5).slot_s == MIN_SLOT_S
    assert IndevoltClient("192.0.2.2", slot_s=float("nan")).slot_s == SLOT_S
    client = IndevoltClient("192.0.2.2", slot_s=0.1, transport=device, clock=clock, sleep=clock.sleep)
    scheduler = IndevoltScheduler(client, [CONTROL_TIER], clock=clock)
    scheduler.start()
    await clock.run(60)
    await scheduler.close()
    gaps = _gaps(device.request_times())
    assert gaps and min(gaps) == MIN_SLOT_S


async def test_backoff_never_shortens_and_the_floor_holds_under_exchange_interval():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    client = IndevoltClient("192.0.2.3", slot_s=3, transport=device, clock=clock, sleep=clock.sleep)

    async def two():
        for _ in range(2):
            await client.exchange(lambda: ("Indevolt.GetData", {"t": [6002]}), interval_s=0.01)

    task = asyncio.ensure_future(two())
    await clock.run(10)
    await task
    assert _gaps(device.request_times()) == [3.0]


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
    assert min(_gaps(device.request_times())) >= SLOT_S
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
    assert driver.scheduler.backoff_factor >= 1
    await clock.run(ic.HEALTHY_STREAK * SLOT_S * 3)
    assert driver.scheduler.backoff_factor == 1
    await driver.close()


async def test_persistent_failures_back_off_and_notify():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    notifier = driver._notifier
    device.timeouts = 10_000
    await clock.run(600)
    await driver.read_telemetry()
    assert driver.scheduler.backoff_factor == ic.MAX_BACKOFF_FACTOR
    gaps = _gaps(device.request_times()[-5:])
    assert min(gaps) >= SLOT_S * ic.MAX_BACKOFF_FACTOR
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
