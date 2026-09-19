"""Tests for contracted-power protection of excluded devices."""
from __future__ import annotations

from custom_components.omnibattery import ChargeDischargeController


def _controller(*, contracted_power: float, excluded_adjustment: float):
    ctrl = object.__new__(ChargeDischargeController)
    ctrl.max_contracted_power = contracted_power
    ctrl._excluded_included_adjustment = excluded_adjustment
    ctrl._icp_excluded_protection_w = 0.0
    return ctrl


def test_engages_above_contracted_power():
    ctrl = _controller(contracted_power=5750, excluded_adjustment=6500)

    sensor = ctrl._apply_icp_excluded_protection(
        sensor_filtered=7000, sensor_actual=500, active_target=0
    )

    assert sensor == 1250
    assert ctrl._icp_excluded_protection_w == 750


def test_does_not_engage_at_or_below_contracted_power():
    ctrl = _controller(contracted_power=5750, excluded_adjustment=4000)

    sensor = ctrl._apply_icp_excluded_protection(
        sensor_filtered=4500, sensor_actual=500, active_target=0
    )

    assert sensor == 500
    assert ctrl._icp_excluded_protection_w == 0


def test_does_not_double_count_peak_shaving_add_back():
    ctrl = _controller(contracted_power=5750, excluded_adjustment=6500)

    sensor = ctrl._apply_icp_excluded_protection(
        sensor_filtered=7000, sensor_actual=7000, active_target=0
    )

    assert sensor == 7000
    assert ctrl._icp_excluded_protection_w == 0


def test_ignores_import_without_excluded_adjustment():
    ctrl = _controller(contracted_power=5750, excluded_adjustment=0)

    sensor = ctrl._apply_icp_excluded_protection(
        sensor_filtered=9000, sensor_actual=500, active_target=0
    )

    assert sensor == 500
    assert ctrl._icp_excluded_protection_w == 0


def test_clamps_excess_to_excluded_adjustment():
    ctrl = _controller(contracted_power=5750, excluded_adjustment=1000)

    sensor = ctrl._apply_icp_excluded_protection(
        sensor_filtered=7500, sensor_actual=500, active_target=0
    )

    assert sensor == 1500
    assert ctrl._icp_excluded_protection_w == 1000


def test_zero_contracted_power_disables_protection():
    ctrl = _controller(contracted_power=0, excluded_adjustment=6500)

    sensor = ctrl._apply_icp_excluded_protection(
        sensor_filtered=7000, sensor_actual=500, active_target=0
    )

    assert sensor == 500
    assert ctrl._icp_excluded_protection_w == 0
