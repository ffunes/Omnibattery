"""Measured fleet round-trip efficiency feeding the arbitrage gate / HPS."""
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.energy import (
    effective_round_trip_efficiency,
    measured_battery_efficiency,
)


def lifetime(charge, discharge, capacity=5.12):
    return SimpleNamespace(
        data={
            "total_charging_energy": charge,
            "total_discharging_energy": discharge,
            "battery_total_energy": capacity,
        }
    )


def integrated(charge_eff, discharge_eff, capacity=5.12):
    legs = lambda: (charge_eff, discharge_eff)  # noqa: E731
    sensor = SimpleNamespace(_integrate_mode=True, _leg_efficiencies=legs)
    return SimpleNamespace(
        efficiency_sensor=sensor,
        capabilities=_DUAL_PLANE,
        data={"battery_total_energy": capacity},
    )


_DUAL_PLANE = SimpleNamespace(has_mppt_pv=True, independent_power_planes=True)


def test_dual_plane_without_its_sensor_ignores_the_lifetime_counters():
    """vA/vD lifetime AC counters are not a round trip, even when in range."""
    battery = lifetime(1000, 900)
    battery.capabilities = _DUAL_PLANE
    assert measured_battery_efficiency(battery) is None


def ctrl(coordinators, configured=0.85):
    return SimpleNamespace(round_trip_efficiency=configured, coordinators=coordinators)


def test_configured_value_wins():
    value, source = effective_round_trip_efficiency(
        ctrl([lifetime(1000, 900)], configured=0.9)
    )
    assert (value, source) == (0.9, "configured")


def test_measured_capacity_weighted_average():
    fleet = [lifetime(1000, 900, 5.0), lifetime(1000, 800, 15.0)]
    value, source = effective_round_trip_efficiency(ctrl(fleet))
    assert source == "measured"
    assert value == pytest.approx((0.9 * 5 + 0.8 * 15) / 20)


def test_invalid_batteries_excluded():
    assert measured_battery_efficiency(lifetime(100, 90)) is None  # < 50 cycles
    assert measured_battery_efficiency(lifetime(1000, 990)) is None  # > 0.95
    assert measured_battery_efficiency(lifetime(1000, 600)) is None  # < 0.75
    assert measured_battery_efficiency(integrated(0.9, None)) is None  # one leg
    assert measured_battery_efficiency(integrated(None, 0.9)) is None
    assert measured_battery_efficiency(SimpleNamespace(data={})) is None
    fleet = [lifetime(100, 90), integrated(0.9, 0.9), lifetime(1000, 990)]
    value, source = effective_round_trip_efficiency(ctrl(fleet))
    assert (round(value, 4), source) == (0.81, "measured")


def test_fallback_default():
    assert effective_round_trip_efficiency(ctrl([lifetime(100, 90)])) == (0.85, "default")
    assert effective_round_trip_efficiency(ctrl([])) == (0.85, "default")


def test_consumers_use_it():
    import inspect
    from custom_components.omnibattery.control import high_price_discharge
    from custom_components.omnibattery.pricing import engine

    assert "effective_round_trip_efficiency" in inspect.getsource(high_price_discharge)
    src = inspect.getsource(engine)
    assert "controller.round_trip_efficiency" not in src
    assert src.count("effective_round_trip_efficiency(self._controller)") == 3
