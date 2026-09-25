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


def test_select_is_gated_on_presence_not_on_value():
    select = (COMPONENT / "select.py").read_text(encoding="utf-8")

    assert "and CONF_HIGH_PRICE_DISCHARGE_ENABLED in entry.data" in select
    assert "entities.append(HighPriceSaleSelect(hass, entry, controller))" in select
    assert 'self._attr_translation_key = "high_price_sale"' in select


def test_dashboard_exposes_the_select_without_a_power_knob():
    panel = PANEL.read_text(encoding="utf-8")

    # A key missing from the allowlist simply never renders.
    assert '{ key: "high_price_sale", domain: "select"' in panel
    assert panel.count("highPriceSale:") == 6
    assert panel.count("    high_price_sale:") == 6
    # The export ceiling is not a knob: the feature uses the fleet's own
    # discharge power, already capped by the system-wide discharge limit.
    assert "high_price_discharge_max_power_w" not in panel
    # The per-kWh margin is min_arbitrage_margin, already on the dashboard.
    assert "high_price_discharge_additional_cost" not in panel
    assert '{ key: "min_arbitrage_margin"' in panel


def test_select_and_its_options_are_named_in_every_language():
    for name in TRANSLATIONS:
        data = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        entry = data["entity"]["select"]["high_price_sale"]

        assert entry["name"], name
        assert set(entry["state"]) == {"off", "surplus", "surplus_arbitrage"}, name


def test_unload_releases_the_override():
    """Shutdown writes stop the batteries, but the convention is explicit."""
    init = (COMPONENT / "__init__.py").read_text(encoding="utf-8")

    assert 'controller._high_price_discharge_mgr.clear_runtime("unload")' in init


def test_both_trigger_keys_are_backfilled():
    setup = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    backfill = setup.split("_backfill = {")[1].split("}")[0]
    assert "CONF_HIGH_PRICE_SURPLUS_EXPORT_ENABLED," in backfill
