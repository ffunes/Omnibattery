"""Keep system-level Home Assistant entity identities separate per config entry.

An entity already registered with the legacy unique ID keeps that ID when it
belongs to the current entry. Other entries receive stable entry-scoped IDs.
Battery-specific entities are not modified.
"""
from __future__ import annotations

from homeassistant.helpers import entity_registry as er

from ..const import DOMAIN
from .entity_naming import SYSTEM_UNIQUE_ID_PREFIX

LEGACY_DEVICE_IDENTIFIER = "marstek_venus_system"


def scope_system_entities(hass, entry, entities):
    """Scope aggregate entities to a config entry before adding them."""
    registry = er.async_get(hass)

    for entity in entities:
        unique_id = getattr(entity, "unique_id", None)
        if not unique_id or not unique_id.startswith(SYSTEM_UNIQUE_ID_PREFIX):
            continue

        info = entity.device_info
        if not info or (DOMAIN, LEGACY_DEVICE_IDENTIFIER) not in info.get(
            "identifiers", set()
        ):
            continue

        entity_id = getattr(entity, "entity_id", None)
        platform = entity_id.partition(".")[0] if entity_id else ""
        if not platform:
            raise ValueError(f"System entity {unique_id} has no entity domain")

        existing_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        existing = registry.async_get(existing_id) if existing_id else None
        legacy_owner = bool(existing and existing.config_entry_id == entry.entry_id)

        if not legacy_owner:
            entity._attr_unique_id = f"{unique_id}__{entry.entry_id}"
            entity.entity_id = f"{entity_id}_{entry.entry_id[:8]}"

        identifier = (
            LEGACY_DEVICE_IDENTIFIER
            if legacy_owner
            else f"{LEGACY_DEVICE_IDENTIFIER}_{entry.entry_id}"
        )
        scoped_info = {
            **info,
            "identifiers": {(DOMAIN, identifier)},
            "name": entry.title or info.get("name", "Omnibattery System"),
        }

        # Instance-specific subclass avoids changing the device_info of
        # other entities of the same class.
        original_type = type(entity)
        entity.__class__ = type(
            f"{original_type.__name__}Scoped",
            (original_type,),
            {"device_info": property(lambda self, value=scoped_info: value)},
        )

    return entities
