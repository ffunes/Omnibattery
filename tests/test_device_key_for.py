"""One device_key formula shared by the coordinator, config flow and device removal."""

from __future__ import annotations

import types

from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)
from custom_components.omnibattery.infra.mac_tracking import device_key_for


def test_formula_slave_1_keeps_historical_form():
    assert device_key_for("10.0.0.5", 502, 1) == "10.0.0.5_502"


def test_formula_other_slave_gets_suffix():
    assert device_key_for("10.0.0.5", 502, 2) == "10.0.0.5_502_2"


def test_coordinator_property_matches_function():
    for slave in (1, 2):
        stub = types.SimpleNamespace(host="10.0.0.5", port=502, slave_id=slave)
        got = MarstekVenusDataUpdateCoordinator.device_key.fget(stub)
        assert got == device_key_for("10.0.0.5", 502, slave)
