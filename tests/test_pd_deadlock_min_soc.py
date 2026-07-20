"""Regression tests for issue #117 — PD deadlock at min SoC on a slow grid sensor.

Reported field case: main sensor = HA ``enphase_envoy`` (hard-capped at a 60 s
scan interval), 2x Marstek Venus at min SoC, sustained solar surplus. Result was
19 h with 0.00 kWh charged while exporting and 3.81 kWh imported.

Three behaviours interacted:

1. The "no available batteries" bailout returns before the end-of-cycle PD state
   update, so ``last_output_sign`` stayed latched at -1 (discharge) forever.
2. Every fresh sensor sample therefore looked like an unproven discharge->charge
   flip and ``_apply_zero_cross_hold`` clamped it to 0.
3. The stale safety recalc in between froze the command at 0 W, and a 0 W request
   cleared ``_zero_cross_since`` — so the hold re-armed at 0.0 s on every sample
   and could never accumulate its settle window. The reporter's logs show exactly
   that: one suppression per Envoy refresh, always at ``0.0s/5.0s``.

Helpers are exercised unbound with a ``SimpleNamespace`` stub, per repo
convention (see ``test_pd_zero_cross.py``). ``_command_idle_no_batteries`` is
async and only touches controller state plus ``_set_battery_power``, so the stub
carries async doubles for those two calls.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import PD_ZERO_CROSS_MIN_HOLD_S


def _coord(latency_s=0.8):
    return SimpleNamespace(capabilities=SimpleNamespace(actuator_latency_s=latency_s))


def _hold_ctrl(last_output_sign, *, zero_cross_since=None, latencies=(0.8,)):
    return SimpleNamespace(
        last_output_sign=last_output_sign,
        _zero_cross_since=zero_cross_since,
        coordinators=[_coord(lat) for lat in latencies],
    )


def _hold(ctrl, new_power, error=0.0, stale_recalc=False):
    return ChargeDischargeController._apply_zero_cross_hold(
        ctrl, new_power, error, stale_recalc
    )


class _IdleStub:
    """Stub self for ``_command_idle_no_batteries`` recording issued commands."""

    def __init__(self, *, last_output_sign, previous_power, deadband=40):
        self.last_output_sign = last_output_sign
        self.previous_power = previous_power
        self.previous_sensor = None
        self.deadband = deadband
        self._zero_cross_since = dt_util.utcnow()
        self._active_discharge_batteries = ["stale"]
        self._active_charge_batteries = ["stale"]
        self._pd_limited = False
        self.coordinators = [object(), object()]
        self.commands = []

    def _is_active_balance_mode_running(self, coordinator):
        return False

    async def _set_battery_power(self, coordinator, charge, discharge):
        self.commands.append((coordinator, charge, discharge))


async def _idle(stub, sensor_actual, error):
    await ChargeDischargeController._command_idle_no_batteries(stub, sensor_actual, error)


# --- 1. the latch ----------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_bailout_clears_latched_discharge_sign():
    """At min SoC the bailout must not leave the controller latched on discharge."""
    stub = _IdleStub(last_output_sign=-1, previous_power=-300)

    await _idle(stub, sensor_actual=-900, error=-904)

    assert stub.last_output_sign == 0
    assert stub.previous_power == 0
    assert stub.commands == [(c, 0, 0) for c in stub.coordinators]


@pytest.mark.asyncio
async def test_idle_bailout_flags_battery_limited_outside_deadband():
    stub = _IdleStub(last_output_sign=-1, previous_power=-300, deadband=40)

    await _idle(stub, sensor_actual=-900, error=-904)
    assert stub._pd_limited is True

    stub._pd_limited = False
    await _idle(stub, sensor_actual=10, error=10)
    assert stub._pd_limited is False


@pytest.mark.asyncio
async def test_charge_request_after_idle_bailout_is_not_suppressed():
    """End to end: the next surplus sample must reach the batteries as a charge order.

    Before the fix this returned 0 on every sample for 19 hours.
    """
    stub = _IdleStub(last_output_sign=-1, previous_power=-300)
    await _idle(stub, sensor_actual=-900, error=-904)

    ctrl = _hold_ctrl(
        last_output_sign=stub.last_output_sign,
        zero_cross_since=stub._zero_cross_since,
    )
    assert _hold(ctrl, new_power=912, error=-904) == 912


# --- 2. the never-accumulating settle window -------------------------------


def test_stale_recalc_zero_keeps_armed_timer():
    """The stale freeze reissues the previous 0 W command; it is not an idle decision."""
    since = dt_util.utcnow() - timedelta(seconds=2)
    ctrl = _hold_ctrl(last_output_sign=-1, zero_cross_since=since)

    out = _hold(ctrl, new_power=0, error=-904, stale_recalc=True)

    assert out == 0
    assert ctrl._zero_cross_since == since


def test_real_zero_request_still_clears_timer():
    """Guard against over-reach: a fresh 0 W decision must still re-arm the window."""
    ctrl = _hold_ctrl(last_output_sign=-1, zero_cross_since=dt_util.utcnow())

    assert _hold(ctrl, new_power=0, error=10, stale_recalc=False) == 0
    assert ctrl._zero_cross_since is None


def test_stale_recalc_with_no_armed_timer_is_untouched():
    ctrl = _hold_ctrl(last_output_sign=-1, zero_cross_since=None)

    assert _hold(ctrl, new_power=0, error=-904, stale_recalc=True) == 0
    assert ctrl._zero_cross_since is None


def test_flip_accumulates_across_stale_cycles_on_slow_sensor():
    """60 s sensor, 2 s control cycles: the flip must eventually pass.

    Sample 1 arms the timer. The stale recalcs in between hold it. By the time the
    next fresh sample arrives the window is long satisfied, so the charge order
    goes through instead of re-arming at 0.0 s forever.
    """
    ctrl = _hold_ctrl(last_output_sign=-1)

    assert _hold(ctrl, new_power=912, error=-904) == 0
    armed_at = ctrl._zero_cross_since
    assert armed_at is not None

    # ~29 stale cycles at 2 s each; the command is frozen at the previous 0 W.
    for _ in range(29):
        assert _hold(ctrl, new_power=0, error=-904, stale_recalc=True) == 0
    assert ctrl._zero_cross_since == armed_at

    # Fresh sample 60 s later: rewind the arm time to stand in for the elapsed wait.
    ctrl._zero_cross_since = armed_at - timedelta(seconds=PD_ZERO_CROSS_MIN_HOLD_S + 1)
    assert _hold(ctrl, new_power=973, error=-964) == 973
    assert ctrl._zero_cross_since is None
