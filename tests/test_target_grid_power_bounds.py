"""Bounds of the PD Target Grid Power slider (number.omnibattery_pd_target_grid_power).

The slider used to be pinned at -2500..+2500 W regardless of system size, so a
three-battery Venus E system could not be asked to move more than one battery's
worth of power. Its bounds now follow the *configured* system power envelope:

    max = +sum(effective max charge power)      positive = import from grid
    min = -sum(effective max discharge power)   negative = export to grid

narrowed by the optional system-wide cap when that feature is on.

Same lightweight style as ``test_price_threshold_number.py``: ``SimpleNamespace``
stand-ins for hass/entry, no HA runtime. ``MarstekConfigNumberEntity.__init__``
only calls the pure ``system_entity_id`` helper, so direct construction is safe,
and every case asserts through the real properties rather than the bare helper.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    CONFIG_NUMBER_DEFINITIONS,
    CONF_ENABLE_SYSTEM_POWER_LIMITS,
    CONF_SYSTEM_MAX_CHARGE_POWER,
    CONF_SYSTEM_MAX_DISCHARGE_POWER,
    CONF_TARGET_GRID_POWER,
    DYNAMIC_BOUNDS_SYSTEM_POWER,
    DYNAMIC_BOUNDS_SYSTEM_POWER_CAP,
    effective_battery_power_limits,
    effective_system_power,
    total_battery_power,
)
from custom_components.omnibattery.number import MarstekConfigNumberEntity

TARGET_DEF = next(
    d for d in CONFIG_NUMBER_DEFINITIONS if d["key"] == CONF_TARGET_GRID_POWER
)
SYSTEM_CHARGE_CAP_DEF = next(
    d for d in CONFIG_NUMBER_DEFINITIONS if d["key"] == CONF_SYSTEM_MAX_CHARGE_POWER
)
SYSTEM_DISCHARGE_CAP_DEF = next(
    d for d in CONFIG_NUMBER_DEFINITIONS if d["key"] == CONF_SYSTEM_MAX_DISCHARGE_POWER
)
STATIC_MIN, STATIC_MAX = TARGET_DEF["min"], TARGET_DEF["max"]


def _bat(charge, discharge):
    return {"max_charge_power": charge, "max_discharge_power": discharge}


def _entity(data, definition=TARGET_DEF):
    return MarstekConfigNumberEntity(
        SimpleNamespace(), SimpleNamespace(data=data), definition
    )


def _bounds(data, definition=TARGET_DEF):
    entity = _entity(data, definition)
    return entity.native_min_value, entity.native_max_value


def test_bounds_scale_with_battery_count():
    """The headline case: three Venus E v3 reach +/-7500 W, not +/-2500 W."""
    assert _bounds({"batteries": [_bat(2500, 2500)] * 3}) == (-7500, 7500)


def test_bounds_scale_to_ten_batteries():
    """Ten 2500 W batteries expose the full aggregate +/-25000 W range."""
    assert _bounds({"batteries": [_bat(2500, 2500)] * 10}) == (-25000, 25000)


def test_system_cap_bounds_follow_configured_directional_totals():
    """System cap sliders use the configured battery ceilings, not 2500 W."""
    data = {"batteries": [_bat(4000, 3500), _bat(2500, 4000)]}

    assert _bounds(data, SYSTEM_CHARGE_CAP_DEF) == (0, 6500)
    assert _bounds(data, SYSTEM_DISCHARGE_CAP_DEF) == (0, 7500)


def test_single_battery_matches_legacy_range():
    """No regression for the common single-battery install."""
    assert _bounds({"batteries": [_bat(2500, 2500)]}) == (-2500, 2500)


def test_bounds_are_asymmetric():
    """Brands with unequal ceilings (Sessy 2200/1700) bound each direction on its own."""
    assert _bounds({"batteries": [_bat(2200, 1700)] * 2}) == (-3400, 4400)


def test_soft_limits_are_used_for_the_effective_system_envelope():
    """Soft-max drivers must contribute their user ceiling, not only hardware cap."""
    battery = _bat(2400, 2400)
    battery.update(user_max_charge_power=600, user_max_discharge_power=700)

    assert effective_battery_power_limits(battery) == (600, 700)
    assert total_battery_power({"batteries": [battery]}) == (600, 700)
    assert _bounds({"batteries": [battery]}) == (-700, 600)


def test_missing_soft_limit_keeps_the_configured_limit():
    """Legacy entries without one of the optional soft caps remain compatible."""
    battery = _bat(2400, 2400)
    battery["user_max_charge_power"] = 600

    assert effective_battery_power_limits(battery) == (600, 2400)
    assert _bounds({"batteries": [battery]}) == (-2400, 600)


def test_normalized_power_limits_are_used_when_present():
    battery = {
        "device_max_charge_power": 2400,
        "device_max_discharge_power": 1800,
        "configured_max_charge_power": 600,
        "configured_max_discharge_power": 2200,
    }

    assert effective_battery_power_limits(battery) == (600, 1800)
    assert _bounds({"batteries": [battery]}) == (-1800, 600)


def test_system_cap_narrows_each_direction_independently():
    """A cap of 0 means "disabled" for that direction, mirroring _configured_system_limit."""
    data = {
        "batteries": [_bat(2500, 2500)] * 3,
        CONF_ENABLE_SYSTEM_POWER_LIMITS: True,
        CONF_SYSTEM_MAX_CHARGE_POWER: 5000,
        CONF_SYSTEM_MAX_DISCHARGE_POWER: 0,
    }
    assert _bounds(data) == (-7500, 5000)


def test_system_cap_ignored_when_switch_off():
    data = {
        "batteries": [_bat(2500, 2500)] * 3,
        CONF_ENABLE_SYSTEM_POWER_LIMITS: False,
        CONF_SYSTEM_MAX_CHARGE_POWER: 5000,
        CONF_SYSTEM_MAX_DISCHARGE_POWER: 5000,
    }
    assert _bounds(data) == (-7500, 7500)


def test_system_cap_cannot_widen():
    """The cap only ever narrows the per-battery sum."""
    data = {
        "batteries": [_bat(2500, 2500)],
        CONF_ENABLE_SYSTEM_POWER_LIMITS: True,
        CONF_SYSTEM_MAX_CHARGE_POWER: 15000,
        CONF_SYSTEM_MAX_DISCHARGE_POWER: 15000,
    }
    assert _bounds(data) == (-2500, 2500)


def test_legacy_entry_without_enable_key_derives_from_cap_values():
    """Entries predating the enable key had the feature on iff a cap was set."""
    data = {
        "batteries": [_bat(2500, 2500)] * 3,
        CONF_SYSTEM_MAX_CHARGE_POWER: 4000,
    }
    assert _bounds(data) == (-7500, 4000)


@pytest.mark.parametrize(
    "data",
    [{}, {"batteries": []}, {"batteries": [{}]}],
    ids=["no-key", "empty-list", "battery-without-limits"],
)
def test_missing_or_empty_batteries_fall_back_to_authored_range(data):
    assert _bounds(data) == (STATIC_MIN, STATIC_MAX)


def test_zero_configured_power_falls_back_per_direction():
    """Fallback is per-direction, so one zeroed direction can't collapse the slider."""
    assert _bounds({"batteries": [_bat(0, 1500)]}) == (-1500, STATIC_MAX)
    assert _bounds({"batteries": [_bat(1500, 0)]}) == (STATIC_MIN, 1500)


