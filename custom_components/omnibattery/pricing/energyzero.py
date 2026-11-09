"""Helpers for identifying the selected official EnergyZero price source."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.helpers import entity_registry as er

ENERGYZERO_DOMAIN = "energyzero"
ENERGYZERO_GET_PRICES_SERVICE = "get_energy_prices"


@dataclass(frozen=True)
class EnergyZeroSource:
    """Service parameters associated with a selected EnergyZero entity."""

    config_entry_id: str
    price_type: str
    interval: str


def resolve_energyzero_source(
    hass: Any,
    entity_id: str | None,
) -> EnergyZeroSource | None:
    """Resolve an EnergyZero electricity entity to its service request options."""
    if not entity_id:
        return None

    entity = er.async_get(hass).async_get(entity_id)
    if (
        entity is None
        or entity.platform != ENERGYZERO_DOMAIN
        or not entity.config_entry_id
    ):
        return None

    if entity.unique_id.endswith("_today_energy_all_in_current_price"):
        price_type = "all_in"
    elif entity.unique_id.endswith("_today_energy_current_hour_price"):
        price_type = "market"
    else:
        return None

    config_entry = hass.config_entries.async_get_entry(entity.config_entry_id)
    interval = (
        config_entry.options.get("electricity_price_interval", "hourly")
        if config_entry is not None
        else "hourly"
    )
    if interval not in ("hourly", "quarter_hourly"):
        interval = "hourly"

    return EnergyZeroSource(
        entity.config_entry_id,
        price_type,
        "quarter" if interval == "quarter_hourly" else "hour",
    )


def is_energyzero_sensor(hass: Any, entity_id: str | None) -> bool:
    """Return whether the selected entity can supply EnergyZero service prices."""
    services = getattr(hass, "services", None)
    if services is None or not services.has_service(
        ENERGYZERO_DOMAIN,
        ENERGYZERO_GET_PRICES_SERVICE,
    ):
        return False
    return resolve_energyzero_source(hass, entity_id) is not None
