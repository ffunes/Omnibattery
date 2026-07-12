"""Tests for the bus-load skip-if-unchanged guard in ``_set_battery_power``.

The control loop runs every ~2 s. When the battery is already in the commanded
state, re-writing force_mode + charge/discharge power (and reading 4 registers
back) every cycle is pure bus traffic. The guard skips that redundant write.

Crucially it must NOT skip when a discharge command is no longer being delivered
(the v3 non-responsive failure mode), otherwise the non-responsive tracker would
never see the battery stop. These tests pin both behaviours.

The method is exercised unbound with light stubs for ``self`` and the
coordinator, so no full ChargeDischargeController has to be constructed.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import (
    IDLE_RUNAWAY_GRACE_S,
    PD_READBACK_EVERY_N_WRITES,
)
from custom_components.omnibattery.drivers import SetpointResult
from tests.conftest import FakeCoordinator


def _Coord(data):
    """Identity-hashable, slot-guarded coordinator stand-in."""
    return FakeCoordinator(
        name="BAT1",
        is_available=True,
        rs485_user_disabled=False,
        balance_hold=False,
        min_soc=10,
        data=data,
        apply_power=AsyncMock(),
    )


class _SlowCoord(FakeCoordinator):
    """Zendure-like slow actuator: per-write readback is skipped, so a stalled
    battery only surfaces at the poll-time delivery check, not via ACK."""

    @property
    def capabilities(self):
        return replace(super().capabilities, actuator_latency_s=3.0)


def _SlowCoordFake(data):
    return _SlowCoord(
        name="ZEN1",
        is_available=True,
        rs485_user_disabled=False,
        balance_hold=False,
        min_soc=10,
        data=data,
        apply_power=AsyncMock(),
    )


def _ok(net, *, confirmed=True, battery_power_w=None):
    """A successful confirmed SetpointResult, as coordinator.apply_power returns."""
    return SetpointResult(
        ok=True, net_power_w=net, confirmed=confirmed, battery_power_w=battery_power_w,
    )


def _controller():
    ctrl = SimpleNamespace(
        _is_backup_function_active=lambda c: False,
        _is_manual_slot_owned=lambda c: False,
        get_charge_blockers=lambda c: {},
        get_discharge_blockers=lambda c: {},
        _log_low_power_delivery=lambda coordinator, **k: None,
        _normal_balance_top_voltage_seen={},
        _last_commanded_net_sign={},
        _discharge_engage_started={},
        _idle_commanded_started={},
        _non_responsive=SimpleNamespace(
            record_non_delivery=lambda *a, **k: False,
            clear=lambda c: None,
            set_wake_attempted=lambda *a, **k: None,
        ),
        _idle_runaway_handled={},
    )
    # The non-delivery judgment is a real method on the controller; bind it to
    # the stub so the readback and poll-time paths exercise the real logic.
    ctrl._check_non_delivery = ChargeDischargeController._check_non_delivery.__get__(ctrl)
    return ctrl


async def test_skip_when_idle_unchanged():
    coord = _Coord({"force_mode": 0, "set_charge_power": 0, "set_discharge_power": 0})
    ctrl = _controller()

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 0)

    assert result is True
    coord.apply_power.assert_not_called()


async def test_no_skip_when_idle_but_delivering():
    """Commanded idle but the battery is exporting under its own internal logic
    (a v3 that dropped RS485 forced mode — issue #434): the matching standby
    set-points are not trustworthy. Must fire the wake recovery once per episode
    instead of skipping."""
    coord = _Coord({
        "force_mode": 0,
        "set_charge_power": 0,
        "set_discharge_power": 0,
        "battery_power": -2600,  # exporting on its own while commanded idle
    })
    ctrl = _controller()
    wake = AsyncMock(return_value=True)
    ctrl._attempt_wake = wake

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 0)

    assert result is True
    wake.assert_awaited_once_with(coord)


async def test_no_wake_when_idle_but_charging_from_solar():
    """Commanded idle while the battery reads *positive* power is not a runaway:
    on a DC-coupled vA/vD the battery_power register lumps in the DC PV feeding
    the bus, so a unit resting at idle while absorbing its own solar reads +W.
    Forcing standby there would dump that PV to grid — must skip, not wake."""
    coord = _Coord({
        "force_mode": 0,
        "set_charge_power": 0,
        "set_discharge_power": 0,
        "battery_power": 1213,  # charging from own MPPT while grid is balanced
    })
    ctrl = _controller()
    wake = AsyncMock(return_value=True)
    ctrl._attempt_wake = wake

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 0)

    assert result is True
    wake.assert_not_awaited()
    coord.apply_power.assert_not_called()  # skipped, set-points already at standby


async def test_no_wake_during_idle_rampdown_grace():
    """A fresh discharge→idle flip where the battery is still ramping down is
    NOT a runaway: set-points read standby before battery_power telemetry
    catches up (actuator settle + poll grain). Waking there re-asserts RS485 on
    every ordinary transition — must skip quietly instead."""
    coord = _Coord({
        "force_mode": 0,
        "set_charge_power": 0,
        "set_discharge_power": 0,
        "battery_power": -300,  # still ramping down from the prior discharge
    })
    ctrl = _controller()
    wake = AsyncMock(return_value=True)
    ctrl._attempt_wake = wake
    ctrl._last_commanded_net_sign[coord] = -1  # was discharging -> flips to idle

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 0)

    assert result is True
    wake.assert_not_awaited()
    coord.apply_power.assert_not_called()


async def test_wake_after_idle_rampdown_grace_expires():
    """Still discharging past the ramp-down grace is a genuine runaway: the
    suppression must not outlive IDLE_RUNAWAY_GRACE_S."""
    coord = _Coord({
        "force_mode": 0,
        "set_charge_power": 0,
        "set_discharge_power": 0,
        "battery_power": -300,
    })
    ctrl = _controller()
    wake = AsyncMock(return_value=True)
    ctrl._attempt_wake = wake
    ctrl._last_commanded_net_sign[coord] = 0  # already idle: no fresh flip
    ctrl._idle_commanded_started[coord] = dt_util.utcnow() - timedelta(
        seconds=IDLE_RUNAWAY_GRACE_S + 1
    )

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 0)

    assert result is True
    wake.assert_awaited_once_with(coord)


async def test_idle_runaway_wake_fires_once_per_episode():
    """The wake must fire once per runaway episode, not every cycle — re-asserting
    every ~2 s is what floods the log without recovering the battery."""
    coord = _Coord({
        "force_mode": 0,
        "set_charge_power": 0,
        "set_discharge_power": 0,
        "battery_power": -2600,  # still running free next cycle too
    })
    ctrl = _controller()
    wake = AsyncMock(return_value=True)
    ctrl._attempt_wake = wake

    for _ in range(3):
        await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 0)

    wake.assert_awaited_once_with(coord)  # only the first cycle wakes

    # Battery returns to idle, then runs away again -> a fresh episode re-arms.
    coord.data["battery_power"] = 0
    await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 0)
    coord.data["battery_power"] = -2600
    await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 0)

    assert wake.await_count == 2


async def test_skip_when_discharge_unchanged_and_delivering():
    coord = _Coord({
        "force_mode": 2,
        "set_charge_power": 0,
        "set_discharge_power": 300,
        "battery_power": -300,  # delivering (sign-agnostic via abs())
    })
    ctrl = _controller()

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 300)

    assert result is True
    coord.apply_power.assert_not_called()


async def test_skip_when_charge_unchanged():
    coord = _Coord({"force_mode": 1, "set_charge_power": 500, "set_discharge_power": 0})
    ctrl = _controller()

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 500, 0)

    assert result is True
    coord.apply_power.assert_not_called()


async def test_no_skip_when_discharge_unchanged_but_not_delivering():
    """Set-points match but the battery stopped delivering: must still write so the
    non-responsive tracker keeps counting toward exclusion."""
    coord = _Coord({
        "force_mode": 2,
        "set_charge_power": 0,
        "set_discharge_power": 300,
        "battery_power": 0,  # ACK'd earlier but now delivering nothing
        "battery_soc": 80,   # above BMS cutoff floor -> a real fault, not protection
        "inverter_state": None,
    })
    # ACK'd (confirmed) but delivering 0 W -> non-delivery tracker must fire.
    coord.apply_power = AsyncMock(return_value=_ok(-300, battery_power_w=0))
    ctrl = _controller()
    # Already discharging for a while (v3 silent-stop), so this is steady state,
    # not a fresh charge→discharge flip — past the engage grace window.
    ctrl._last_commanded_net_sign[coord] = -1
    record = MagicMock(return_value=False)  # sync: not awaited in _set_battery_power
    ctrl._non_responsive.record_non_delivery = record

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 300)

    assert result is True
    coord.apply_power.assert_called_once()
    record.assert_called_once()


async def test_no_record_during_discharge_engage_grace():
    """A fresh charge→discharge flip that has not engaged yet must NOT be recorded
    as non-delivery while within the engage grace window — a slow inverter takes
    seconds to reverse into discharge and 0 W out that soon is engage latency."""
    coord = _Coord({
        "force_mode": 2,
        "set_charge_power": 0,
        "set_discharge_power": 300,
        "battery_power": 0,  # not delivering yet — still engaging
        "battery_soc": 80,
        "inverter_state": None,
    })
    coord.apply_power = AsyncMock(return_value=_ok(-300, battery_power_w=0))
    ctrl = _controller()
    ctrl._last_commanded_net_sign[coord] = 1  # was charging -> this call flips to discharge
    record = MagicMock(return_value=False)
    ctrl._non_responsive.record_non_delivery = record

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 300)

    assert result is True
    coord.apply_power.assert_called_once()
    record.assert_not_called()  # suppressed: inverter still within engage grace


async def test_no_skip_when_setpoints_differ():
    coord = _Coord({
        "force_mode": 2,
        "set_charge_power": 0,
        "set_discharge_power": 100,  # device at 100W, commanding 300W
        "battery_power": -100,
    })
    coord.apply_power = AsyncMock(return_value=_ok(-300, battery_power_w=-300))
    ctrl = _controller()

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 300)

    assert result is True
    coord.apply_power.assert_called_once()


async def test_readback_throttled_to_every_n_writes():
    """Only every Nth real write reads back; the rest are write-only.

    The battery is in-state but not delivering, so option B never skips and every
    call reaches the write path (counter advances each time)."""
    coord = _Coord({
        "force_mode": 2,
        "set_charge_power": 0,
        "set_discharge_power": 300,
        "battery_power": 0,    # not delivering -> option B won't skip
        "battery_soc": 80,
        "inverter_state": None,
    })
    seen_read_back: list[bool] = []

    async def fake_apply(net, read_back=True):
        seen_read_back.append(read_back)
        # Write-only cycles return unconfirmed (no readback); verify cycles
        # confirm and carry delivered power (0 W -> non-delivery still fires).
        return SetpointResult(
            ok=True, net_power_w=net, confirmed=read_back,
            battery_power_w=0 if read_back else None,
        )

    coord.apply_power = fake_apply
    ctrl = _controller()
    ctrl._non_responsive.record_non_delivery = MagicMock(return_value=False)

    for _ in range(PD_READBACK_EVERY_N_WRITES + 1):
        await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 300)

    # First write verifies; the next N-1 are write-only; the Nth verifies again.
    assert seen_read_back[0] is True
    assert seen_read_back[1:PD_READBACK_EVERY_N_WRITES] == [False] * (
        PD_READBACK_EVERY_N_WRITES - 1
    )
    assert seen_read_back[PD_READBACK_EVERY_N_WRITES] is True


async def test_slow_actuator_records_non_delivery_at_poll_time():
    """A slow actuator (Zendure HTTP) skips per-write readback, so a silently
    stalled battery never reaches the ACK-path detection. The poll-time check
    must record it (toward exclusion) instead of re-commanding it forever."""
    coord = _SlowCoordFake({
        "force_mode": 2,
        "set_charge_power": 0,
        "set_discharge_power": 300,
        "battery_power": 0,   # ACK'd earlier but now delivering nothing
        "battery_soc": 80,    # above BMS cutoff floor -> real fault, not protection
        "inverter_state": None,
    })
    # Write-only cycle for a slow actuator (read_back=False): confirmed irrelevant.
    coord.apply_power = AsyncMock(return_value=SetpointResult(
        ok=True, net_power_w=-300, confirmed=False, battery_power_w=None,
    ))
    ctrl = _controller()
    ctrl._last_commanded_net_sign[coord] = -1  # steady state, past engage grace
    record = MagicMock(return_value=False)
    ctrl._non_responsive.record_non_delivery = record

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 300)

    assert result is True
    record.assert_called_once()             # tracked toward exclusion at poll time
    coord.apply_power.assert_called_once()  # still re-asserts as a wake nudge


async def test_slow_actuator_skips_when_delivering():
    """A delivering slow actuator is in-state: skip the write, never record."""
    coord = _SlowCoordFake({
        "force_mode": 2,
        "set_charge_power": 0,
        "set_discharge_power": 300,
        "battery_power": -300,  # delivering
    })
    ctrl = _controller()
    record = MagicMock(return_value=False)
    ctrl._non_responsive.record_non_delivery = record

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 300)

    assert result is True
    coord.apply_power.assert_not_called()
    record.assert_not_called()


async def test_slow_actuator_no_record_when_battery_power_unknown():
    """Pre-first-poll: no battery_power reading yet. Must not record a false
    non-delivery, which would wrongly exclude a healthy battery."""
    coord = _SlowCoordFake({
        "force_mode": 2,
        "set_charge_power": 0,
        "set_discharge_power": 300,
        # battery_power absent
    })
    coord.apply_power = AsyncMock(return_value=SetpointResult(
        ok=True, net_power_w=-300, confirmed=False, battery_power_w=None,
    ))
    ctrl = _controller()
    record = MagicMock(return_value=False)
    ctrl._non_responsive.record_non_delivery = record

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 300)

    assert result is True
    record.assert_not_called()


async def test_no_skip_when_data_missing():
    coord = _Coord({})  # no setpoints known yet (pre-first-poll)
    coord.apply_power = AsyncMock(return_value=_ok(-300, battery_power_w=-300))
    ctrl = _controller()

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 300)

    assert result is True
    coord.apply_power.assert_called_once()
