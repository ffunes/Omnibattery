"""The Discharge Reserve switch and its minimum saving are gone (#888).

Dynamic Pricing holds the battery whenever the plan is cheaper; only the
status sensor stays, and the v19 migration removes the two keys and the two
entities, found by unique_id (installs carry marstek_venus_* and
omnibattery_* entity ids).
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.omnibattery import async_migrate_entry
from custom_components.omnibattery.const import DOMAIN

COMPONENT = Path("custom_components/omnibattery")
PANEL = COMPONENT / "frontend" / "marstek-panel.js"
TRANSLATIONS = ["strings.json"] + [
    f"translations/{lang}.json" for lang in ("ca", "de", "en", "es", "fr", "nl")
]
GONE = ("discharge_reserve_enabled", "discharge_reserve_min_saving")


def test_no_switch_no_number_no_setting():
    for name in ("switch.py", "number.py", "__init__.py", "const/integration_const.py",
                 "diagnostics.py", "binary_sensor.py"):
        source = (COMPONENT / name).read_text(encoding="utf-8")
        assert "DischargeReserveSwitch" not in source, name
        assert "CONF_DISCHARGE_RESERVE" not in source, name
    for name in TRANSLATIONS:
        entity = json.loads((COMPONENT / name).read_text(encoding="utf-8"))["entity"]
        assert "discharge_reserve" not in entity.get("switch", {}), name
        assert "discharge_reserve_min_saving" not in entity.get("number", {}), name
        assert entity["binary_sensor"]["discharge_reserve_status"]["name"], name


def test_the_dashboard_keeps_only_the_status():
    panel = PANEL.read_text(encoding="utf-8")
    assert '{ key: "discharge_reserve", domain: "switch"' not in panel
    assert '{ key: "discharge_reserve_min_saving"' not in panel
    assert "itemDischargeReserveSaving" not in panel
    assert '{ key: "discharge_reserve_status", domain: "binary_sensor"' in panel
    # One description per language block.
    assert panel.count("\n    discharge_reserve_status: ") == 6


class _Entries:
    def __init__(self):
        self.updated = None

    def async_update_entry(self, entry, *, data, version, title=None, options=None):
        self.updated = {"data": data, "options": options, "version": version}
        entry.data, entry.version = data, version
        if options is not None:
            entry.options = options


def test_v19_drops_both_keys_and_both_entities_whatever_their_entity_id():
    """Looked up by unique_id: the entity ids differ between old and new
    installs (marstek_venus_* / omnibattery_*), the unique ids do not."""
    registry = {
        ("switch", "marstek_venus_system_discharge_reserve"): "switch.marstek_venus_discharge_reserve",
        ("number", "marstek_venus_system_discharge_reserve_min_saving"):
            "number.omnibattery_discharge_reserve_min_saving",
        ("number", "marstek_venus_system_surplus_hold_min_saving"):
            "number.omnibattery_surplus_hold_min_saving",
    }
    removed = []

    def get_entity_id(domain, platform, unique_id):
        assert platform == DOMAIN
        return registry.get((domain, unique_id))

    fake = SimpleNamespace(async_get_entity_id=get_entity_id, async_remove=removed.append)
    hass = SimpleNamespace(config_entries=_Entries())
    entry = SimpleNamespace(
        version=18, entry_id="entry",
        data={"batteries": [], GONE[0]: True, GONE[1]: 0.05, "surplus_hold_min_saving": 0.02},
        options={GONE[1]: 0.03, "other": 1},
    )
    with patch("homeassistant.helpers.entity_registry.async_get", lambda _hass: fake):
        assert asyncio.run(async_migrate_entry(hass, entry)) is True
    assert entry.version == 21
    assert not set(GONE) & set(entry.data) and not set(GONE) & set(entry.options)
    assert entry.data["surplus_hold_min_saving"] == 0.02 and entry.options["other"] == 1
    assert sorted(removed) == [
        "number.omnibattery_discharge_reserve_min_saving",
        "switch.marstek_venus_discharge_reserve",
    ]
