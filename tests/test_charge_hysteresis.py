"""Tests for the charge-hysteresis latch in ``_refresh_battery_charge_limit_blocks``.

Regression for the stale-latch bug: when the configured ceiling (``max_soc``) is
temporarily lowered to at/below the current SOC and then raised again, the
hysteresis latch had captured the lower SOC and blocked charging up to the new
target until the battery discharged 10 % below that lower latch point. On Zendure
``coordinator.max_soc`` tracks the polled ``socSet``, so any Target-SOC dip during
a charge could lock the battery partway. The fix releases the latch when
``max_soc`` rises above the latched base, while preserving a genuine
top-of-charge hold.

The method is exercised unbound with light stubs for ``self`` and the
coordinator, so no full ``ChargeDischargeController`` has to be constructed
(same pattern as test_set_battery_power_skip.py).
"""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.binary_sensor import ChargeHysteresisActiveSensor
from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
)


class _Coord:
    """Identity-hashable coordinator stand-in (used as a dict key)."""

    def __init__(self, *, soc, max_soc, base=None, active=False,
                 vmax=3.30, pct=10, name="bat", battery_version="v2"):
        self.name = name
        self.battery_version = battery_version
        self.data = {"battery_soc": soc, "max_cell_voltage": vmax}
        self.max_soc = max_soc
        self.min_soc = 10
        self.charge_hysteresis_percent = pct
        self.enable_charge_hysteresis = True
        self._hysteresis_active = active
        self._hysteresis_base_soc = base
        self.battery_manual_mode_enabled = False
        self.is_available = True
        self._consecutive_failures = 0
        self.rs485_user_disabled = False
        self._discharge_min_soc_latched = False


def _controller(coord, *, bms_full=False):
    blocks: dict = {}

    def _set_block(source, reason, details=None, coordinator=None):
        blocks.setdefault(coordinator, set()).add(source)

    def _remove_block(source, coordinator=None):
        blocks.get(coordinator, set()).discard(source)

    ctrl = SimpleNamespace(
        coordinators=[coord],
        _weekly_full_charge_unlocked=lambda: False,
        _normal_balance_bms_cutoff_retry_pending={},
        _normal_balance_recal_override={},
        _weekly_charge_mgr=SimpleNamespace(is_battery_full=lambda c: bms_full),
        _effective_charge_max_soc=lambda c, weekly, **_kw: (c.max_soc, "config"),
        _predictive_solar_only_batteries={},
        set_charge_block=_set_block,
        remove_charge_block=_remove_block,
    )
    ctrl._blocks = blocks
    return ctrl


def _hyst_blocked(ctrl, coord):
    return "charge_hysteresis" in ctrl._blocks.get(coord, set())


# ----------------------------------------------------------------------
# The fix: a stale latch clears when the ceiling is raised back up
# ----------------------------------------------------------------------

def test_stale_latch_clears_when_target_raised_above_base():
    # Latched at base=90 (Target SOC had been lowered to 90), now raised to 100.
    # SOC 90 and climbing -> must resume charging toward 100, not stay locked.
    c = _Coord(soc=90, max_soc=100, base=90, active=True)
    ctrl = _controller(c)

    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)

    assert c._hysteresis_active is False
    assert c._hysteresis_base_soc is None
    assert not _hyst_blocked(ctrl, c)


def test_target_lowered_then_raised_releases_the_latch():
    # Target SOC lowered to the current SOC latches there; raising it again
    # lets the battery charge on toward the new target.
    c = _Coord(soc=90, max_soc=100)
    ctrl = _controller(c)
    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)
    assert not _hyst_blocked(ctrl, c)

    c.max_soc = 90
    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)
    assert _hyst_blocked(ctrl, c)
    assert c._hysteresis_base_soc == 90

    c.max_soc = 100
    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)
    assert not _hyst_blocked(ctrl, c)
    assert c._hysteresis_base_soc is None


# ----------------------------------------------------------------------
# A BMS cutoff below max SOC holds until the hysteresis threshold
# ----------------------------------------------------------------------

def _cutoff_setup():
    """A battery at 99% (max SOC 100, hysteresis 2%) asked to charge that its
    BMS refuses."""
    c = _Coord(soc=99, max_soc=100, pct=2)
    c.data.update(battery_power=0, inverter_state=1)
    c.commanded_charge_power = 2400
    ctrl = _controller(c)
    weekly = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    weekly._controller = ctrl
    weekly._bms_cutoff_counts = {}
    weekly._bms_charge_commanded_at = {c.name: 0.0}  # charging for a while
    weekly._already_complete_logged = False
    weekly.is_active = lambda: False
    ctrl._weekly_charge_mgr = weekly
    ctrl._non_responsive = SimpleNamespace(is_excluded=lambda _coord: False)
    ctrl._is_backup_function_active = lambda _coord: False
    ctrl._is_manual_slot_owned = lambda _coord: False
    sensor = ChargeHysteresisActiveSensor.__new__(ChargeHysteresisActiveSensor)
    sensor.coordinator = c
    return c, ctrl, weekly, sensor


def _cycle(ctrl, weekly):
    """One control cycle: detector tick, blocker refresh, charge availability."""
    weekly.tick_bms_cutoff()
    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)
    return ChargeDischargeController._get_available_batteries(ctrl, True, False)


