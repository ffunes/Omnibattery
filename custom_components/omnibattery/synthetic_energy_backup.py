"""Per-serial backup of synthetic energy totals.

Drivers without a hardware kWh counter (Zendure) integrate charge/discharge
energy in software (:class:`SyntheticEnergySensor`). That accumulator persists
via ``RestoreEntity``, whose store is keyed by ``entity_id`` and tied to the
entity registry — so deleting the config entry purges it and a re-add starts the
lifetime total back at zero.

This keeps a copy keyed by the device **serial number** in a domain-level Store
with a fixed key. A config-entry deletion does not touch arbitrary integration
Stores, so the copy survives it (the same survives-a-delete philosophy as
:mod:`.config_backup`). On re-add, the sensor reclaims its total from here once
the serial is known. Keyed by serial, not host/port, because DHCP can move the
device but the serial is stable.

Layout: ``{serial: {sensor_key: {"kwh": float, "reset_date": str | None}}}``.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_KEY = f"{DOMAIN}.synthetic_energy_backup"
STORE_VERSION = 1
# Coalesce the per-poll writes; a crash loses at most this much accumulation.
_SAVE_DELAY_S = 30
_HASS_DATA_KEY = "synthetic_energy_backup"


class SyntheticEnergyBackup:
    """In-memory cache of the per-serial totals with debounced persistence."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, STORE_VERSION, STORE_KEY)
        self._data: dict = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    def get(self, serial: str, key: str) -> dict | None:
        """Return the saved ``{"kwh", "reset_date"}`` for a serial+key, or None."""
        return self._data.get(serial, {}).get(key)

    def set(self, serial: str, key: str, kwh: float, reset_date: str | None) -> None:
        """Update one serial+key and schedule a debounced save."""
        self._data.setdefault(serial, {})[key] = {"kwh": kwh, "reset_date": reset_date}
        # async_delay_save re-reads self._data at flush time, so the latest value
        # is what lands on disk even though many sets share one pending save.
        self._store.async_delay_save(lambda: self._data, _SAVE_DELAY_S)


async def async_get_backup(hass: HomeAssistant) -> SyntheticEnergyBackup:
    """Return the shared backup, loading it once per HA instance.

    Stored at the domain level (not under an ``entry_id``) so it is shared across
    entries and outlives any single entry's deletion. Call once up-front in
    ``async_setup_entry`` so the entity setup that follows finds it cached and
    never races on the initial load.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    backup = domain_data.get(_HASS_DATA_KEY)
    if backup is None:
        backup = SyntheticEnergyBackup(hass)
        await backup.async_load()
        domain_data[_HASS_DATA_KEY] = backup
    return backup
