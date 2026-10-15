"""Behaviour of the Indevolt driver against the fake device.

Covers the control state machine (auto-acquire, mode loss and resume, restart
with STOP first, unknown models), the rolling target SOC under steady loads, the
once-per-episode standby re-send and the cache-only telemetry path.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.omnibattery.drivers.base import DELIVERED_AC_POWER_KEY
from custom_components.omnibattery.drivers.indevolt import (
    ControlState,
    IndevoltDriver,
    firmware_below,
)
from custom_components.omnibattery.infra.indevolt_client import SLOT_S
from tests.indevolt_fake import (
    FakeIndevoltDevice,
    RecordingNotifier,
    VirtualClock,
    connected_driver,
)


async def _hold(clock: VirtualClock, driver, seconds: float, *, poll_s: float = 2.0):
    """Let time pass while the coordinator polls; the core sends no setpoints."""
    end = clock.now + seconds
    while clock.now < end:
        await driver.read_telemetry()
        await clock.run(poll_s)


# --- monitoring and acquisition ------------------------------------------------------


async def test_monitoring_serves_decoded_telemetry_from_cache():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1)
    driver = await connected_driver(clock, device, own=False)
    await clock.run(40)
    before = len(device.requests)
    for _ in range(50):
        snapshot = await driver.read_telemetry()
    assert len(device.requests) == before  # cache only
    assert snapshot["battery_soc"] == 60
    assert snapshot["energy_mode"] == 1
    assert snapshot["max_charge_power"] == 2400
    assert snapshot["discharging_cutoff_capacity"] == 15
    assert snapshot["battery_total_energy"] == 7.18
    assert snapshot["control_status"] == "realtime_mode_required"
    assert set(await driver.read_telemetry(["battery_soc", "nope"])) == {"battery_soc"}
    assert device.set_data_requests == []
    await driver.close()


async def test_sign_conventions_live_in_one_place():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, loss_w=50)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(-800)  # discharge
    await clock.run(4 * SLOT_S)
    data = await driver.read_telemetry()
    assert data["battery_power"] == pytest.approx(-800, abs=1)  # + charge / - discharge
    assert data[DELIVERED_AC_POWER_KEY] == pytest.approx(-850, abs=1)
    assert data["ac_power"] == pytest.approx(850, abs=1)  # core: + discharging
    assert data["inverter_state"] == 3
    assert driver.net_power_from_data(data) == -800
    await driver.close()


async def test_auto_acquire_sends_stop_first_and_confirms_standby():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    assert device.writes == [(47015, [0, 0, 0])]
    assert driver.control_status == "active"
    assert driver.net_power_from_data({}) == 0
    assert driver._notifier.count("resumed") == 1
    await driver.close()


async def test_setpoints_are_refused_until_control_is_owned():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1)
    driver = await connected_driver(clock, device, own=False)
    result = await driver.apply_setpoint(-500)
    assert not result.ok and result.failure_reason == "realtime_mode_required"
    # 0 W is accepted without a write: nothing of Omnibattery's runs on the battery.
    idle = await driver.apply_setpoint(0)
    assert idle.ok and idle.net_power_w == 0
    assert driver.net_power_from_data({}) is None
    assert driver.dynamic_discharge_limit_w({}) == 0
    await clock.run(60)
    assert device.set_data_requests == []
    await driver.close()


async def test_restart_with_a_retained_command_stops_before_anything_else():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, action=2, rt_power=1500, rt_target=20, power_w=-1500)
    driver = await connected_driver(clock, device, own=False)
    assert await clock.run_until(lambda: driver.control_state is ControlState.OWNED, 60)
    assert device.writes[0] == (47015, [0, 0, 0])
    assert device.power_w == pytest.approx(0, abs=1)
    await driver.close()


async def test_handover_fails_when_the_device_keeps_running_and_retries_later():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, ramp_w_per_s=0.0, power_w=-900)  # never ramps down
    notifier = RecordingNotifier()
    driver = await connected_driver(clock, device, own=False, notifier=notifier)
    assert await clock.run_until(lambda: driver.paused_reason == "handover_failed", 120)
    assert notifier.count("paused", "handover_failed") == 1
    stops = len(device.writes)
    await clock.run(55)
    assert len(device.writes) == stops  # backoff: no STOP storm
    device.ramp_w_per_s = 1000.0
    assert await clock.run_until(lambda: driver.control_state is ControlState.OWNED, 180)
    assert notifier.count("resumed") == 1
    await driver.close()


# --- mode loss and resume ------------------------------------------------------------


async def test_mode_loss_pauses_with_notification_and_resume_is_automatic():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    notifier = RecordingNotifier()
    driver = await connected_driver(clock, device, notifier=notifier)
    await driver.apply_setpoint(-600)
    await clock.run(3 * SLOT_S)
    device.set_mode(1)  # user switches to self-consumption in the app
    assert await clock.run_until(lambda: driver.paused_reason == "realtime_mode_required", 2 * SLOT_S)
    assert notifier.count("paused", "realtime_mode_required") == 1
    assert (await driver.read_telemetry())["control_status"] == "realtime_mode_required"
    writes = len(device.writes)
    for _ in range(10):
        assert not (await driver.apply_setpoint(-600)).ok
        await clock.run(SLOT_S)
    assert len(device.writes) == writes  # nothing written while paused
    device.set_mode(4)  # and back to Realtime
    assert await clock.run_until(lambda: driver.control_state is ControlState.OWNED, 60)
    assert device.writes[writes] == (47015, [0, 0, 0])  # STOP first, again
    assert notifier.events[-1] == ("resumed",)
    assert (await driver.apply_setpoint(-600)).ok
    await driver.close()


async def test_unknown_model_is_monitored_but_never_controlled():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, device_type="CMS-XY9000")
    notifier = RecordingNotifier()
    driver = await connected_driver(clock, device, own=False, notifier=notifier)
    await _hold(clock, driver, 120)
    assert driver.paused_reason == "unknown_model"
    assert notifier.count("paused", "unknown_model") == 1
    assert (await driver.read_telemetry())["battery_soc"] == 60
    assert not (await driver.apply_setpoint(500)).ok
    assert device.set_data_requests == []
    await driver.close()


async def test_control_can_be_disabled_for_monitoring_only():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device, own=False, control_enabled=False)
    await _hold(clock, driver, 60)
    assert driver.control_state is ControlState.MONITORING
    assert (await driver.read_telemetry())["control_status"] == "not_configured"
    assert device.set_data_requests == []
    await driver.close()


@pytest.mark.parametrize("family", [0, 1000])
async def test_both_realtime_status_families(family):
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, status_family=family)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(700)
    await _hold(clock, driver, 60)
    assert device.effective_action() == 1
    assert driver._echo()[0] == 1
    await driver.close()


# --- rolling target SOC --------------------------------------------------------------


def _soc_span_seconds(power_w: float, span_pct: float, capacity_kwh: float = 7.18) -> float:
    return span_pct / 100 * capacity_kwh * 1000 / power_w * 3600


@pytest.mark.parametrize("power", [400, 800])
async def test_steady_discharge_never_stops_without_new_core_setpoints(power):
    """A heat pump at night: P1 stays in the deadband, the core sends nothing new."""
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=62.0)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(-power)
    await _hold(clock, driver, _soc_span_seconds(power, 21.5), poll_s=5.0)
    assert device.soc <= 41.0  # crossed 20% of SOC
    assert device.standby_events == []  # never reached a target
    assert device.power_w == pytest.approx(-power, abs=1)
    targets = [v[2] for t, v in device.writes if v[0] == 2]
    assert targets == sorted(targets, reverse=True) and len(targets) >= 7
    # Every refresh landed before the battery reached the old target.
    assert all(target <= 57 for target in targets)
    await driver.close()


async def test_steady_charge_never_stops_without_new_core_setpoints():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=30.0)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(800)
    await _hold(clock, driver, _soc_span_seconds(800, 21.5), poll_s=5.0)
    assert device.soc >= 50.5
    assert device.standby_events == []
    assert device.power_w == pytest.approx(800, abs=1)
    await driver.close()


async def test_charge_target_saturates_at_100_and_discharge_target_at_the_floor():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=97.0)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(2000)
    await clock.run(2 * SLOT_S)
    assert device.writes[-1] == (47015, [1, 2000, 100])
    device.jump_soc(17.0)
    await clock.run(2 * SLOT_S)  # the driver sees the new SOC
    await driver.apply_setpoint(-2000)
    await clock.run(2 * SLOT_S)
    assert device.writes[-1] == (47015, [2, 2000, 15])  # native reserve 6105 = 15
    await _hold(clock, driver, 200)
    assert device.writes[-1] == (47015, [2, 2000, 15])  # no refresh loop at the floor
    await driver.close()


async def test_soc_jump_past_the_target_is_resent_at_once():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=50.0)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(-500)
    await _hold(clock, driver, 30)
    assert device.writes[-1] == (47015, [2, 500, 45])
    device.jump_soc(44.0)  # BMS recalibration: below the target, device stops
    assert device.effective_action() == 0
    jumped = clock.now
    assert await clock.run_until(lambda: device.effective_action() == 2, 3 * SLOT_S, step=0.5)
    assert clock.now - jumped <= 2 * SLOT_S
    assert device.writes[-1][1][2] == 39
    await driver.close()


async def test_standby_echo_gets_one_resend_per_episode():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=70.0)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(-600)
    await _hold(clock, driver, 60)
    writes = len(device.writes)
    # The device drops to standby for its own reasons (BMS protection) and
    # stays there: one re-send, then the core's non-delivery handling decides.
    device.action_state = 0
    device.ramp_w_per_s = 10_000

    def stuck_standby():
        device.action_state = 0

    for _ in range(80):
        stuck_standby()
        await driver.read_telemetry()
        await clock.run(SLOT_S)
    assert len(device.writes) == writes + 1
    assert device.writes[-1][1][:2] == [2, 600]
    # A new core setpoint starts a new episode.
    await driver.apply_setpoint(-650)
    await clock.run(2 * SLOT_S)
    assert len(device.writes) == writes + 2
    await driver.close()


async def test_standby_while_the_device_starts_is_not_a_dropout():
    # The real SF2000 echoes standby for 10–15 s after a move commanded from
    # standby. The echo is expedited after every write, so it lands in that
    # window; re-sending there would spend the episode's one re-send on a
    # normal start.
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=70.0, start_delay_s=15.0)
    driver = await connected_driver(clock, device)
    writes = len(device.writes)
    await driver.apply_setpoint(-500)
    await _hold(clock, driver, 90)
    assert len(device.writes) == writes + 1
    assert device.effective_action() == 2
    assert device.power_w == pytest.approx(-500, abs=1)
    await driver.close()


# --- safety, limits and advisories ------------------------------------------------------


async def test_power_is_clamped_to_the_device_limits():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, limit_w=1800)
    # The configured 1200 W is the coordinator's to enforce (it can change at
    # runtime); the driver clamps to what the device reports.
    driver = await connected_driver(clock, device, max_discharge_power_w=1200)
    result = await driver.apply_setpoint(-5000)
    assert result.ok and result.net_power_w == -1800
    result = await driver.apply_setpoint(5000)
    assert result.net_power_w == 1800
    await clock.run(2 * SLOT_S)
    assert device.writes[-1][1][:2] == [1, 1800]
    data = await driver.read_telemetry()
    assert data["max_charge_power"] == data["max_discharge_power"] == 1800
    await driver.close()


async def test_configured_limits_apply_until_the_device_reports_its_own():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, missing_points={11009, 114, 11011, 11010, 11032, 115})
    driver = await connected_driver(clock, device, max_charge_power_w=1500, max_discharge_power_w=1200)
    assert (await driver.apply_setpoint(-5000)).net_power_w == -1200
    assert (await driver.apply_setpoint(5000)).net_power_w == 1500
    assert "max_charge_power" not in await driver.read_telemetry()
    await driver.close()


async def test_lost_telemetry_pauses_and_recovers_through_stop():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(-700)
    await clock.run(20)
    device.timeouts = 12
    assert await clock.run_until(lambda: driver.paused_reason == "telemetry_stale", 300)
    assert not (await driver.apply_setpoint(-700)).ok
    assert await clock.run_until(lambda: driver.control_state is ControlState.OWNED, 600)
    assert device.writes[-1] == (47015, [0, 0, 0])
    await driver.close()


async def test_firmware_advisory_is_raised_once():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, f_ver="V1405.07.002E")
    notifier = RecordingNotifier()
    driver = await connected_driver(clock, device, notifier=notifier)
    await _hold(clock, driver, 60)
    assert notifier.count("firmware") == 1
    await driver.close()


def test_firmware_comparison():
    assert not firmware_below("V140E.0B.003E", "V1406.07.002E")  # a CMS-SF2000 on firmware V1.4.0E
    assert firmware_below("V1405.07.002E", "V1406.07.002E")
    assert not firmware_below("garbage", "V1406.07.002E")
    assert not firmware_below(None, "V1406.07.002E")


async def test_standby_on_unload_waits_for_the_stop():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(-900)
    await clock.run(3 * SLOT_S)
    task = asyncio.ensure_future(driver.standby())
    await clock.run_until(task.done, 3 * SLOT_S)
    assert task.result() is True
    assert device.writes[-1] == (47015, [0, 0, 0])
    await driver.close()


async def test_reconnect_adds_no_device_traffic():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    await clock.run(SLOT_S / 2)
    before = len(device.requests)
    for _ in range(20):
        assert await driver.connect()
    assert len(device.requests) == before
    await driver.close()


async def test_charge_discharge_reversal_follows_the_core():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=50.0)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(-600)
    await _hold(clock, driver, 20)
    assert device.power_w == pytest.approx(-600, abs=1)
    await driver.apply_setpoint(900)
    await _hold(clock, driver, 20)
    assert device.writes[-1] == (47015, [1, 900, 55])
    assert device.power_w == pytest.approx(900, abs=1)
    assert driver.net_power_from_data({}) == 900
    await driver.apply_setpoint(0)
    await _hold(clock, driver, 20)
    assert device.writes[-1] == (47015, [0, 0, 0])
    assert device.power_w == pytest.approx(0, abs=1)
    await driver.close()


@pytest.mark.parametrize("style", ["missing", "empty"])
@pytest.mark.parametrize("packs", [0, 3, 5])
async def test_temperatures_follow_the_packs_that_are_present(packs, style):
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, packs=packs, absent_pack_style=style)
    driver = await connected_driver(clock, device, own=False)
    data = await driver.read_telemetry()
    pack_keys = sorted(k for k in data if k.startswith("expansion_battery_"))
    assert pack_keys == [f"expansion_battery_{n}_temperature" for n in range(1, packs + 1)]
    assert [d["key"] for d in driver.sensor_definitions if d["key"].startswith("expansion_battery_")] == pack_keys
    assert any(set(pack_keys) <= set(g.keys) for g in driver.read_groups) or not pack_keys
    # The thermal derate uses the warmest battery, never the transformer (45 °C).
    assert data["inverter_battery_temperature"] == 41
    assert data["internal_temperature"] == max([41] + [30 + n for n in range(1, packs + 1)])
    assert data["transformer_temperature"] == 45
    await driver.close()


async def test_absent_pack_points_do_not_hold_up_control_reads():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, packs=1)  # four packs' points are missing
    driver = await connected_driver(clock, device)
    await clock.run(360)
    reads = [r for r in device.requests if r.endpoint == "Indevolt.GetData"]
    singles = [r for r in reads if len(r.config["t"]) == 1]
    assert len(singles) == 8  # each missing point verified once, then skipped
    gaps = [b.at - a.at for a, b in zip(singles, singles[1:])]
    assert min(gaps) >= 30  # verify reads are spread out
    assert driver.scheduler.max_batch == 50
    await driver.close()
