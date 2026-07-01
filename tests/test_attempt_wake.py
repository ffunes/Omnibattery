"""Tests for the non-responsive wake nudge branching in ``_attempt_wake``.

A battery already sitting in standby with RS485 reading enabled is exactly the
case only an HA restart used to fix (a fresh TCP connection, not a register
write) -- so that path goes straight to ``async_reconnect_fresh``. The
non-standby path must keep the re-assert-only behaviour so it doesn't regress
issue #434 (handing control to a live-exporting battery's internal logic via a
disable step).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.omnibattery import ChargeDischargeController
from tests.conftest import FakeCoordinator


def _controller():
    return SimpleNamespace(_set_battery_power=AsyncMock(return_value=True))


def _coord(**overrides):
    coord = FakeCoordinator(name="BAT1", rs485_user_disabled=False)
    coord.set_rs485_control = AsyncMock(return_value=True)
    coord.rs485_control_enabled = AsyncMock(return_value=True)
    coord.async_reconnect_fresh = AsyncMock(return_value=True)
    for key, value in overrides.items():
        setattr(coord, key, value)
    return coord


async def test_standby_reconnects_fresh():
    coord = _coord()
    ctrl = _controller()

    result = await ChargeDischargeController._attempt_wake(ctrl, coord, is_standby=True)

    assert result is True
    coord.async_reconnect_fresh.assert_awaited_once()
    coord.set_rs485_control.assert_not_awaited()  # reconnect_fresh re-enables RS485 itself
    ctrl._set_battery_power.assert_awaited_once()


async def test_non_standby_reasserts_without_disabling():
    """Issue #434 regression guard: must never write RS485=False for an awake
    battery that might be exporting under its own internal logic."""
    coord = _coord()
    ctrl = _controller()

    result = await ChargeDischargeController._attempt_wake(ctrl, coord, is_standby=False)

    assert result is True
    coord.set_rs485_control.assert_awaited_once_with(True)
    ctrl._set_battery_power.assert_awaited_once()


async def test_non_standby_reconnects_fresh_when_reassert_does_not_take():
    coord = _coord()
    coord.rs485_control_enabled = AsyncMock(return_value=False)
    ctrl = _controller()

    result = await ChargeDischargeController._attempt_wake(ctrl, coord, is_standby=False)

    assert result is True
    coord.async_reconnect_fresh.assert_awaited_once()


async def test_skipped_when_rs485_user_disabled():
    coord = _coord(rs485_user_disabled=True)
    ctrl = _controller()

    result = await ChargeDischargeController._attempt_wake(ctrl, coord, is_standby=True)

    assert result is False
    coord.set_rs485_control.assert_not_awaited()
