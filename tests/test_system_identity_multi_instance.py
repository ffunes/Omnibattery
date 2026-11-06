"""Regression tests for system identity scoping across config entries."""
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.omnibattery.infra.system_identity import (
    LEGACY_DEVICE_IDENTIFIER,
    scope_system_entities,
)
from custom_components.omnibattery.const import DOMAIN


class FakeEntity:
    def __init__(self, key="pd_target_grid_power"):
        self._attr_unique_id = f"marstek_venus_system_{key}"
        self.entity_id = f"number.omnibattery_{key}"

    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, LEGACY_DEVICE_IDENTIFIER)},
            "name": "Omnibattery System",
        }


class FakeRegistry:
    def __init__(self, owner=None):
        self.owner = owner

    def async_get_entity_id(self, platform, integration, unique_id):
        assert platform == "number"
        assert integration == DOMAIN
        return "number.existing" if self.owner else None

    def async_get(self, entity_id):
        return SimpleNamespace(config_entry_id=self.owner)


def test_new_entries_have_distinct_system_identities():
    with patch(
        "custom_components.omnibattery.infra.system_identity.er.async_get",
        return_value=FakeRegistry(),
    ):
        first, second = FakeEntity(), FakeEntity()
        scope_system_entities(None, SimpleNamespace(entry_id="p1", title="Phase 1"), [first])
        scope_system_entities(None, SimpleNamespace(entry_id="p2", title="Phase 2"), [second])

    assert first.unique_id != second.unique_id
    assert first.unique_id.endswith("__p1")
    assert second.unique_id.endswith("__p2")
    assert first.device_info["identifiers"] != second.device_info["identifiers"]


def test_legacy_owner_keeps_existing_unique_id():
    with patch(
        "custom_components.omnibattery.infra.system_identity.er.async_get",
        return_value=FakeRegistry(owner="p3"),
    ):
        original = FakeEntity()
        scope_system_entities(None, SimpleNamespace(entry_id="p3", title="Phase 3"), [original])

    assert original.unique_id == "marstek_venus_system_pd_target_grid_power"
    assert original.entity_id == "number.omnibattery_pd_target_grid_power"
    assert original.device_info["identifiers"] == {(DOMAIN, LEGACY_DEVICE_IDENTIFIER)}


def test_other_entry_cannot_claim_legacy_identity():
    with patch(
        "custom_components.omnibattery.infra.system_identity.er.async_get",
        return_value=FakeRegistry(owner="p3"),
    ):
        other = FakeEntity()
        scope_system_entities(None, SimpleNamespace(entry_id="p1", title="Phase 1"), [other])

    assert other.unique_id.endswith("__p1")
    assert other.device_info["identifiers"] == {
        (DOMAIN, "marstek_venus_system_p1")
    }


def test_battery_entity_is_unchanged():
    battery = FakeEntity()
    battery._attr_unique_id = "battery_123_soc"
    with patch(
        "custom_components.omnibattery.infra.system_identity.er.async_get",
        return_value=FakeRegistry(),
    ):
        scope_system_entities(None, SimpleNamespace(entry_id="p1", title="Phase 1"), [battery])

    assert type(battery) is FakeEntity
    assert battery.unique_id == "battery_123_soc"
