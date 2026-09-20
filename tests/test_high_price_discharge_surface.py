"""The feature must be reachable without reopening the options flow.

Entries created before high-price discharge existed carry no enable key, and
its entities are gated on key *presence*, so the whole surface hangs on the
setup backfill. These are text assertions on purpose: the wiring they protect
lives in three files that no unit test instantiates.
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

    assert "CONF_HIGH_PRICE_DISCHARGE_ENABLED," in backfill


def test_switch_is_gated_on_presence_not_on_value():
    switch = (COMPONENT / "switch.py").read_text(encoding="utf-8")

    assert (
        "if controller and CONF_HIGH_PRICE_DISCHARGE_ENABLED in entry.data:" in switch
    )
    assert "entities.append(HighPriceDischargeSwitch(hass, entry, controller))" in switch
    assert 'self._attr_translation_key = "high_price_discharge"' in switch


def test_dashboard_exposes_the_toggle_and_its_only_setting():
    panel = PANEL.read_text(encoding="utf-8")

    # A key missing from the allowlist simply never renders.
    assert '{ key: "high_price_discharge", domain: "switch"' in panel
    assert '{ key: "high_price_discharge_max_power_w"' in panel
    # One label per language block, or the row falls back to its raw key.
    assert panel.count("itemHighPriceExport:") == 6
    assert panel.count('lk: "itemHighPriceExport"') == 1
    # The per-kWh margin is min_arbitrage_margin, already on the dashboard.
    assert "high_price_discharge_additional_cost" not in panel
    assert '{ key: "min_arbitrage_margin"' in panel


def test_switch_is_named_in_every_language():
    for name in TRANSLATIONS:
        data = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        entry = data["entity"]["switch"]["high_price_discharge"]

        assert entry["name"], name
