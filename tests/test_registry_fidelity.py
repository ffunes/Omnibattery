"""Fidelity of the registry refactor for brands the registry does not know and
for stale keys left behind by a brand change (plan section 8).

Before the refactor the coordinator compared the stored ``brand`` literally, so
a brand that falls back to the default (Marstek) driver got none of Marstek's
brand-gated syncs, and the setup grouping split on ``serial_port`` for every brand.
"""
from __future__ import annotations

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.omnibattery.drivers.registry import DRIVERS
from tests.test_driver_construction_characterization import MARSTEK, ZENDURE, _build_coordinator


@pytest.fixture(autouse=True)
def _no_ha_runtime(monkeypatch):
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_a, **_k: None)


async def test_unknown_brand_on_vd_is_not_clamped_at_init():
    config = {**MARSTEK, "brand": "acme", "battery_version": "vD",
              "device_max_charge_power": 2500, "device_max_discharge_power": 2500}
    coordinator = _build_coordinator(config)
    assert coordinator.device_max_charge_power == 2500
    assert coordinator.device_max_discharge_power == 2500


async def test_unknown_brand_on_vd_does_not_follow_the_ems_ceiling():
    config = {**MARSTEK, "brand": "acme", "battery_version": "vD",
              "device_max_charge_power": 2500, "device_max_discharge_power": 2500}
    coordinator = _build_coordinator(config)
    coordinator.data = {"ems_version": 999}
    coordinator._reconcile_driver("poll")
    assert coordinator.device_max_charge_power == 2500
    assert coordinator.ems_version is None


@pytest.mark.parametrize("brand", list(DRIVERS))
def test_a_stored_serial_port_is_its_own_setup_link_only_for_marstek(brand):
    # fixed: plan section 8 #11 #12 #14 (T1.1): a stale serial_port on another brand is ignored.
    cls = DRIVERS[brand]
    base = {**ZENDURE, "brand": brand}
    same = cls.link_key(base) == cls.link_key({**base, "serial_port": "/dev/ttyUSB0"})
    assert same is (brand != "marstek")


async def test_sessy_weekly_hours_still_book_a_taper_tail():
    """Why D3 was not applied (plan header and section 8): a Sessy coordinator
    keeps its stored taper flag (True by default), and the weekly planner reads
    it without checking for cell voltages, so it books the last 5 % at 200 W.
    Same numbers as test_the_voltage_taper_charges_the_last_percent_at_200_w."""
    from types import SimpleNamespace

    from custom_components.omnibattery.pricing.engine import PricingManager
    from tests.test_driver_construction_characterization import SESSY

    config = {k: v for k, v in SESSY.items() if k != "full_charge_voltage_taper_enabled"}
    coordinator = _build_coordinator(config)
    assert coordinator.full_charge_voltage_taper_enabled is True
    coordinator.max_charge_power = 2400.0
    coordinator.battery_manual_mode_enabled = False
    coordinator.data = {"battery_soc": 90.0, "battery_total_energy": 5.12}
    ctrl = SimpleNamespace(coordinators=[coordinator], max_contracted_power=20000,
                           max_charge_capacity=2400.0)
    assert PricingManager(SimpleNamespace(), ctrl)._weekly_charge_hours_needed(0.512) == 2.5
