"""Regression tests for the maximum controllable battery count."""

from custom_components.omnibattery.config_flow import _battery_count_schema
from custom_components.omnibattery.const import (
    CONF_SYSTEM_MAX_CHARGE_POWER,
    CONF_SYSTEM_MAX_DISCHARGE_POWER,
    CONFIG_NUMBER_DEFINITIONS,
    DYNAMIC_BOUNDS_SYSTEM_POWER_CAP,
    MAX_BATTERIES,
    MAX_SYSTEM_POWER_W,
)


def _battery_count_selector(schema):
    marker, selector = next(iter(schema.schema.items()))
    assert marker.schema == "num_batteries"
    return selector


def test_battery_count_selector_allows_ten_batteries():
    selector = _battery_count_selector(_battery_count_schema(default=MAX_BATTERIES))

    assert selector.config["min"] == 1
    assert selector.config["max"] == MAX_BATTERIES == 10


def test_system_power_definitions_cover_ten_max_power_batteries():
    definitions = {
        definition["key"]: definition for definition in CONFIG_NUMBER_DEFINITIONS
    }

    assert MAX_SYSTEM_POWER_W == 25_000
    assert definitions[CONF_SYSTEM_MAX_CHARGE_POWER]["max"] == MAX_SYSTEM_POWER_W
    assert definitions[CONF_SYSTEM_MAX_DISCHARGE_POWER]["max"] == MAX_SYSTEM_POWER_W
    assert (
        definitions[CONF_SYSTEM_MAX_CHARGE_POWER]["dynamic_bounds"]
        == DYNAMIC_BOUNDS_SYSTEM_POWER_CAP
    )
    assert (
        definitions[CONF_SYSTEM_MAX_DISCHARGE_POWER]["dynamic_bounds"]
        == DYNAMIC_BOUNDS_SYSTEM_POWER_CAP
    )
