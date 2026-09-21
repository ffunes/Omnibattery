"""Price-aware discharge reserve shipped with a status sensor and no controls.

Its enable flag and minimum-saving number lived only in the options flow, making
it the only one of the six dynamic-pricing economic features with no dashboard
switch. Same text assertions as its surplus-hold and high-price-discharge
siblings, guarding the same files.
"""

import json
from pathlib import Path


COMPONENT = Path("custom_components/omnibattery")
PANEL = COMPONENT / "frontend" / "marstek-panel.js"
TRANSLATIONS = ["strings.json"] + [
    f"translations/{lang}.json" for lang in ("ca", "de", "en", "es", "fr", "nl")
]


def test_enable_key_is_backfilled_on_existing_entries():
    setup = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    backfill = setup.split("_backfill = {")[1].split("}")[0]

    assert "CONF_DISCHARGE_RESERVE_ENABLED," in backfill


def test_switch_is_gated_on_presence_not_on_value():
    switch = (COMPONENT / "switch.py").read_text(encoding="utf-8")

    assert "if CONF_DISCHARGE_RESERVE_ENABLED in entry.data:" in switch
    assert "entities.append(DischargeReserveSwitch(hass, entry, controller))" in switch
    assert 'self._attr_translation_key = "discharge_reserve"' in switch


def test_number_is_gated_on_the_switch_key():
    const = (COMPONENT / "const" / "integration_const.py").read_text(encoding="utf-8")
    entry = const.split('"key": CONF_DISCHARGE_RESERVE_MIN_SAVING,')[1].split("},")[0]

    assert '"condition": CONF_DISCHARGE_RESERVE_ENABLED' in entry


def test_dashboard_exposes_the_toggle_and_its_threshold():
    panel = PANEL.read_text(encoding="utf-8")

    assert '{ key: "discharge_reserve", domain: "switch"' in panel
    assert '{ key: "discharge_reserve_min_saving"' in panel
    # One label per language block, or the row falls back to its raw key.
    assert panel.count("itemDischargeReserveSaving:") == 6
    assert panel.count('lk: "itemDischargeReserveSaving"') == 1


def test_switch_and_status_do_not_share_a_name():
    """Both entities hang off the same device; identical names are ambiguous."""
    for name in TRANSLATIONS:
        data = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        toggle = data["entity"]["switch"]["discharge_reserve"]["name"]
        status = data["entity"]["binary_sensor"]["discharge_reserve_status"]["name"]

        assert toggle, name
        assert status != toggle, name