def test_bounds_snap_down_to_the_step_grid():
    """Flooring keeps every advertised bound reachable and never over-advertises."""
    low, high = _bounds({"batteries": [_bat(1005, 995)]})
    assert (low, high) == (-990, 1000)
    step = TARGET_DEF["step"]
    assert low % step == 0 and high % step == 0
    assert abs(low) <= 995 and high <= 1005


def test_stored_value_outside_narrowed_range_is_reported_unchanged():
    """A narrowed range must not silently rewrite the stored setpoint.

    HA validates on the ``number.set_value`` service, not on the reported state,
    and the controller already clamps the resulting battery request downstream.
    Clamping here would desync the state from config_entry.data.
    """
    entity = _entity({"batteries": [_bat(1500, 1500)], CONF_TARGET_GRID_POWER: 2500})
    assert entity.native_value == 2500
    assert entity.native_max_value == 1500


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"batteries": []},
        {"batteries": [{}]},
        {"batteries": [_bat(0, 1500)]},
        {"batteries": [_bat(1500, 0)]},
        {"batteries": [_bat(5, 5)]},  # both sums floor below one step
    ],
)
def test_min_is_always_below_max(data):
    """HA needs min < max, and the panel divides by (max - min)."""
    low, high = _bounds(data)
    assert low < high


@pytest.mark.parametrize(
    "definition",
    [d for d in CONFIG_NUMBER_DEFINITIONS if not d.get("dynamic_bounds")],
    ids=lambda d: d["key"],
)
def test_other_config_numbers_keep_their_authored_bounds(definition):
    """The generic path is untouched, including the scale: 60 charge-delay entry."""
    data = {"batteries": [_bat(2500, 2500)] * 3}
    assert _bounds(data, definition) == (definition["min"], definition["max"])


def test_dynamic_power_bounds_are_marked_for_target_and_system_caps():
    marked = [d for d in CONFIG_NUMBER_DEFINITIONS if d.get("dynamic_bounds")]
    assert {d["key"] for d in marked} == {
        CONF_TARGET_GRID_POWER,
        CONF_SYSTEM_MAX_CHARGE_POWER,
        CONF_SYSTEM_MAX_DISCHARGE_POWER,
    }
    assert TARGET_DEF["dynamic_bounds"] == DYNAMIC_BOUNDS_SYSTEM_POWER
    assert (
        SYSTEM_CHARGE_CAP_DEF["dynamic_bounds"] == DYNAMIC_BOUNDS_SYSTEM_POWER_CAP
    )
    assert (
        SYSTEM_DISCHARGE_CAP_DEF["dynamic_bounds"]
        == DYNAMIC_BOUNDS_SYSTEM_POWER_CAP
    )


def test_effective_power_is_the_battery_sum_then_the_cap():
    """Pins the two-stage composition: sum the per-battery limits, then narrow."""
    data = {
        "batteries": [_bat(2500, 2500), _bat(1500, 1500), {}],
        CONF_ENABLE_SYSTEM_POWER_LIMITS: True,
        CONF_SYSTEM_MAX_CHARGE_POWER: 3000,
        CONF_SYSTEM_MAX_DISCHARGE_POWER: 0,
    }
    batteries = data["batteries"]
    total_charge = sum(b.get("max_charge_power", 0) or 0 for b in batteries)
    total_discharge = sum(b.get("max_discharge_power", 0) or 0 for b in batteries)
    assert total_battery_power(data) == (total_charge, total_discharge)
    assert effective_system_power(data) == (
        min(total_charge, data[CONF_SYSTEM_MAX_CHARGE_POWER]),
        total_discharge,  # cap of 0 leaves this direction uncapped
    )
