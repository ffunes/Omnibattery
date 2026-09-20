"""Tests for the v12 -> v13 price-discharge-switch merge (``async_migrate_entry``).

``dp_price_discharge_control`` and ``rt_price_discharge_control`` were the same
switch behind two ``translation_key``/``unique_id`` pairs, one per predictive
mode. The modes are mutually exclusive, so v13 merges them into a single
``price_discharge_control``: the entity matching the active mode is re-keyed onto
the new ``unique_id`` (its ``entity_id``, and therefore its history, untouched)
and a stale twin left behind by a past mode switch is removed rather than left
orphaned -- orphans have rendered as duplicate controls on the panel before.

Needs the real ``hass`` / entity-registry, so it runs only without the suite's
``-p no:homeassistant`` flag (conftest skips it otherwise), e.g.::

    .venv-test/Scripts/python -m pytest tests/test_price_discharge_control_migration.py -o addopts=""
"""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omnibattery import async_migrate_entry
from custom_components.omnibattery.const import (
    CONF_DP_PRICE_DISCHARGE_CONTROL,
    CONF_PREDICTIVE_CHARGING_MODE,
    CONF_PRICE_DISCHARGE_CONTROL,
    CONF_RT_PRICE_DISCHARGE_CONTROL,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_REALTIME_PRICE,
)

DOMAIN = "omnibattery"
PREFIX = "marstek_venus_system_"
NEW_UID = f"{PREFIX}price_discharge_control"


def _entry(hass: HomeAssistant, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, version=12, data={"batteries": [], **data}
    )
    entry.add_to_hass(hass)
    return entry


def _register(hass, entry, slug: str) -> er.RegistryEntry:
    """Register a pre-merge switch at the entity_id its unique_id produced."""
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "marstek_venus_system")},
    )
    return er.async_get(hass).async_get_or_create(
        "switch", DOMAIN, f"{PREFIX}{slug}",
        suggested_object_id=f"{PREFIX}{slug}",
        config_entry=entry, device_id=dev.id,
    )


@pytest.mark.parametrize(
    ("mode", "slug", "conf_key", "other_key"),
    [
        (
            PREDICTIVE_MODE_DYNAMIC_PRICING,
            "dp_price_discharge_control",
            CONF_DP_PRICE_DISCHARGE_CONTROL,
            CONF_RT_PRICE_DISCHARGE_CONTROL,
        ),
        (
            PREDICTIVE_MODE_REALTIME_PRICE,
            "rt_price_discharge_control",
            CONF_RT_PRICE_DISCHARGE_CONTROL,
            CONF_DP_PRICE_DISCHARGE_CONTROL,
        ),
    ],
    ids=["from_dp", "from_rt"],
)
async def test_v13_merges_from_either_provenance(
    hass: HomeAssistant, mode, slug, conf_key, other_key
) -> None:
    """An entry in either mode keeps its ON state and its entity_id."""
    entry = _entry(hass, **{CONF_PREDICTIVE_CHARGING_MODE: mode, conf_key: True})
    original = _register(hass, entry, slug)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 13

    # The ON/OFF the user had set survives under the merged key; the two
    # per-mode keys are gone so nothing can read a stale value.
    assert entry.data[CONF_PRICE_DISCHARGE_CONTROL] is True
    assert conf_key not in entry.data
    assert other_key not in entry.data

    # Re-keyed in place: new unique_id, same entity_id (history preserved).
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("switch", DOMAIN, f"{PREFIX}{slug}") is None
    assert reg.async_get_entity_id("switch", DOMAIN, NEW_UID) == original.entity_id
    assert reg.async_get(original.entity_id).unique_id == NEW_UID


async def test_v13_off_stays_off(hass: HomeAssistant) -> None:
    """A user who had the switch off does not get it armed by the merge."""
    entry = _entry(
        hass,
        **{
            CONF_PREDICTIVE_CHARGING_MODE: PREDICTIVE_MODE_DYNAMIC_PRICING,
            CONF_DP_PRICE_DISCHARGE_CONTROL: False,
        },
    )
    _register(hass, entry, "dp_price_discharge_control")

    assert await async_migrate_entry(hass, entry) is True
    assert entry.data[CONF_PRICE_DISCHARGE_CONTROL] is False


async def test_v13_active_mode_wins_and_stale_twin_is_deleted(
    hass: HomeAssistant,
) -> None:
    """A past mode switch left both entities; only the active one survives."""
    entry = _entry(
        hass,
        **{
            CONF_PREDICTIVE_CHARGING_MODE: PREDICTIVE_MODE_REALTIME_PRICE,
            CONF_DP_PRICE_DISCHARGE_CONTROL: False,
            CONF_RT_PRICE_DISCHARGE_CONTROL: True,
        },
    )
    stale = _register(hass, entry, "dp_price_discharge_control")
    keeper = _register(hass, entry, "rt_price_discharge_control")

    assert await async_migrate_entry(hass, entry) is True

    # The active mode decides both the merged value and the surviving entity.
    assert entry.data[CONF_PRICE_DISCHARGE_CONTROL] is True
    reg = er.async_get(hass)
    assert reg.async_get(keeper.entity_id).unique_id == NEW_UID
    # Deleted outright, not left behind as an unavailable duplicate control.
    assert reg.async_get(stale.entity_id) is None
    assert reg.async_get_entity_id(
        "switch", DOMAIN, f"{PREFIX}dp_price_discharge_control"
    ) is None


async def test_v13_without_registry_entries_still_merges_data(
    hass: HomeAssistant,
) -> None:
    """A fresh install with no pre-merge entity migrates its data anyway."""
    entry = _entry(
        hass,
        **{
            CONF_PREDICTIVE_CHARGING_MODE: PREDICTIVE_MODE_DYNAMIC_PRICING,
            CONF_DP_PRICE_DISCHARGE_CONTROL: True,
        },
    )

    assert await async_migrate_entry(hass, entry) is True
    assert entry.data[CONF_PRICE_DISCHARGE_CONTROL] is True
