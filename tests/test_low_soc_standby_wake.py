"""A battery in Standby below the BMS cutoff SOC gets the one-shot wake (#720).

At SOC <= BMS_DISCHARGE_CUTOFF_SOC a discharge that delivers nothing was taken
as a BMS cutoff and returned before the standby wake/reconnect path. A battery
left in Standby by a short AC interruption was then never woken. It now gets
one standby wake per refusal episode before being judged as cut off.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import (
    LOW_SOC_REFUSAL_CONFIRM_S,
    NORMAL_BALANCE_RECAL_INVERTER_STANDBY,
)
from custom_components.omnibattery.tracking.non_responsive_tracker import (
    NonResponsiveTracker,
)
from tests.conftest import FakeCoordinator


class _Clock:
    def __init__(self) -> None:
        self.now = dt_util.utcnow()

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(dt_util, "utcnow", clock)
    return clock


def _setup(clock, *, inverter_state):
    coord = FakeCoordinator(
        name="BAT1",
        min_soc=10,
        data={
            "battery_power": 0,
            "battery_soc": 16,
            "inverter_state": inverter_state,
        },
    )
    ctrl = SimpleNamespace(
        _discharge_engage_started={coord: clock.now - timedelta(minutes=10)},
        _charge_engage_started={},
        _high_soc_taper_started={},
        _non_responsive=NonResponsiveTracker(fail_threshold=3),
        _weekly_charge_mgr=SimpleNamespace(is_battery_full=lambda c: False),
        _attempt_wake=AsyncMock(return_value=True),
    )
    return coord, ctrl


async def _judge(ctrl, coord, clock, delivered=0):
    """One delivery judgement; ``delivered`` is battery power (W, -discharge)."""
    clock.now += timedelta(seconds=1)
    await ChargeDischargeController._check_non_delivery(
        ctrl, coord, 675, delivered, attempt=0, direction="discharge",
    )


async def test_standby_battery_below_cutoff_soc_is_woken_once(clock):
    coord, ctrl = _setup(clock, inverter_state=NORMAL_BALANCE_RECAL_INVERTER_STANDBY)

    await _judge(ctrl, coord, clock)
    ctrl._attempt_wake.assert_awaited_once_with(coord, is_standby=True)

    # Still nothing after the wake: a real cutoff, no further wakes, and the
    # battery leaves the discharge pool as before.
    for _ in range(LOW_SOC_REFUSAL_CONFIRM_S + 1):
        await _judge(ctrl, coord, clock)
    ctrl._attempt_wake.assert_awaited_once()
    assert coord._discharge_refused_soc == 16


async def test_delivery_rearms_the_low_soc_standby_wake(clock):
    coord, ctrl = _setup(clock, inverter_state=NORMAL_BALANCE_RECAL_INVERTER_STANDBY)
    await _judge(ctrl, coord, clock)
    await _judge(ctrl, coord, clock, delivered=-600)
    await _judge(ctrl, coord, clock)
    assert ctrl._attempt_wake.await_count == 2


async def test_awake_battery_below_cutoff_soc_is_not_woken(clock):
    coord, ctrl = _setup(clock, inverter_state=None)
    for _ in range(5):
        await _judge(ctrl, coord, clock)
    ctrl._attempt_wake.assert_not_awaited()
