"""Removing an excluded device must not leave its entities behind.

The per-device entities are keyed by the device's index. Removing a device
moves the later ones down, so the last index's entities had nothing behind
them and stayed in the registry as unavailable rows.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from custom_components.omnibattery.infra import excluded_device_entities
from custom_components.omnibattery.infra.excluded_device_entities import (
    async_remove_orphaned_entities,
    expected_unique_ids,
)

PREFIX = "marstek_venus_system_"
METERED = ("excluded_device_enabled", "solar_surplus", "cover_home", "excluded_device_status",
           "dynamic_power_control", "exclusion_pct")
STATE_ONLY = METERED[:4]


def _registry(unique_ids):
    entities = [SimpleNamespace(unique_id=uid, entity_id=f"x.{uid}") for uid in unique_ids]
    removed = []
    registry = SimpleNamespace(async_remove=removed.append)
    return entities, registry, removed


def _sweep(devices, unique_ids):
    entities, registry, removed = _registry(unique_ids)
    entry = SimpleNamespace(entry_id="entry", data={"excluded_devices": devices})
    with patch.multiple(
        excluded_device_entities.er,
        async_get=lambda hass: registry,
        async_entries_for_config_entry=lambda reg, entry_id: entities,
    ):
        assert async_remove_orphaned_entities(SimpleNamespace(), entry) == removed
    return removed


def test_a_metered_device_has_six_entities_a_state_only_charger_four():
    assert expected_unique_ids([{}, {"ev_charger_no_telemetry": True}]) == (
        {f"{PREFIX}{key}_0" for key in METERED} | {f"{PREFIX}{key}_1" for key in STATE_ONLY}
    )


def test_removing_a_device_drops_the_last_indexs_entities():
    # Two devices, the first removed: the second is index 0 now.
    before = [f"{PREFIX}{key}_{index}" for index in (0, 1) for key in METERED]

    removed = _sweep([{}], before)

    assert sorted(removed) == sorted(f"x.{PREFIX}{key}_1" for key in METERED)


def test_an_index_that_became_a_state_only_charger_loses_the_two_it_lacks():
    before = [f"{PREFIX}{key}_0" for key in METERED]

    removed = _sweep([{"ev_charger_no_telemetry": True}], before)

    assert sorted(removed) == [f"x.{PREFIX}dynamic_power_control_0", f"x.{PREFIX}exclusion_pct_0"]


def test_nothing_else_of_the_entry_is_touched():
    others = [
        f"{PREFIX}solar_surplus_power",          # not a per-device key
        f"{PREFIX}time_slot_3",
        f"{PREFIX}excluded_device_status",       # no index
        "battery_1_cover_home_0",                # not a system entity
    ]

    assert _sweep([], others) == []
    assert _sweep([{}], [f"{PREFIX}{key}_0" for key in METERED]) == []


def test_the_key_list_matches_what_the_platforms_create():
    # A per-device key missing from _KEYS would be deleted and re-created at
    # every start, losing the user's renames and disabled state.
    import re
    from pathlib import Path

    root = Path(excluded_device_entities.__file__).parents[1]
    created = set()
    for platform in ("switch.py", "number.py", "sensor.py"):
        source = (root / platform).read_text(encoding="utf-8")
        created |= set(re.findall(r'SYSTEM_UNIQUE_ID_PREFIX\}(\w+)_\{index\}"', source))
    assert created == set(excluded_device_entities._KEYS)
