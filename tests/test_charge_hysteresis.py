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
