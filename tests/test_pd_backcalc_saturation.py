"""Tests for ``_backcalc_is_saturated`` — the PD anti-windup re-anchor gate.

The incremental PD re-anchors its command base to measured AC power when the
batteries under-deliver. That is only correct on REAL saturation (SOC/taper/
blocker/cap). A slow MQTT/HTTP actuator (Zendure) ramps over seconds, and that
ramp lag must NOT be read as saturation, or the base is yanked down to the
lagging measurement before the device reaches the cap. This gate distinguishes
the two: ``True`` only when no active battery has headroom below its own limit.

The method is exercised unbound with light stubs, so no full controller is built.
``ac_power`` convention is + discharge / - charge.
"""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.marstek_venus_energy_manager import ChargeDischargeController


def _coord(ac_power, limit=1250, blocked=False):
    return SimpleNamespace(data={"ac_power": ac_power}, limit=limit, blocked=blocked)


def _zendure(battery_power, limit=1250, blocked=False):
    """Registerless driver: no ac_power, only synthesised battery_power (+charge)."""
    return SimpleNamespace(
        data={"battery_power": battery_power}, limit=limit, blocked=blocked
    )


def _ctrl(coords):
    return SimpleNamespace(
        coordinators=coords,
        saturation_backcalc_threshold=150.0,
        is_charge_blocked=lambda c: c.blocked,
        is_discharge_blocked=lambda c: c.blocked,
        _battery_power_limit=lambda c, is_charging: c.limit,
        _coordinator_delivered_power=ChargeDischargeController._coordinator_delivered_power,
    )


def _saturated(coords, is_charging=True):
    return ChargeDischargeController._backcalc_is_saturated(_ctrl(coords), is_charging)


def test_ramp_lag_not_saturated():
    # Charging 400W but cap is 1250W and unblocked → headroom → ramp lag.
    assert _saturated([_coord(-400, limit=1250)]) is False


def test_at_cap_is_saturated():
    # Delivering near the cap leaves no headroom → genuine saturation.
    assert _saturated([_coord(-1200, limit=1250)]) is True


def test_blocked_battery_is_saturated():
    # A blocked battery cannot give more; it is not "headroom".
    assert _saturated([_coord(-400, limit=1250, blocked=True)]) is True


def test_unknown_delivery_not_saturated():
    # No ac_power and no battery_power: cannot prove saturation → assume ramp lag.
    coord = SimpleNamespace(data={"ac_power": None}, limit=1250, blocked=False)
    assert _saturated([coord]) is False


def test_zendure_at_cap_is_saturated():
    # Registerless driver: battery_power only. At cap → no headroom → saturated.
    assert _saturated([_zendure(1250, limit=1250)]) is True


def test_zendure_ramp_lag_not_saturated():
    # Registerless driver still ramping below cap → headroom → not saturated.
    assert _saturated([_zendure(400, limit=1250)]) is False


def test_mixed_blocked_plus_headroom_not_saturated():
    # Marstek blocked (max_soc) + Zendure still ramping below cap → not saturated.
    coords = [_coord(0, limit=2500, blocked=True), _coord(-400, limit=1250)]
    assert _saturated(coords) is False


def test_mixed_blocked_plus_at_cap_is_saturated():
    coords = [_coord(0, limit=2500, blocked=True), _coord(-1200, limit=1250)]
    assert _saturated(coords) is True


def test_discharge_direction_headroom_not_saturated():
    # ac_power positive = discharging; 400W out of a 1250W cap → headroom.
    assert _saturated([_coord(400, limit=1250)], is_charging=False) is False


def _measured(coords):
    return ChargeDischargeController._measured_battery_power(
        SimpleNamespace(
            coordinators=coords,
            _coordinator_delivered_power=ChargeDischargeController._coordinator_delivered_power,
        )
    )


def test_measured_includes_zendure_via_battery_power():
    # Regression: Zendure has no ac_power. The controller must still see its
    # charge (battery_power, + charge) instead of reading it as 0 W.
    # Marstek idle (ac_power 0, + discharge/- charge) + Zendure charging 1250.
    assert _measured([_coord(0), _zendure(1250)]) == 1250.0


def test_measured_none_when_no_battery_reports():
    coord = SimpleNamespace(data={})
    assert _measured([coord]) is None
