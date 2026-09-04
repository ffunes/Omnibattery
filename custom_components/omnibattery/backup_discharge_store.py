"""Persistent software-integrated backup-port discharge energy."""
from __future__ import annotations

import math

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN


STORE_KEY = f"{DOMAIN}.backup_discharge_energy"
STORE_VERSION = 1
_SAVE_DELAY_S = 30
_HASS_DATA_KEY = "backup_discharge_energy_store"


class BackupDischargeEnergyStore:
    """Keep per-config-entry battery totals across reloads and restarts."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, STORE_VERSION, STORE_KEY)
        self._data: dict[str, dict] = {}

    async def async_load(self) -> None:
        """Load valid non-negative totals and discard malformed values."""
        stored = await self._store.async_load() or {}
        if not isinstance(stored, dict):
            stored = {}
        data: dict[str, dict] = {}
        for key, raw_value in stored.items():
            if not isinstance(raw_value, dict):
                continue
            try:
                total_kwh = float(raw_value["total_kwh"])
                daily_kwh = float(raw_value.get("daily_kwh", 0.0))
            except (KeyError, TypeError, ValueError):
                continue
            if (
                math.isfinite(total_kwh)
                and total_kwh >= 0
                and math.isfinite(daily_kwh)
                and daily_kwh >= 0
            ):
                data[str(key)] = {
                    "total_kwh": total_kwh,
                    "daily_kwh": daily_kwh,
                    "reset_date": raw_value.get("reset_date"),
                }
        self._data = data

    def get(self, key: str, today: str) -> dict:
        """Return lifetime and current-local-day totals for one battery."""
        stored = self._data.get(key, {})
        same_day = stored.get("reset_date") == today
        return {
            "total_kwh": stored.get("total_kwh", 0.0),
            "daily_kwh": stored.get("daily_kwh", 0.0) if same_day else 0.0,
            "reset_date": today,
        }

    def set(
        self,
        key: str,
        *,
        total_kwh: float,
        daily_kwh: float,
        reset_date: str,
    ) -> None:
        """Update one total and schedule a coalesced Store write."""
        self._data[key] = {
            "total_kwh": total_kwh,
            "daily_kwh": daily_kwh,
            "reset_date": reset_date,
        }
        self._store.async_delay_save(lambda: self._data, _SAVE_DELAY_S)


async def async_get_backup_discharge_store(
    hass: HomeAssistant,
) -> BackupDischargeEnergyStore:
    """Return the domain-level store, loading it once per Home Assistant run."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get(_HASS_DATA_KEY)
    if store is None:
        store = BackupDischargeEnergyStore(hass)
        await store.async_load()
        domain_data[_HASS_DATA_KEY] = store
    return store
