"""Price-aware surplus hold shipped with a slider nobody could see.

Its enable flag lived only in the options flow and its minimum-saving number
never reached the dashboard allowlist, so the feature existed as entities and
diagnostics but not as a control. Same text assertions as its high-price
sibling, guarding the same three files.
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

    assert "CONF_SURPLUS_PRICE_HOLD_ENABLED," in backfill


def test_switch_is_gated_on_presence_not_on_value():
    switch = (COMPONENT / "switch.py").read_text(encoding="utf-8")

    assert "if controller and CONF_SURPLUS_PRICE_HOLD_ENABLED in entry.data:" in switch
    assert "entities.append(SurplusPriceHoldSwitch(hass, entry, controller))" in switch
    assert 'self._attr_translation_key = "surplus_price_hold"' in switch


def test_dashboard_exposes_the_toggle_and_its_threshold():
    panel = PANEL.read_text(encoding="utf-8")

    assert '{ key: "surplus_price_hold", domain: "switch"' in panel
    assert '{ key: "surplus_hold_min_saving"' in panel
    # One label per language block, or the row falls back to its raw key.
    assert panel.count("itemSurplusHoldSaving:") == 6
    assert panel.count('lk: "itemSurplusHoldSaving"') == 1


def test_switch_and_status_do_not_share_a_name():
    """Both entities hang off the same device; identical names are ambiguous."""
    for name in TRANSLATIONS:
        data = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        toggle = data["entity"]["switch"]["surplus_price_hold"]["name"]
        status = data["entity"]["binary_sensor"]["surplus_price_hold_status"]["name"]

        assert toggle, name
        assert status != toggle, name
