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
def test_a_stored_serial_port_is_its_own_setup_link_for_every_brand(brand):
    cls = DRIVERS[brand]
    base = {**ZENDURE, "brand": brand}
    assert cls.link_key(base) != cls.link_key({**base, "serial_port": "/dev/ttyUSB0"})
