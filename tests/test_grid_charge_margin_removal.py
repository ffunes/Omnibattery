"""Tests for dropping ``predictive_grid_charge_margin_pct`` (v13 -> v14).

The percentage inflated the *already computed* deficit, so it scaled inversely
to the solar risk it claimed to hedge: a big top-up on a cloudy day with little
solar, a small one on a sunny day where the forecast could actually be wrong by
a lot. The kWh safety margin sits where the risk is -- it haircuts the solar
forecast itself -- so the percentage is removed and the kWh one gets a default
that is no longer 0.

The migration half needs the real ``hass`` / entity-registry, so it runs only
without the suite's ``-p no:homeassistant`` flag (conftest skips it otherwise)::

    .venv-test/Scripts/python -m pytest tests/test_grid_charge_margin_removal.py -o addopts=""
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omnibattery import async_migrate_entry
from custom_components.omnibattery.const import (
    CONFIG_NUMBER_DEFINITIONS,
    CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT,
    CONF_PREDICTIVE_SAFETY_MARGIN_KWH,
    DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH,
    default_predictive_safety_margin_kwh,
)
from custom_components.omnibattery.number import MarstekConfigNumberEntity

DOMAIN = "omnibattery"
PREFIX = "marstek_venus_system_"
REMOVED_UID = f"{PREFIX}{CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT}"


# ----------------------------------------------------------------------
# The replacement default (no hass needed)
# ----------------------------------------------------------------------

def test_default_margin_is_five_percent_of_the_configured_fleet():
    data = {"batteries": [{"battery_capacity_kwh": 5.12}, {"battery_capacity_kwh": 5.12}]}
    assert default_predictive_safety_margin_kwh(data) == 0.51


def test_default_margin_scales_with_the_fleet():
    small = default_predictive_safety_margin_kwh({"batteries": [{"battery_capacity_kwh": 5.0}]})
    big = default_predictive_safety_margin_kwh(
        {"batteries": [{"battery_capacity_kwh": 5.0}] * 6}
    )
    assert big > small > 0


@pytest.mark.parametrize(
    "data",
    [{}, {"batteries": []}, {"batteries": [{}]}, {"batteries": [{"battery_capacity_kwh": 0}]}],
    ids=["no_key", "no_batteries", "no_capacity", "zero_capacity"],
)
def test_default_margin_falls_back_to_no_margin_without_capacity(data):
    assert default_predictive_safety_margin_kwh(data) == DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH


# ----------------------------------------------------------------------
# The slider must show that same default, not 0.0 (no hidden values)
# ----------------------------------------------------------------------

MARGIN_DEF = next(
    d for d in CONFIG_NUMBER_DEFINITIONS if d["key"] == CONF_PREDICTIVE_SAFETY_MARGIN_KWH
)
FLEET = {"batteries": [{"battery_capacity_kwh": 5.12}] * 2}


def _slider_value(data):
    """native_value of the margin slider for a config entry holding ``data``."""
    entry = SimpleNamespace(data=data)
    return MarstekConfigNumberEntity(None, entry, MARGIN_DEF).native_value


def test_slider_shows_the_same_default_the_controller_uses():
    assert _slider_value(FLEET) == default_predictive_safety_margin_kwh(FLEET) > 0


@pytest.mark.parametrize("stored", [0.0, 3.0], ids=["explicit_zero", "explicit_value"])
def test_a_stored_margin_wins_over_the_default(stored):
    """An upgrade must not rewrite the margin the user already fixed."""
    assert _slider_value({**FLEET, CONF_PREDICTIVE_SAFETY_MARGIN_KWH: stored}) == stored


# ----------------------------------------------------------------------
# The v13 -> v14 migration
# ----------------------------------------------------------------------

def _entry(hass: HomeAssistant, **data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, version=13, data={"batteries": [], **data})
    entry.add_to_hass(hass)
    return entry


async def test_v14_drops_the_key_and_deletes_its_entity(hass: HomeAssistant) -> None:
    entry = _entry(hass, **{CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT: 25.0})
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "marstek_venus_system")},
    )
    number = er.async_get(hass).async_get_or_create(
        "number", DOMAIN, REMOVED_UID,
        suggested_object_id=REMOVED_UID, config_entry=entry, device_id=dev.id,
    )

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 16
    assert CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT not in entry.data
    # Deleted, not left behind unavailable: orphans have rendered as dead
    # duplicate controls on the panel before.
    assert er.async_get(hass).async_get(number.entity_id) is None


async def test_v14_leaves_a_hand_set_safety_margin_alone(hass: HomeAssistant) -> None:
    """Upgrading must not change what the battery does. The config flow always
    wrote this key, so every existing install carries an explicit value."""
    entry = _entry(
        hass,
        **{
            CONF_PREDICTIVE_SAFETY_MARGIN_KWH: 0.0,
            CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT: 50.0,
            "batteries": [{"battery_capacity_kwh": 10.0}],
        },
    )

    assert await async_migrate_entry(hass, entry) is True
    assert entry.data[CONF_PREDICTIVE_SAFETY_MARGIN_KWH] == 0.0


async def test_v14_without_the_entity_still_migrates(hass: HomeAssistant) -> None:
    entry = _entry(hass, **{CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT: 10.0})

    assert await async_migrate_entry(hass, entry) is True
    assert CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT not in entry.data
