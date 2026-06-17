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

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.marstek_venus_energy_manager import ChargeDischargeController
from custom_components.marstek_venus_energy_manager.const import (
    PD_READBACK_EVERY_N_WRITES,
)
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
        write_power_atomic=AsyncMock(),
    )


def _controller():
    return SimpleNamespace(
        _is_backup_function_active=lambda c: False,
        _is_manual_slot_owned=lambda c: False,
        get_charge_blockers=lambda c: {},
        get_discharge_blockers=lambda c: {},
        _log_low_power_delivery=lambda coordinator, **k: None,
        _normal_balance_top_voltage_seen={},
        _non_responsive=SimpleNamespace(
            record_non_delivery=lambda *a, **k: False,
            clear=lambda c: None,
        ),
    )


async def test_skip_when_idle_unchanged():
    coord = _Coord({"force_mode": 0, "set_charge_power": 0, "set_discharge_power": 0})
    ctrl = _controller()

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 0)

    assert result is True
    coord.write_power_atomic.assert_not_called()


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
    coord.write_power_atomic.assert_not_called()


async def test_skip_when_charge_unchanged():
    coord = _Coord({"force_mode": 1, "set_charge_power": 500, "set_discharge_power": 0})
    ctrl = _controller()

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 500, 0)

    assert result is True
    coord.write_power_atomic.assert_not_called()


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
    coord.write_power_atomic = AsyncMock(return_value={
        "force_mode": 2,
        "set_charge_power": 0,
        "set_discharge_power": 300,
        "battery_power": 0,
    })
    ctrl = _controller()
    record = MagicMock(return_value=False)  # sync: not awaited in _set_battery_power
    ctrl._non_responsive.record_non_delivery = record

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 300)

    assert result is True
    coord.write_power_atomic.assert_called_once()
    record.assert_called_once()


async def test_no_skip_when_setpoints_differ():
    coord = _Coord({
        "force_mode": 2,
        "set_charge_power": 0,
        "set_discharge_power": 100,  # device at 100W, commanding 300W
        "battery_power": -100,
    })
    coord.write_power_atomic = AsyncMock(return_value={
        "force_mode": 2,
        "set_charge_power": 0,
        "set_discharge_power": 300,
        "battery_power": -300,
    })
    ctrl = _controller()

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 300)

    assert result is True
    coord.write_power_atomic.assert_called_once()


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

    async def fake_write(discharge, charge, force, read_back=True):
        seen_read_back.append(read_back)
        result = {
            "force_mode": force,
            "set_charge_power": charge,
            "set_discharge_power": discharge,
        }
        if read_back:
            result["battery_power"] = 0
        return result

    coord.write_power_atomic = fake_write
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


async def test_no_skip_when_data_missing():
    coord = _Coord({})  # no setpoints known yet (pre-first-poll)
    coord.write_power_atomic = AsyncMock(return_value={
        "force_mode": 2,
        "set_charge_power": 0,
        "set_discharge_power": 300,
        "battery_power": -300,
    })
    ctrl = _controller()

    result = await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 300)

    assert result is True
    coord.write_power_atomic.assert_called_once()
