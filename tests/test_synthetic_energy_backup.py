"""Per-serial synthetic-energy backup contract.

The point of the backup is that a deleted-and-re-added battery reclaims its
lifetime total: the value is keyed by serial number, isolated per serial, and a
write schedules a debounced save. The Store I/O is HA-owned and not exercised
here; this guards the in-memory cache and keying logic.
"""
from __future__ import annotations

from custom_components.omnibattery.synthetic_energy_backup import SyntheticEnergyBackup


class _FakeStore:
    def __init__(self):
        self.saves = 0

    def async_delay_save(self, _data_func, _delay):
        self.saves += 1


def _make_backup() -> tuple[SyntheticEnergyBackup, _FakeStore]:
    # Bypass __init__ so no real Store / hass is needed.
    backup = SyntheticEnergyBackup.__new__(SyntheticEnergyBackup)
    store = _FakeStore()
    backup._store = store
    backup._data = {}
    return backup, store


def test_set_then_get_reclaims_value():
    backup, store = _make_backup()
    backup.set("SN-A", "total_charging_energy", 12.5, None)
    assert backup.get("SN-A", "total_charging_energy") == {"kwh": 12.5, "reset_date": None}
    assert store.saves == 1  # write was debounced-scheduled


def test_isolated_per_serial():
    # The crux: a different battery (serial) does not see another's total.
    backup, _ = _make_backup()
    backup.set("SN-A", "total_charging_energy", 12.5, None)
    assert backup.get("SN-B", "total_charging_energy") is None


def test_unknown_key_returns_none():
    backup, _ = _make_backup()
    backup.set("SN-A", "total_charging_energy", 12.5, None)
    assert backup.get("SN-A", "total_discharging_energy") is None


def test_daily_carries_reset_date():
    backup, _ = _make_backup()
    backup.set("SN-A", "total_daily_charging_energy", 3.2, "2026-06-30")
    assert backup.get("SN-A", "total_daily_charging_energy")["reset_date"] == "2026-06-30"
