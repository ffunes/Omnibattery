"""Every config-backed controller setting has a known way to reach the plan.

A setting the evaluation reads, changed from the panel, used to leave the old
plan standing until the next scheduled evaluation unless it happened to be in
``predictive_balance_fingerprint()``: the Discharge Reserve, peak shaving and
Vacation Mode among them. Each attribute the controller fills from the config
entry is sorted into one group here, so a new setting must pick one:

- ``FINGERPRINT``: the evaluation reads it; a change re-plans after the settle
  window.
- ``LIVE``: read every control cycle, not part of the plan.
- ``OWN_PATH``: its switch rebuilds the plan itself (Predictive Charging).
- ``RELOAD``: set only in the options flow, which reloads the integration and
  so evaluates again at startup.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController

INIT = Path("custom_components/omnibattery/__init__.py")

# The fingerprint reads the effective margin, which follows the stored one.
_FINGERPRINT_VIA = {"_predictive_safety_margin_stored": "_predictive_safety_margin_kwh"}

FINGERPRINT = {
    "_predictive_safety_margin_stored",
    "_predictive_min_soc_floor",
    "_predictive_min_soc_floor_enabled",
    "max_price_threshold",
    "min_arbitrage_margin",
    "round_trip_efficiency",
    "negative_injection_threshold",
    "predischarge_reserve_soc",
    "weekly_full_charge_enabled",
    "weekly_full_charge_day",
    "discharge_reserve_enabled",
    "capacity_protection_enabled",
    "capacity_protection_limit",
    "max_contracted_power",
    "enable_system_power_limits",
    "system_max_charge_power",
    "system_max_discharge_power",
    "vacation_mode_enabled",
    "charge_delay_enabled",
    "smart_predischarge_enabled",
    "negative_price_charging_enabled",
}

LIVE = {
    "_charge_delay_balance_deadband_kwh",
    "_delay_safety_margin_h",
    "_delay_soc_setpoint",
    "_delay_soc_setpoint_enabled",
    "_min_cycle_interval_s",
    "_no_pd_command_delay",
    "_relay_cooldown_s",
    "_temp_charge_limit_band_c",
    "_temp_charge_limit_c",
    "_temp_charge_limit_floor_pct",
    "_weekly_full_charge_skip_delay",
    "capacity_protection_excluded_devices",
    "capacity_protection_soc_threshold",
    "charge_priority",
    "deadband",
    "direction_hysteresis",
    "discharge_price_threshold",
    "discharge_reserve_min_saving",
    "high_price_discharge_enabled",
    "high_price_discharge_max_power_w",
    "high_price_surplus_export_enabled",
    "hourly_balance_enabled",
    "kd",
    "kp",
    "manual_mode_enabled",
    "max_power_change_per_cycle",
    "min_charge_power",
    "min_discharge_power",
    "no_pd_mode_enabled",
    "offgrid_mode_enabled",
    "predictive_charging_notifications_enabled",
    "primary_battery",
    "primary_feedforward_enabled",
    "surplus_hold_min_saving",
    "surplus_price_hold_enabled",
    "temp_charge_limit_enabled",
    "temp_limit_apply_discharge",
}

OWN_PATH = {
    "predictive_charging_enabled",
    "predictive_charging_overridden",
}

RELOAD = {
    "average_price_sensor",
    "export_price_integration_type",
    "export_price_sensor",
    "meter_inverted",
    "offgrid_meter_inverted",
    "offgrid_power_sensor",
    "predictive_charging_mode",
    "price_integration_type",
    "price_sensor",
    "solar_production_sensor",
    "solar_profile_mode",
    "target_grid_power",
    "zonneplan_export_bonus_enabled",
}


def _config_backed_attributes() -> set[str]:
    """Controller attributes assigned from the config entry's data."""
    tree = ast.parse(INIT.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        source = ast.unparse(node.value)
        if not re.search(r"entry\.data\b", source):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                found.add(target.attr)
    return found


def _ctrl(**overrides):
    base = dict(
        _predictive_safety_margin_kwh=0,
        _predictive_min_soc_floor=0,
        _predictive_min_soc_floor_enabled=0,
        coordinators=[SimpleNamespace(device_key="b1", min_soc=12, max_soc=95)],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _fingerprint(ctrl):
    return ChargeDischargeController.predictive_balance_fingerprint(ctrl)


def test_every_config_setting_is_in_exactly_one_group():
    groups = (FINGERPRINT, LIVE, OWN_PATH, RELOAD)
    for i, group in enumerate(groups):
        for other in groups[i + 1:]:
            assert not group & other, group & other
    attributes = _config_backed_attributes()
    unsorted = attributes - set().union(*groups)
    assert not unsorted, f"sort these into a group in {__file__}: {sorted(unsorted)}"
    stale = set().union(*groups) - attributes
    assert not stale, f"no longer config-backed: {sorted(stale)}"


@pytest.mark.parametrize("name", sorted(FINGERPRINT))
def test_a_planning_setting_moves_the_fingerprint(name):
    field = _FINGERPRINT_VIA.get(name, name)
    ctrl = _ctrl(**{field: 0})
    before = _fingerprint(ctrl)
    setattr(ctrl, field, 1)
    assert _fingerprint(ctrl) != before


@pytest.mark.parametrize("name", sorted(LIVE | OWN_PATH | RELOAD))
def test_any_other_setting_leaves_the_fingerprint(name):
    ctrl = _ctrl(**{name: 0})
    before = _fingerprint(ctrl)
    setattr(ctrl, name, 1)
    assert _fingerprint(ctrl) == before
