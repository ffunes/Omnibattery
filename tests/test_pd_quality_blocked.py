"""Tests for the PD control-quality verdict while the loop cannot act.

Regression: with charge delay active and a 2.7 kW solar surplus the battery sat
idle and the sensor reported "sluggish" on the most aggressive profile. The
cycle's restriction check keys on the *commanded* power, which a previous
blocked cycle already zeroed; the direction check then asked "may we discharge?"
(yes), so the loop counted as active and fed the export error into the quality
metric as if it were a tuning fault.

The demand direction comes from the error sign instead, the metric skips blocked
cycles, and the sensor reports "blocked". A verdict that stopped advancing hours
ago is reported as "collecting_data" rather than presented as live.

Convention: error > 0 = grid import (needs discharge), error < 0 = export
(needs charge).
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.sensors.aggregate_sensors import PdControlQualitySensor


def _ctrl(*, charge_blocked=False, discharge_blocked=False, deadband=40):
    return SimpleNamespace(
        deadband=deadband,
        is_charge_blocked=lambda: charge_blocked,
        is_discharge_blocked=lambda: discharge_blocked,
        _is_operation_allowed=ChargeDischargeController._is_operation_allowed,
    )


def _demand_blocked(ctrl, error, commanded_power=0):
    # _is_operation_allowed is unbound on the stub, so bind both by hand.
    ctrl._is_operation_allowed = lambda is_charging: ChargeDischargeController._is_operation_allowed(
        ctrl, is_charging
    )
    return ChargeDischargeController._pd_demand_blocked(ctrl, error, commanded_power)


def test_charge_demand_blocked_while_output_is_zero():
    # The live case: charge delay active, 2690W export, previous cycle left the
    # command at 0W. The demand is charge, charging is blocked -> blocked.
    ctrl = _ctrl(charge_blocked=True)
    assert _demand_blocked(ctrl, error=-2690) is True


def test_charge_demand_not_blocked_when_charging_allowed():
    ctrl = _ctrl(charge_blocked=False)
    assert _demand_blocked(ctrl, error=-2690) is False


def test_discharge_demand_uses_discharge_gate():
    # Import with discharge blocked (price/EV) is blocked; a blocked charge gate
    # must not leak into the discharge verdict.
    assert _demand_blocked(_ctrl(discharge_blocked=True), error=500) is True
    assert _demand_blocked(_ctrl(charge_blocked=True), error=500) is False


def test_inside_deadband_is_never_blocked():
    # No demand, nothing to block - the loop is simply at target.
    ctrl = _ctrl(charge_blocked=True, deadband=40)
    assert _demand_blocked(ctrl, error=-30) is False


def test_reducing_discharge_is_not_blocked():
    # Discharging 400W into house load with charging blocked; a cloud gap pushes
    # the grid to 200W export. The answer is "discharge less", which the loop may
    # do -> the tuning verdict stays valid.
    ctrl = _ctrl(charge_blocked=True)
    assert _demand_blocked(ctrl, error=-200, commanded_power=-400) is False


def test_reducing_charge_is_not_blocked():
    # Mirror case: charging 400W with discharging blocked, grid imports 200W.
    ctrl = _ctrl(discharge_blocked=True)
    assert _demand_blocked(ctrl, error=200, commanded_power=400) is False


def test_running_into_the_blocked_direction_is_blocked():
    # No headroom left: already charging while charging is blocked and the export
    # demands more charge.
    ctrl = _ctrl(charge_blocked=True)
    assert _demand_blocked(ctrl, error=-2690, commanded_power=400) is True


class _MetricCtrl:
    """Minimal controller carrying only the quality-metric state."""

    def __init__(self):
        self.deadband = 40
        self._pd_quality_tau = 60.0
        self._pd_quality_rms_ema = None
        self._pd_quality_osc_ema = 0.0
        self._pd_quality_last_ts = None
        self._pd_quality_last_advance_ts = None
        self._pd_quality_step_grace_s = 10.0
        self._pd_quality_settle_until = 0.0
        self._pd_quality_prev_target = 0.0

    def update(self, error, *, blocked):
        ChargeDischargeController._update_pd_quality_metrics(
            self, error, False, 0.0, blocked
        )

    @property
    def rms(self):
        return ChargeDischargeController.pd_quality_rms_error.fget(self)

    @property
    def age(self):
        return ChargeDischargeController.pd_quality_age_s.fget(self)


def test_blocked_cycles_do_not_inflate_the_metric():
    c = _MetricCtrl()
    c.update(-50, blocked=False)      # seeds the EMA
    c.update(-50, blocked=False)
    seeded = c.rms
    for _ in range(20):
        c.update(-2690, blocked=True)  # charge delay + solar surplus
    assert c.rms == seeded


def test_metric_age_tracks_last_advance():
    c = _MetricCtrl()
    assert c.age is None
    c.update(-50, blocked=False)
    assert c.age is not None and c.age < 1.0


def test_age_grows_across_skipped_cycles():
    # Skipped cycles bump the EMA anchor so the metric resumes smoothly; the age
    # must keep measuring how old the numbers are, else the staleness guard can
    # never fire during exactly the long block it was written for.
    c = _MetricCtrl()
    c.update(-50, blocked=False)
    c.update(-50, blocked=False)
    c._pd_quality_last_advance_ts -= 3600.0  # pretend the advance was an hour ago
    for _ in range(20):
        c.update(-2690, blocked=True)
    assert c.age > 3599.0


def _sensor(controller):
    sensor = PdControlQualitySensor.__new__(PdControlQualitySensor)
    sensor._controller = controller
    return sensor


def _quality_ctrl(**kwargs):
    defaults = dict(
        no_pd_mode_enabled=False,
        pd_blocked=False,
        pd_limited=False,
        pd_quality_rms_error=2687.0,
        pd_quality_oscillation_per_min=0.0,
        pd_quality_age_s=1.0,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_sensor_reports_blocked_over_a_tuning_verdict():
    # The exact regression: high RMS, no oscillation, loop muzzled. Without the
    # blocked check this reads "sluggish" on the most aggressive profile.
    sensor = _sensor(_quality_ctrl(pd_blocked=True))
    assert sensor.native_value == "blocked"


def test_sensor_still_reports_sluggish_when_free_to_act():
    sensor = _sensor(_quality_ctrl())
    assert sensor.native_value == "sluggish"


def test_blocked_outranks_battery_limited():
    sensor = _sensor(_quality_ctrl(pd_blocked=True, pd_limited=True))
    assert sensor.native_value == "blocked"


def test_stale_metric_is_not_presented_as_a_live_verdict():
    sensor = _sensor(_quality_ctrl(pd_quality_age_s=2 * 3600.0))
    assert sensor.native_value == "collecting_data"


def test_blocked_is_a_declared_option():
    assert "blocked" in PdControlQualitySensor._STATES


def test_flags_expire_instead_of_latching():
    """Both flags are written only in the PD tail, which many cycles never reach
    (weekly full charge or predictive charging owning the cycle, max SOC handling,
    manual mode). Clearing them at the top of every cycle erased a verdict that
    was still true, and never clearing them latched a stale one for the whole
    session. They are stamped when set and expire on their own instead.
    """
    ctrl = SimpleNamespace(
        _pd_flag_ttl_s=60.0,
        _pd_blocked=False,
        _pd_blocked_ts=None,
    )
    ctrl._pd_flag_live = lambda value, ts: ChargeDischargeController._pd_flag_live(ctrl, value, ts)

    def live():
        return ChargeDischargeController.pd_blocked.fget(ctrl)

    assert live() is False
    ChargeDischargeController._set_pd_blocked(ctrl, True)
    assert live() is True

    ctrl._pd_blocked_ts -= 30.0  # a cycle that skipped the tail 30s ago
    assert live() is True

    ctrl._pd_blocked_ts -= 40.0  # 70s: nothing confirmed it, so it lapses
    assert live() is False

    ChargeDischargeController._set_pd_blocked(ctrl, True)
    ChargeDischargeController._set_pd_blocked(ctrl, False)
    assert ctrl._pd_blocked_ts is None
    assert live() is False


if __name__ == "__main__":
    test_charge_demand_blocked_while_output_is_zero()
    test_charge_demand_not_blocked_when_charging_allowed()
    test_discharge_demand_uses_discharge_gate()
    test_inside_deadband_is_never_blocked()
    test_reducing_discharge_is_not_blocked()
    test_reducing_charge_is_not_blocked()
    test_running_into_the_blocked_direction_is_blocked()
    test_blocked_cycles_do_not_inflate_the_metric()
    test_metric_age_tracks_last_advance()
    test_age_grows_across_skipped_cycles()
    test_sensor_reports_blocked_over_a_tuning_verdict()
    test_sensor_still_reports_sluggish_when_free_to_act()
    test_blocked_outranks_battery_limited()
    test_stale_metric_is_not_presented_as_a_live_verdict()
    test_blocked_is_a_declared_option()
    test_flags_expire_instead_of_latching()
    print("ok")
