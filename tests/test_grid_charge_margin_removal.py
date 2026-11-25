"""Tests for dropping ``predictive_grid_charge_margin_pct`` (v13 -> v14).

The percentage inflated the *already computed* deficit, so it scaled inversely
to the solar risk it claimed to hedge: a big top-up on a cloudy day with little
solar, a small one on a sunny day where the forecast could actually be wrong by
a lot. The kWh safety margin sits where the risk is -- it haircuts the solar
forecast itself -- so the percentage is removed. Since v20 that margin is
itself a % of the forecast sun (see the v19 -> v20 tests at the end).

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
    CONF_PREDICTIVE_SAFETY_MARGIN_PCT,
    DEFAULT_PREDICTIVE_SAFETY_MARGIN_PCT,
)
from custom_components.omnibattery.number import ConfigNumberEntity

DOMAIN = "omnibattery"
PREFIX = "marstek_venus_system_"
REMOVED_UID = f"{PREFIX}{CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT}"


# ----------------------------------------------------------------------
# The % slider shows its fixed default, or the stored value
# ----------------------------------------------------------------------

MARGIN_DEF = next(
    d for d in CONFIG_NUMBER_DEFINITIONS if d["key"] == CONF_PREDICTIVE_SAFETY_MARGIN_PCT
)
FLEET = {"batteries": [{"battery_capacity_kwh": 5.12}] * 2}


def _slider_value(data):
    """native_value of the margin slider for a config entry holding ``data``."""
    entry = SimpleNamespace(data=data)
    return ConfigNumberEntity(None, entry, MARGIN_DEF).native_value


def test_slider_is_a_percent_with_a_fixed_default():
    assert (MARGIN_DEF["min"], MARGIN_DEF["max"], MARGIN_DEF["unit"]) == (0, 50, "%")
    assert _slider_value(FLEET) == DEFAULT_PREDICTIVE_SAFETY_MARGIN_PCT > 0


@pytest.mark.parametrize("stored", [0.0, 30.0], ids=["explicit_zero", "explicit_value"])
def test_a_stored_margin_wins_over_the_default(stored):
    assert _slider_value({**FLEET, CONF_PREDICTIVE_SAFETY_MARGIN_PCT: stored}) == stored


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
    assert entry.version == 22
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
    # v20 turns the kWh margin into a %: a hand-set 0 stays 0.
    assert CONF_PREDICTIVE_SAFETY_MARGIN_KWH not in entry.data
    assert entry.data[CONF_PREDICTIVE_SAFETY_MARGIN_PCT] == 0.0


async def test_v14_without_the_entity_still_migrates(hass: HomeAssistant) -> None:
    entry = _entry(hass, **{CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT: 10.0})

    assert await async_migrate_entry(hass, entry) is True
    assert CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT not in entry.data


# ----------------------------------------------------------------------
# The v19 -> v20 migration: the kWh safety margin becomes a %
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "stored, expected",
    [(0.0, 0.0), (0, 0.0), (2.5, None), (None, None)],
    ids=["zero", "int_zero", "hand_set_value", "never_set"],
)
def test_v20_converts_only_a_zero_margin(stored, expected):
    """No fixed sun turns kWh into %: only 0 carries over, anything else
    takes the default (the key stays unset)."""
    from custom_components.omnibattery import _migrate_safety_margin_to_pct

    data = {"batteries": []}
    if stored is not None:
        data[CONF_PREDICTIVE_SAFETY_MARGIN_KWH] = stored

    _migrate_safety_margin_to_pct(data)

    assert CONF_PREDICTIVE_SAFETY_MARGIN_KWH not in data
    assert data.get(CONF_PREDICTIVE_SAFETY_MARGIN_PCT) == expected


async def test_v20_deletes_the_kwh_margin_entity(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, version=19,
        data={"batteries": [], CONF_PREDICTIVE_SAFETY_MARGIN_KWH: 2.5},
    )
    entry.add_to_hass(hass)
    old_uid = f"{PREFIX}{CONF_PREDICTIVE_SAFETY_MARGIN_KWH}"
    number = er.async_get(hass).async_get_or_create(
        "number", DOMAIN, old_uid, suggested_object_id=old_uid, config_entry=entry,
    )

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 22
    assert CONF_PREDICTIVE_SAFETY_MARGIN_KWH not in entry.data
    assert CONF_PREDICTIVE_SAFETY_MARGIN_PCT not in entry.data
    assert er.async_get(hass).async_get(number.entity_id) is None
