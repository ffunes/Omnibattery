"""Control cycles must launch as config-entry background tasks.

Timer/state-change trackers run their callbacks as HA-tracked tasks, and HA
startup waits for tracked tasks. A cycle stuck in Modbus retries against a slow
gateway blocked the whole bootstrap ("Something is blocking Home Assistant...")
and delayed every integration set up after this one. Background tasks are
exempt from the startup gate; entry unload still cancels them.

Exercised unbound with light stubs (same pattern as test_no_pd_tracking).
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController


def _stub_controller(calls):
    def _bg(hass, coro, name):
        calls.append((hass, coro, name))
        coro.close()  # never awaited in this test

    async def _cycle(now=None):
        pass

    return SimpleNamespace(
        hass=object(),
        config_entry=SimpleNamespace(async_create_background_task=_bg),
        async_update_charge_discharge=_cycle,
        _run_no_pd_debounced_cycle=_cycle,
        _no_pd_debounce_unsub=object(),
    )


def test_schedule_control_cycle_launches_background_task():
    calls = []
    ctl = _stub_controller(calls)
    ChargeDischargeController.schedule_control_cycle(ctl, now=None)
    assert len(calls) == 1
    hass, _coro, name = calls[0]
    assert hass is ctl.hass
    assert name == "omnibattery_control_cycle"


def test_no_pd_debounce_fire_launches_background_task():
    calls = []
    ctl = _stub_controller(calls)
    ChargeDischargeController._fire_no_pd_debounced_run(ctl, None)
    assert ctl._no_pd_debounce_unsub is None
    assert len(calls) == 1
    assert calls[0][2] == "omnibattery_no_pd_cycle"


def _paced_controller(runs):
    """Stub with just the state the pacing gate reads."""

    async def _run(now=None):
        runs.append(now)

    return SimpleNamespace(
        _unloading=False,
        no_pd_mode_enabled=False,
        _no_pd_command_delay=0.0,
        _min_cycle_interval_s=1.0,
        _last_cycle_monotonic=time.monotonic(),
        _control_lock=asyncio.Lock(),
        _phase_safety_pending=True,
        _run_control_cycle=_run,
    )


async def test_event_trigger_inside_the_min_interval_is_dropped():
    """Phase sensors schedule cycles with no `now`, so this gate paces them (#452).

    Three phase meters on a 1 Hz P1 used to bypass it and drive several ungated
    write bursts per second, which a slow bridge answers with a full queue. The
    drop is safe because _phase_safety_pending survives it.
    """
    runs = []
    ctl = _paced_controller(runs)

    await ChargeDischargeController.async_update_charge_discharge(ctl, None)

    assert runs == []
    assert ctl._phase_safety_pending is True


async def test_the_safety_timer_is_never_paced():
    runs = []
    ctl = _paced_controller(runs)
    stamp = dt_util.utcnow()

    await ChargeDischargeController.async_update_charge_discharge(ctl, stamp)

    assert runs == [stamp]


def _clocked_controller(monkeypatch, runs, *, keeps_last_command):
    """Paced stub on a fake monotonic clock.

    Every run reports through ``_cycle_kept_last_command`` whether it found no
    new meter sample and kept the last command.
    """
    clock = [0.0]
    monkeypatch.setattr(
        "custom_components.omnibattery.time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    ctl = _paced_controller(runs)
    ctl._last_cycle_monotonic = -10.0

    async def _run(now=None):
        runs.append((clock[0], now))
        ctl._cycle_kept_last_command = keeps_last_command

    ctl._run_control_cycle = _run
    return ctl, clock


async def _trigger(ctl, clock, *steps):
    for at, now in steps:
        clock[0] = at
        await ChargeDischargeController.async_update_charge_discharge(ctl, now)


async def test_a_tick_that_kept_the_last_command_does_not_hold_off_the_meter(monkeypatch):
    """Meter updates every 2s, a safety-timer tick 1.4s after the first one.

    The tick finds nothing new, so it doesn't count for PD Min Cycle Interval
    and the update at 2.0s runs at once instead of waiting for the next tick
    at 3.4s.
    """
    runs = []
    ctl, clock = _clocked_controller(monkeypatch, runs, keeps_last_command=True)
    tick = dt_util.utcnow()

    await _trigger(ctl, clock, (0.0, None), (1.4, tick), (2.0, None))

    assert runs == [(0.0, None), (1.4, tick), (2.0, None)]
    assert ctl._last_cycle_monotonic == 2.0


async def test_a_tick_that_acted_on_a_new_sample_still_paces_the_meter(monkeypatch):
    """With a fast meter a tick usually finds a new sample and acts on it.

    That tick still counts for PD Min Cycle Interval, so ticks cannot add
    cycles on top of the paced meter updates (#452).
    """
    runs = []
    ctl, clock = _clocked_controller(monkeypatch, runs, keeps_last_command=False)
    tick = dt_util.utcnow()

    await _trigger(ctl, clock, (0.0, None), (1.4, tick), (2.0, None))

    assert runs == [(0.0, None), (1.4, tick)]
    assert ctl._last_cycle_monotonic == 1.4


async def test_a_meter_update_that_kept_the_last_command_still_counts(monkeypatch):
    """Only safety-timer ticks give the interval back; meter updates always count."""
    runs = []
    ctl, clock = _clocked_controller(monkeypatch, runs, keeps_last_command=True)

    await _trigger(ctl, clock, (0.0, None), (0.6, None))

    assert runs == [(0.0, None)]
    assert ctl._last_cycle_monotonic == 0.0
