"""The two high-price switches became one select (off / surplus / surplus +
arbitrage), v15 -> v16.

The migration half needs the real ``hass`` / entity-registry, so it runs only
without the suite's ``-p no:homeassistant`` flag (conftest skips it otherwise)::

    .venv-test/Scripts/python -m pytest tests/test_high_price_sale_select.py -o addopts=""
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    CONF_HIGH_PRICE_DISCHARGE_ENABLED,
    CONF_HIGH_PRICE_SURPLUS_EXPORT_ENABLED,
)
from custom_components.omnibattery.select import HighPriceSaleSelect

DOMAIN = "omnibattery"
PREFIX = "marstek_venus_system_"


def _select(surplus: bool, arbitrage: bool):
    entry = SimpleNamespace(data={
        CONF_HIGH_PRICE_SURPLUS_EXPORT_ENABLED: surplus,
        CONF_HIGH_PRICE_DISCHARGE_ENABLED: arbitrage,
    })
    hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_update_entry=lambda e, data: setattr(e, "data", data)
    ))
    controller = SimpleNamespace(
        high_price_surplus_export_enabled=surplus,
        high_price_discharge_enabled=arbitrage,
    )
    sel = HighPriceSaleSelect(hass, entry, controller)
    sel.async_write_ha_state = lambda: None
    return sel, entry, controller


@pytest.mark.parametrize(
    ("option", "surplus", "arbitrage"),
    [("off", False, False), ("surplus", True, False), ("surplus_arbitrage", True, True)],
)
def test_option_round_trips_through_both_flags(option, surplus, arbitrage):
    sel, entry, controller = _select(not surplus, not arbitrage)

    asyncio.run(sel.async_select_option(option))

    assert (controller.high_price_surplus_export_enabled,
            controller.high_price_discharge_enabled) == (surplus, arbitrage)
    assert entry.data[CONF_HIGH_PRICE_SURPLUS_EXPORT_ENABLED] is surplus
    assert entry.data[CONF_HIGH_PRICE_DISCHARGE_ENABLED] is arbitrage
    assert sel.current_option == option


async def test_v16_folds_arbitrage_only_and_deletes_the_switches(hass) -> None:
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.omnibattery import async_migrate_entry

    entry = MockConfigEntry(domain=DOMAIN, version=15, data={
        "batteries": [],
        CONF_HIGH_PRICE_DISCHARGE_ENABLED: True,
        CONF_HIGH_PRICE_SURPLUS_EXPORT_ENABLED: False,
    })
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    old = [
        reg.async_get_or_create("switch", DOMAIN, f"{PREFIX}{key}", config_entry=entry)
        for key in ("high_price_discharge", "high_price_surplus_export")
    ]

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 16
    assert entry.data[CONF_HIGH_PRICE_SURPLUS_EXPORT_ENABLED] is True
    assert entry.data[CONF_HIGH_PRICE_DISCHARGE_ENABLED] is True
    for e in old:
        assert reg.async_get(e.entity_id) is None
