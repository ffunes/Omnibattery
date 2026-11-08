"""Regression tests for peak-shaving conservation decisions."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import (
    CONFIG_NUMBER_DEFINITIONS,
    CONF_CAPACITY_PROTECTION_LIMIT,
)


def test_peak_shaving_limit_supports_20_kw():
    definition = next(
        item for item in CONFIG_NUMBER_DEFINITIONS
        if item["key"] == CONF_CAPACITY_PROTECTION_LIMIT
    )

    assert definition["max"] == 20000
    assert definition["step"] == 100


def _controller(*, previous_power: float):
    ctrl = object.__new__(ChargeDischargeController)
    ctrl.capacity_protection_enabled = True
    ctrl.capacity_protection_excluded_devices = False
    ctrl.capacity_protection_soc_threshold = 25
    ctrl.capacity_protection_limit = 3000
    ctrl.coordinators = [SimpleNamespace(data={"battery_soc": 24})]
    ctrl._non_responsive = SimpleNamespace(is_excluded=lambda _c: False)
    ctrl.previous_power = previous_power
    ctrl._excluded_included_adjustment = 0.0
    ctrl._setpoint_offsets = {"user_target": 0.0}
    ctrl._setpoint_overrides = {}
    ctrl._capacity_protection_active = False
    ctrl._capacity_protection_force_idle = False
    ctrl._capacity_protection_status = {}
    return ctrl


def _non_responsive(*excluded):
    """Tracker stub reporting exactly ``excluded`` as non-responsive."""
    return SimpleNamespace(is_excluded=lambda coordinator: coordinator in excluded)


@pytest.mark.parametrize("previous_power", [10.1, -500.0])
def test_conserving_stops_any_existing_battery_command(previous_power):
    ctrl = _controller(previous_power=previous_power)
    grid_power = 321.4 + previous_power

    target, sensor = ctrl._apply_capacity_protection(grid_power, active_target=0.0)

    assert sensor == pytest.approx(grid_power)
    assert target == pytest.approx(grid_power)
    assert ctrl._capacity_protection_force_idle is True
    assert ctrl._capacity_protection_status["action"] == "conserving"


def test_conserving_idle_does_not_request_redundant_stop():
    ctrl = _controller(previous_power=0.0)

    ctrl._apply_capacity_protection(321.4, active_target=0.0)

    assert ctrl._capacity_protection_force_idle is False
    assert ctrl._capacity_protection_status["action"] == "conserving"


def test_solar_surplus_charge_remains_allowed():
    ctrl = _controller(previous_power=10.1)
    grid_power = -89.9

    target, sensor = ctrl._apply_capacity_protection(grid_power, active_target=0.0)

    assert sensor == pytest.approx(grid_power)
    assert target == 0.0
    assert ctrl._capacity_protection_force_idle is False
    assert ctrl._capacity_protection_status["action"] == "charging"


def test_excluded_devices_switch_is_backward_compatible_when_off():
    ctrl = _controller(previous_power=0.0)
    ctrl.coordinators = [SimpleNamespace(data={"battery_soc": 80})]
    ctrl._excluded_included_adjustment = 4000.0

    target, sensor = ctrl._apply_capacity_protection(
        1000.0, active_target=0.0
    )

    assert target == 0.0
    assert sensor == 1000.0
    assert ctrl._capacity_protection_active is False
    assert ctrl._capacity_protection_status["action"] == "idle"


def test_excluded_devices_switch_covers_only_demand_above_peak_limit():
    ctrl = _controller(previous_power=0.0)
    ctrl.coordinators = [SimpleNamespace(data={"battery_soc": 80})]
    ctrl.capacity_protection_excluded_devices = True
    ctrl._excluded_included_adjustment = 4000.0

    target, sensor = ctrl._apply_capacity_protection(
        1000.0, active_target=0.0
    )

    assert target == 0.0
    assert sensor == 2000.0
    assert ctrl._capacity_protection_active is True
    assert ctrl._capacity_protection_status["action"] == "shaving_excluded"
    assert ctrl._capacity_protection_status["excluded_peak_excess"] == 1000


def test_excluded_devices_switch_preserves_normal_home_coverage():
    ctrl = _controller(previous_power=-2000.0)
    ctrl.coordinators = [SimpleNamespace(data={"battery_soc": 80})]
    ctrl.capacity_protection_excluded_devices = True
    ctrl._excluded_included_adjustment = 4000.0

    target, sensor = ctrl._apply_capacity_protection(
        -1000.0, active_target=0.0
    )

    assert target == 0.0
    assert sensor == 0.0
    assert ctrl._capacity_protection_status["action"] == "shaving_excluded"


def test_excluded_devices_below_peak_limit_keep_normal_exclusion():
    ctrl = _controller(previous_power=0.0)
    ctrl.coordinators = [SimpleNamespace(data={"battery_soc": 80})]
    ctrl.capacity_protection_excluded_devices = True
    ctrl._excluded_included_adjustment = 2500.0

    target, sensor = ctrl._apply_capacity_protection(
        1000.0, active_target=0.0
    )

    assert target == 0.0
    assert sensor == 1000.0
    assert ctrl._capacity_protection_active is False


# --- Issue #803: a non-responsive battery's stale SOC must not be averaged in ---

# House load of 321.4 W with a battery discharging 500 W: below the peak limit
# but above the normal target, so peak shaving holds the fleet instead of
# discharging it (the "conserving" action).
STALE_SOC_HOUSE_LOAD = -178.6
STALE_SOC_PREVIOUS_POWER = -500.0


def _stale_soc_fleet(*, threshold: float = 35):
    """Two batteries: one unreachable at 89%, one live at 12% (#803)."""
    ctrl = _controller(previous_power=STALE_SOC_PREVIOUS_POWER)
    ctrl.capacity_protection_soc_threshold = threshold
    dead = SimpleNamespace(data={"battery_soc": 89})
    live = SimpleNamespace(data={"battery_soc": 12})
    ctrl.coordinators = [dead, live]
    return ctrl, dead, live


def test_nonresponsive_battery_stale_soc_is_left_out_of_the_average():
    # Before the fix avg = (89 + 12) / 2 = 50.5 >= 35, so peak shaving stayed
    # idle while the remaining battery discharged to its 12% minimum.
    ctrl, dead, _live = _stale_soc_fleet()
    ctrl._non_responsive = _non_responsive(dead)

    ctrl._apply_capacity_protection(
        STALE_SOC_HOUSE_LOAD, active_target=0.0
    )

    assert ctrl._capacity_protection_status["avg_soc"] == 12.0
    assert ctrl._capacity_protection_status["action"] == "conserving"
    assert ctrl._capacity_protection_active is True


def test_counting_the_stale_soc_keeps_peak_shaving_idle():
    # Control for the test above: with the dead battery still in the average
    # (the pre-#803 behaviour) nothing holds the remaining battery.
    ctrl, _dead, _live = _stale_soc_fleet()

    ctrl._apply_capacity_protection(
        STALE_SOC_HOUSE_LOAD, active_target=0.0
    )

    assert ctrl._capacity_protection_status["avg_soc"] == 50.5
    assert ctrl._capacity_protection_status["action"] == "idle"
    assert ctrl._capacity_protection_active is False


def test_soc_limited_check_ignores_nonresponsive_stale_soc():
    ctrl, dead, _live = _stale_soc_fleet()
    ctrl._non_responsive = _non_responsive(dead)

    assert ctrl._is_capacity_protection_soc_limited() is True

    # Same fleet without the exclusion: the stale 89% hides the 12% SOC.
    ctrl._non_responsive = _non_responsive()
    assert ctrl._is_capacity_protection_soc_limited() is False


def test_unavailable_battery_stale_soc_is_left_out_of_the_average():
    # An unreachable battery that never entered non-responsive cooldown still
    # keeps its last SOC; availability alone excludes it, as in the control path.
    ctrl = _controller(previous_power=STALE_SOC_PREVIOUS_POWER)
    ctrl.capacity_protection_soc_threshold = 35
    ctrl.coordinators = [
        SimpleNamespace(data={"battery_soc": 89}, is_available=False),
        SimpleNamespace(data={"battery_soc": 12}),
    ]

    ctrl._apply_capacity_protection(
        STALE_SOC_HOUSE_LOAD, active_target=0.0
    )

    assert ctrl._capacity_protection_status["avg_soc"] == 12.0
    assert ctrl._capacity_protection_status["action"] == "conserving"


def test_peak_shaving_stays_idle_when_no_battery_can_respond():
    ctrl, dead, live = _stale_soc_fleet()
    ctrl._non_responsive = _non_responsive(dead, live)

    ctrl._apply_capacity_protection(
        STALE_SOC_HOUSE_LOAD, active_target=0.0
    )

    assert ctrl._capacity_protection_status["avg_soc"] == 100
    assert ctrl._capacity_protection_status["action"] == "idle"
    assert ctrl._capacity_protection_active is False
    assert ctrl._is_capacity_protection_soc_limited() is False


def test_responsive_fleet_still_averages_every_battery():
    # Guard against over-exclusion: two reachable batteries keep averaging to
    # the simple mean, so a high-SOC partner still lifts the fleet average.
    ctrl = _controller(previous_power=STALE_SOC_PREVIOUS_POWER)
    ctrl.capacity_protection_soc_threshold = 35
    ctrl.coordinators = [
        SimpleNamespace(data={"battery_soc": 89}),
        SimpleNamespace(data={"battery_soc": 12}),
    ]

    ctrl._apply_capacity_protection(
        STALE_SOC_HOUSE_LOAD, active_target=0.0
    )

    assert ctrl._capacity_protection_status["avg_soc"] == 50.5
    assert ctrl._capacity_protection_status["action"] == "idle"
    assert ctrl._is_capacity_protection_soc_limited() is False

