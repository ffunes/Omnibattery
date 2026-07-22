"""Helpers for identifying the official Home Assistant Nord Pool source."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.helpers import device_registry as dr, entity_registry as er

NORDPOOL_DOMAIN = "nordpool"
NORDPOOL_GET_PRICES_SERVICE = "get_prices_for_date"


@dataclass(frozen=True)
class OfficialNordPoolSource:
    """Config entry and market area associated with an official Nord Pool entity."""

    config_entry_id: str
    area: str | None


def resolve_official_nordpool_source(
    hass: Any,
    entity_id: str | None,
) -> OfficialNordPoolSource | None:
    """Resolve the official Nord Pool config entry and area for an entity.

    The official integration exposes prices for a date through a response-only
    service. That service needs the integration's config-entry ID, while the
    selected sensor identifies the intended market area through its device.
    """
    if not entity_id:
        return None

    entity = er.async_get(hass).async_get(entity_id)
    if (
        entity is None
        or entity.platform != NORDPOOL_DOMAIN
        or not entity.config_entry_id
    ):
        return None

    area = None
    if entity.device_id:
        device = dr.async_get(hass).async_get(entity.device_id)
        if device is not None:
            area = next(
                (
                    str(identifier)
                    for domain, identifier in device.identifiers
                    if domain == NORDPOOL_DOMAIN
                ),
                None,
            )

    # A single configured area is unambiguous even if an older registry entry
    # lacks the device identifier used by current Home Assistant versions.
    if area is None:
        config_entry = hass.config_entries.async_get_entry(entity.config_entry_id)
        configured_areas = (config_entry.data.get("areas") if config_entry else None) or []
        if len(configured_areas) == 1:
            area = str(configured_areas[0])

    return OfficialNordPoolSource(entity.config_entry_id, area)


def is_official_nordpool_sensor(
    hass: Any,
    entity_id: str | None,
    attributes: dict | None = None,
) -> bool:
    """Return whether an entity can supply official Nord Pool service prices.

    ``raw_today`` takes precedence so existing HACS Nordpool installations keep
    their established sensor-attribute path even if a similarly named service
    happens to be registered.
    """
    if attributes is not None and "raw_today" in attributes:
        return False
    services = getattr(hass, "services", None)
    if services is None or not services.has_service(
        NORDPOOL_DOMAIN,
        NORDPOOL_GET_PRICES_SERVICE,
    ):
        return False
    return resolve_official_nordpool_source(hass, entity_id) is not None
