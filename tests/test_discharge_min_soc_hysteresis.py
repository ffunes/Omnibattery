"""Tests for the min-SOC re-entry hysteresis in ``_get_available_batteries``.

Regression for the min_soc ping-pong: discharge availability was a bare
``soc > min_soc`` check. After a battery empties to min_soc and rests, its SOC
rebounds 1-2% (cell relaxation), re-admitting it for a sliver of discharge that
drops it right back — relay on/off cycling and micro-cycles at the worst SOC
region. The fix latches the exclusion at min_soc and only releases it after the
SOC recovers ``DISCHARGE_MIN_SOC_REENTRY_MARGIN`` percent above min_soc.

Exercised unbound with the shared FakeCoordinator and a light controller stub
(same pattern as test_charge_hysteresis.py).
"""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery import (
    ChargeDischargeController,
    DISCHARGE_MIN_SOC_REENTRY_MARGIN,
)

from tests.conftest import FakeCoordinator


def _coord(soc, *, min_soc=10):
    return FakeCoordinator(min_soc=min_soc, data={"battery_soc": soc})


def _ctrl(coords):
    return SimpleNamespace(
        coordinators=list(coords),
        _non_responsive=SimpleNamespace(is_excluded=lambda c: False),
        _is_active_balance_mode_running=lambda c: False,
        _is_backup_function_active=lambda c: False,
        _is_manual_slot_owned=lambda c: False,
        is_discharge_blocked=lambda c: False,
    )


def _available(ctrl):
    return ChargeDischargeController._get_available_batteries(ctrl, is_charging=False)


def test_above_margin_available():
    c = _coord(soc=50)
    assert _available(_ctrl([c])) == [c]


def test_at_min_soc_excluded_and_latched():
    c = _coord(soc=10)
    assert _available(_ctrl([c])) == []
    assert c._discharge_min_soc_latched is True


def test_rebound_within_margin_stays_excluded():
    # The ping-pong regression: SOC rebounds to min_soc + 1 after resting.
    # Without the latch this re-admitted the battery for a sliver of discharge.
    c = _coord(soc=10)
    ctrl = _ctrl([c])
    assert _available(ctrl) == []          # hits min_soc -> latch
    c.data["battery_soc"] = 11             # rebound, still inside the margin
    assert _available(ctrl) == []


def test_recovery_past_margin_releases_latch():
    c = _coord(soc=10)
    ctrl = _ctrl([c])
    assert _available(ctrl) == []
    c.data["battery_soc"] = 10 + DISCHARGE_MIN_SOC_REENTRY_MARGIN
    assert _available(ctrl) == [c]
    assert c._discharge_min_soc_latched is False


def test_fresh_start_inside_margin_not_latched():
    # After a restart the latch is gone; a battery sitting at min_soc + 1 with
    # no recorded excursion to min_soc is admitted (at most one extra engage).
    c = _coord(soc=11)
    assert _available(_ctrl([c])) == [c]


if __name__ == "__main__":
    test_above_margin_available()
    test_at_min_soc_excluded_and_latched()
    test_rebound_within_margin_stays_excluded()
    test_recovery_past_margin_releases_latch()
    test_fresh_start_inside_margin_not_latched()
    print("ok")
