"""Tests for the engage grace after a large step up in commanded power.

A battery that follows a small command needs time to ramp when the command
jumps several-fold in one step, and a driver that serves cached telemetry keeps
showing the pre-step power for seconds. A large step up by a battery that met
the delivery bar for its previous command restarts that direction's engage
grace, like a direction switch. It only restarts the clock: the non-responsive
episode and the high-SOC taper clock carry on.

The replays drive ``_set_battery_power`` once a second on a slow actuator, whose
delivery is judged at poll time once the driver shows the new command. The
method is exercised unbound with light stubs, as in test_set_battery_power_skip.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import (
    DISCHARGE_ENGAGE_GRACE_S,
    LOW_SOC_REFUSAL_CONFIRM_S,
)
from custom_components.omnibattery.drivers import SetpointResult
from custom_components.omnibattery.tracking.non_responsive_tracker import (
    NonResponsiveTracker,
)
from tests.conftest import FakeCoordinator


class _HttpCoord(FakeCoordinator):
    """Slow actuator without RS485 control: no per-write readback."""

    @property
    def capabilities(self):
        return replace(
            super().capabilities, actuator_latency_s=3.0, has_rs485_control=False
        )


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


def _setup(clock, *, command, battery_power, soc=60):
    """A battery that has been following ``command`` (W, +charge/-discharge)."""
    coord = _HttpCoord(
        name="BAT1",
        min_soc=10,
        data={"battery_power": battery_power, "battery_soc": soc, "inverter_state": None},
        apply_power=AsyncMock(),
    )

    async def _queue(net, *, read_back=False):
        # The driver queues the tuple; its cached state shows it from now on.
        coord.data.update(
            force_mode=1 if net > 0 else 2 if net < 0 else 0,
            set_charge_power=max(net, 0),
            set_discharge_power=max(-net, 0),
        )
        return SetpointResult(ok=True, net_power_w=net, confirmed=False)

    coord.apply_power.side_effect = _queue
    coord.data.update(
        force_mode=1 if command > 0 else 2,
        set_charge_power=max(command, 0),
        set_discharge_power=max(-command, 0),
    )
    coord.commanded_charge_power = max(command, 0)
    coord.commanded_discharge_power = max(-command, 0)

    direction = 1 if command > 0 else -1
    long_ago = clock.now - timedelta(minutes=10)
    ctrl = SimpleNamespace(
        _is_backup_function_active=lambda c: False,
        _is_manual_slot_owned=lambda c: False,
        get_charge_blockers=lambda c: {},
        get_discharge_blockers=lambda c: {},
        _log_low_power_delivery=lambda coordinator, **k: None,
        _last_commanded_net_sign={coord: direction},
        _charge_engage_started={coord: long_ago} if direction > 0 else {},
        _discharge_engage_started={coord: long_ago} if direction < 0 else {},
        _high_soc_taper_started={},
        _idle_commanded_started={},
        _idle_runaway_handled={},
        _non_responsive=NonResponsiveTracker(fail_threshold=3),
        _weekly_charge_mgr=SimpleNamespace(is_battery_full=lambda c: False),
        _attempt_wake=AsyncMock(return_value=False),
    )
    ctrl._check_non_delivery = ChargeDischargeController._check_non_delivery.__get__(ctrl)
    return coord, ctrl


async def _cycle(ctrl, coord, clock, command, battery_power):
    """One control cycle, a second after the previous one."""
    clock.now += timedelta(seconds=1)
    coord.data["battery_power"] = battery_power
    return await ChargeDischargeController._set_battery_power(
        ctrl, coord, max(command, 0), max(-command, 0),
    )


def _fail_count(ctrl, coord):
    return ctrl._non_responsive.batteries.get(coord, {}).get("fail_count", 0)


async def test_large_step_is_not_judged_while_the_battery_ramps(clock):
    """A battery delivering its 80 W command gets 405 W, then 2400 W. The cached
    power keeps showing 82 W for several seconds before the ramp appears."""
    coord, ctrl = _setup(clock, command=80, battery_power=82)
    for _ in range(5):
        await _cycle(ctrl, coord, clock, 80, 82)

    await _cycle(ctrl, coord, clock, 405, 82)
    await _cycle(ctrl, coord, clock, 2400, 82)
    for _ in range(6):
        await _cycle(ctrl, coord, clock, 2400, 82)
    assert _fail_count(ctrl, coord) == 0
    assert ctrl._non_responsive.is_excluded(coord) is False

    for power in (490, 800, 1100, 1532):
        await _cycle(ctrl, coord, clock, 2400, power)
    assert ctrl._non_responsive.is_excluded(coord) is False


async def test_battery_that_never_follows_the_step_is_excluded_after_the_grace(clock):
    coord, ctrl = _setup(clock, command=80, battery_power=82)
    await _cycle(ctrl, coord, clock, 405, 82)
    await _cycle(ctrl, coord, clock, 2400, 82)
    stepped_at = clock.now

    while clock.now - stepped_at < timedelta(seconds=DISCHARGE_ENGAGE_GRACE_S - 1):
        await _cycle(ctrl, coord, clock, 2400, 82)
    assert _fail_count(ctrl, coord) == 0
    assert ctrl._non_responsive.is_excluded(coord) is False

    for _ in range(10):
        await _cycle(ctrl, coord, clock, 2400, 82)
    assert ctrl._non_responsive.is_excluded(coord) is True


async def test_zero_watt_stall_is_excluded_while_the_pd_re_ramps(clock):
    """A battery stuck at 0 W: the PD anti-windup re-anchors to the measured 0 W
    every ~15s and ramps back to the cap in steps of up to 2x. None of those
    steps restarts the grace, because the battery never met the previous bar."""
    coord, ctrl = _setup(clock, command=2400, battery_power=0)
    engage_stamp = ctrl._charge_engage_started[coord]

    for _ in range(2):
        for command in (800, 1600, *[2400] * 12):
            await _cycle(ctrl, coord, clock, command, 0)
        if ctrl._non_responsive.is_excluded(coord):
            break

    assert ctrl._non_responsive.is_excluded(coord) is True
    assert ctrl._charge_engage_started[coord] == engage_stamp


async def test_tapering_battery_re_ramped_in_small_steps_is_judged_as_before(clock):
    """A battery tapering to 235 W of a 2400 W command at 98%: every ~15s the PD
    re-anchors to the measured power and climbs back to the cap in steps below
    2x. The battery meets the bar for each previous command, but no step is
    large enough to restart the grace, so it is judged at the cap as before."""
    coord, ctrl = _setup(clock, command=2400, battery_power=235, soc=98)
    engage_stamp = ctrl._charge_engage_started[coord]

    for _ in range(2):
        for command in (635, 1035, 1435, 1835, 2235, *[2400] * 10):
            await _cycle(ctrl, coord, clock, command, 235)
        if ctrl._non_responsive.is_excluded(coord):
            break

    assert ctrl._non_responsive.is_excluded(coord) is True
    assert ctrl._charge_engage_started[coord] == engage_stamp


async def test_large_step_only_restarts_the_engage_clock(clock):
    """Unlike a direction switch, a step neither clears the non-responsive
    episode nor restarts the high-SOC taper clock."""
    coord, ctrl = _setup(clock, command=300, battery_power=120, soc=99)
    clear = MagicMock(wraps=ctrl._non_responsive.clear)
    ctrl._non_responsive.clear = clear
    taper_started = clock.now - timedelta(minutes=5)
    ctrl._high_soc_taper_started[coord] = taper_started

    await _cycle(ctrl, coord, clock, 2400, 120)

    assert ctrl._charge_engage_started[coord] == clock.now
    clear.assert_not_called()
    assert ctrl._high_soc_taper_started[coord] == taper_started


async def test_direction_switch_still_starts_a_fresh_episode(clock):
    coord, ctrl = _setup(clock, command=-300, battery_power=-290, soc=99)
    clear = MagicMock(wraps=ctrl._non_responsive.clear)
    ctrl._non_responsive.clear = clear
    ctrl._high_soc_taper_started[coord] = clock.now - timedelta(minutes=5)

    await _cycle(ctrl, coord, clock, 2400, -290)

    assert ctrl._charge_engage_started[coord] == clock.now
    clear.assert_called_once_with(coord, delivering=False)
    assert coord not in ctrl._high_soc_taper_started


async def test_large_discharge_step_is_not_judged_while_the_battery_ramps(clock):
    coord, ctrl = _setup(clock, command=-100, battery_power=-95)
    await _cycle(ctrl, coord, clock, -100, -95)

    await _cycle(ctrl, coord, clock, -2400, -95)
    for _ in range(8):
        await _cycle(ctrl, coord, clock, -2400, -95)

    assert _fail_count(ctrl, coord) == 0
    assert ctrl._discharge_engage_started[coord] == clock.now - timedelta(seconds=8)


async def test_low_soc_refusal_clock_runs_through_a_large_step(clock):
    """A battery refusing discharge near its floor did not meet the previous
    bar, so a step does not pause the refusal clock that takes it out of the
    discharge pool."""
    coord, ctrl = _setup(clock, command=-300, battery_power=0, soc=15)
    engage_stamp = ctrl._discharge_engage_started[coord]
    await _cycle(ctrl, coord, clock, -300, 0)
    assert coord._low_soc_refusal_started == clock.now

    await _cycle(ctrl, coord, clock, -2400, 0)
    for _ in range(LOW_SOC_REFUSAL_CONFIRM_S):
        await _cycle(ctrl, coord, clock, -2400, 0)

    assert ctrl._discharge_engage_started[coord] == engage_stamp
    assert coord._discharge_refused_soc == 15
