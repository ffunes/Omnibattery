"""Issue #477: a register battery's manual forced mode is re-asserted if lost."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.drivers.marstek import MarstekModbusDriver

reassert = ChargeDischargeController._reassert_register_manual_intent


def _coord(mode="Discharge", force=0, discharge=0, **overrides):
    coord = SimpleNamespace(
        name="v3",
        manual_force_mode=mode,
        manual_set_charge_power=0,
        manual_set_discharge_power=500,
        is_available=True,
        rs485_user_disabled=False,
        data={"force_mode": force, "set_charge_power": 0, "set_discharge_power": discharge},
        driver=SimpleNamespace(
            net_power_from_data=lambda d: MarstekModbusDriver.net_power_from_data(None, d)
        ),
        apply_power=AsyncMock(),
    )
    for key, value in overrides.items():
        setattr(coord, key, value)
    return coord


def test_lost_forced_discharge_is_rewritten_once_per_window():
    coord = _coord(force=0, discharge=0)  # firmware dropped to None / 0 W
    asyncio.run(reassert(coord))
    coord.apply_power.assert_awaited_once_with(-500)
    asyncio.run(reassert(coord))  # still lost, but throttled
    assert coord.apply_power.await_count == 1


def test_forced_power_zeroed_under_discharge_mode_is_rewritten():
    coord = _coord(force=2, discharge=0)
    asyncio.run(reassert(coord))
    coord.apply_power.assert_awaited_once_with(-500)


@pytest.mark.parametrize(
    "coord",
    [
        _coord(force=2, discharge=400),  # same direction, clamp differs: leave it
        _coord(mode="None"),  # idle intent is never asserted
        _coord(is_available=False),
        _coord(rs485_user_disabled=True),
        _coord(data={}),  # no telemetry yet
    ],
)
def test_no_write_when_intent_holds_or_battery_is_off_limits(coord):
    asyncio.run(reassert(coord))
    coord.apply_power.assert_not_awaited()
