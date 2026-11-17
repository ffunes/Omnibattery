"""The minimum-saving sliders show the price currency (€/kWh or CHF/kWh).

They were the only per-kWh price settings shown as a bare "/kWh"; the arbitrage
margin and the price thresholds already carried the currency.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    CONF_DISCHARGE_RESERVE_MIN_SAVING,
    CONF_PRICE_INTEGRATION_TYPE,
    CONF_SURPLUS_HOLD_MIN_SAVING,
    CONFIG_NUMBER_DEFINITIONS,
    PRICE_INTEGRATION_CKW,
)
from custom_components.omnibattery.number import MarstekConfigNumberEntity


def _definition(key):
    return next(d for d in CONFIG_NUMBER_DEFINITIONS if d["key"] == key)


@pytest.mark.parametrize("key", [CONF_DISCHARGE_RESERVE_MIN_SAVING, CONF_SURPLUS_HOLD_MIN_SAVING])
@pytest.mark.parametrize(
    ("data", "unit"),
    [({}, "€/kWh"), ({CONF_PRICE_INTEGRATION_TYPE: PRICE_INTEGRATION_CKW}, "CHF/kWh")],
)
def test_a_minimum_saving_is_shown_in_the_price_currency(key, data, unit):
    entity = MarstekConfigNumberEntity(None, SimpleNamespace(data=data), _definition(key))

    assert entity.native_unit_of_measurement == unit


def test_other_config_numbers_keep_their_unit():
    for definition in CONFIG_NUMBER_DEFINITIONS:
        if definition.get("unit") in (None, "/kWh"):
            continue
        entity = MarstekConfigNumberEntity(None, SimpleNamespace(data={}), definition)
        assert entity.native_unit_of_measurement == definition["unit"]