def test_cutoff_below_max_soc_holds_until_the_resume_threshold(bms_cutoff_clock):
    c, ctrl, weekly, sensor = _cutoff_setup()
    for _ in range(10):
        _cycle(ctrl, weekly)
    assert weekly.is_battery_full(c)
    assert _cycle(ctrl, weekly) == []
    assert _hyst_blocked(ctrl, c)
    assert c._hysteresis_base_soc == 99
    assert sensor.extra_state_attributes["charge_resume_threshold"] == 97

    # Full: no longer asked to charge. Standby draw takes it down slowly.
    c.commanded_charge_power = 0
    for soc in (99, 98, 97):
        c.data["battery_soc"] = soc
        for _ in range(5):
            assert _cycle(ctrl, weekly) == []
        assert _hyst_blocked(ctrl, c)
        assert sensor.extra_state_attributes["charge_resume_threshold"] == 97

    c.data["battery_soc"] = 96
    assert _cycle(ctrl, weekly) == [c]
    assert not _hyst_blocked(ctrl, c)
    assert c._hysteresis_base_soc is None


# ----------------------------------------------------------------------
# Regressions: legitimate behaviour preserved
# ----------------------------------------------------------------------

def test_latch_holds_after_full_charge_when_ceiling_unchanged():
    # Reached 100 at max_soc=100 (base=100), drifted to 95. Must stay latched:
    # don't recharge until 10 % below the top.
    c = _Coord(soc=95, max_soc=100, base=100, active=True)
    ctrl = _controller(c)

    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)

    assert c._hysteresis_active is True
    assert _hyst_blocked(ctrl, c)


def test_activates_and_latches_base_at_ceiling():
    c = _Coord(soc=100, max_soc=100, base=None, active=False)
    ctrl = _controller(c)

    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)

    assert c._hysteresis_active is True
    assert c._hysteresis_base_soc == 100
    assert _hyst_blocked(ctrl, c)


def test_normal_release_below_threshold():
    # base=100 -> threshold 90; SOC 89 falls through it -> release.
    c = _Coord(soc=89, max_soc=100, base=100, active=True)
    ctrl = _controller(c)

    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)

    assert c._hysteresis_active is False
    assert c._hysteresis_base_soc is None
    assert not _hyst_blocked(ctrl, c)


def test_lowered_ceiling_still_latches_and_holds():
    # User deliberately lowers the ceiling to 80 while at 85: latch at 85 and
    # hold (max_soc 80 is NOT > base 85, so the stale-clear must not fire).
    c = _Coord(soc=85, max_soc=80, base=85, active=True)
    ctrl = _controller(c)

    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)

    assert c._hysteresis_active is True
    assert _hyst_blocked(ctrl, c)


def test_venus_ad_top_voltage_does_not_create_charge_hysteresis_block():
    c = _Coord(
        soc=100,
        max_soc=100,
        vmax=3.60,
        battery_version="vA",
    )
    ctrl = _controller(c)
    ctrl._should_charge_to_bms_cutoff = lambda _coord, _max_soc: True

    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)

    assert c._hysteresis_active is False
    assert not _hyst_blocked(ctrl, c)


def test_venus_ad_top_voltage_remains_available_at_reported_100_soc():
    c = _Coord(
        soc=100,
        max_soc=100,
        vmax=3.57,
        battery_version="vD",
    )
    ctrl = _controller(c)
    ctrl._non_responsive = SimpleNamespace(is_excluded=lambda _coord: False)
    ctrl._is_backup_function_active = lambda _coord: False
    ctrl._is_manual_slot_owned = lambda _coord: False
    ctrl.get_charge_blockers = lambda _coord: {}
    ctrl.is_discharge_blocked = lambda _coord: False
    ctrl.get_discharge_blockers = lambda _coord: {}
    ctrl._should_charge_to_bms_cutoff = lambda _coord, _max_soc: True
    ctrl._weekly_charge_mgr = SimpleNamespace(is_battery_full=lambda _coord: True)

    assert ChargeDischargeController._get_available_batteries(ctrl, True, False) == [c]


def test_venus_ad_first_cutoff_waiting_blocks_only_until_relaxation_retry():
    c = _Coord(
        soc=94,
        max_soc=100,
        vmax=3.60,
        battery_version="vA",
    )
    ctrl = _controller(c)
    ctrl._normal_balance_bms_cutoff_retry_pending[c] = True

    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)

    assert "bms_cutoff_retry" in ctrl._blocks[c]
    assert not _hyst_blocked(ctrl, c)


def _predictive_ctrl(coord, target):
    ctrl = _controller(coord)
    ctrl._effective_charge_max_soc = lambda c, weekly, ignore_predictive_target=False: (
        (c.max_soc, "max_soc") if ignore_predictive_target else (target, "predictive_target")
    )
    return ctrl


def test_predictive_target_reports_solar_only_instead_of_a_charge_block():
    # Issue #470: past the grid-charge target the battery still takes solar.
    c = _Coord(soc=38, max_soc=100)
    ctrl = _predictive_ctrl(c, 37.3)

    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)

    assert "max_soc" not in ctrl._blocks.get(c, set())
    assert ctrl._predictive_solar_only_batteries == {
        "bat": {"soc": 38, "predictive_target": 37.3}
    }


def test_predictive_target_at_normal_ceiling_is_still_a_charge_block():
    c = _Coord(soc=100, max_soc=100)
    ctrl = _predictive_ctrl(c, 90)

    ChargeDischargeController._refresh_battery_charge_limit_blocks(ctrl)

    assert "max_soc" in ctrl._blocks.get(c, set())
    assert ctrl._predictive_solar_only_batteries == {}
