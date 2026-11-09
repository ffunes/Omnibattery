"""A paused Indevolt must not be given power that other batteries could deliver.

While the battery is not in Realtime mode (or control is otherwise paused), the
driver reports no headroom: ``dynamic_discharge_limit_w`` and
``dynamic_charge_limit_w`` return 0. The controller's per-battery limit then
reads 0 and battery selection skips it, so the load goes to the others.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control.power_distribution import PowerDistribution
from custom_components.omnibattery.drivers.base import BatteryDriver
from custom_components.omnibattery.drivers.indevolt import IndevoltDriver
from tests.conftest import FakeCoordinator


class _IndevoltCoord(FakeCoordinator):
    """A coordinator whose driver is a real (not connected, so paused) Indevolt."""

    _driver = IndevoltDriver("192.0.2.30")

    @property
    def driver(self):
        return self._driver


def _limit_fn():
    """The real ChargeDischargeController._battery_power_limit, on a stub controller."""
    identity = lambda coordinator, limit: limit  # noqa: E731
    stub = SimpleNamespace(
        _temp_limit_mgr=SimpleNamespace(apply_discharge_limit=identity,
                                        apply_temperature_limit=identity),
        _max_soc_mgr=SimpleNamespace(apply_charge_taper=identity),
        _apply_slot_power_ceiling=lambda coordinator, is_charging, limit: limit,
    )
    return lambda coordinator, is_charging: ChargeDischargeController._battery_power_limit(
        stub, coordinator, is_charging
    )


def _distribution(coords):
    limit = _limit_fn()
    controller = SimpleNamespace(
        previous_power=0,
        coordinators=list(coords),
        _active_charge_batteries=[],
        _active_discharge_batteries=[],
        _battery_power_limit=limit,
        _clamp_to_system_capacity=lambda power, batteries, is_charging: min(
            power, sum(limit(c, is_charging) for c in batteries)
        ),
        _get_available_batteries=lambda is_charging, *a, **k: list(coords),
        _log_power_command_plan=lambda **k: None,
        _phase_power_limiter=None,
    )
    return PowerDistribution(SimpleNamespace(), SimpleNamespace(data={}), controller)


def _pair():
    indevolt = _IndevoltCoord(name="indevolt", max_charge_power=2400, max_discharge_power=2400,
                              data={"battery_soc": 50})
    marstek = FakeCoordinator(name="marstek", max_charge_power=2500, max_discharge_power=2500,
                              data={"battery_soc": 50})
    return indevolt, marstek


def test_a_paused_indevolt_gets_no_charge():
    indevolt, marstek = _pair()
    indevolt.data["battery_soc"] = 10  # lowest SOC: would be charged first
    pd = _distribution([indevolt, marstek])
    assert pd._select_batteries_for_operation(1000, [indevolt, marstek], is_charging=True) == [marstek]


def test_a_paused_indevolt_gets_no_discharge():
    indevolt, marstek = _pair()
    indevolt.data["battery_soc"] = 95  # highest SOC: would be discharged first
    pd = _distribution([indevolt, marstek])
    assert pd._select_batteries_for_operation(1000, [indevolt, marstek], is_charging=False) == [marstek]


def test_other_drivers_keep_their_limits():
    """The hook defaults to None, so every other brand is unchanged."""
    assert BatteryDriver.dynamic_charge_limit_w(None, {}) is None
    limit = _limit_fn()
    marstek = FakeCoordinator(name="m", max_charge_power=2500, max_discharge_power=2000,
                              data={"battery_soc": 50})
    assert limit(marstek, True) == 2500
    assert limit(marstek, False) == 2000


def test_an_owned_indevolt_keeps_its_limits(monkeypatch):
    indevolt, _ = _pair()
    monkeypatch.setattr(IndevoltDriver, "dynamic_charge_limit_w", lambda self, data: None)
    monkeypatch.setattr(IndevoltDriver, "dynamic_discharge_limit_w", lambda self, data: None)
    limit = _limit_fn()
    assert limit(indevolt, True) == 2400
    assert limit(indevolt, False) == 2400
