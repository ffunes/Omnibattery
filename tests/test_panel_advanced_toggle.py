"""Advanced-settings toggle on the Control tab (frontend only, no new entities).

The predictive-charging card packed 19 rows into one view. Items marked `adv`
in SYS_SECTIONS are hidden behind a per-browser "Advanced settings" toggle
(localStorage, no HA entity), reusing the same display:none mechanism a
`gate` switch already uses to hide its sibling rows. Static text assertions
on the raw panel source, same style as the other panel-surface tests.
"""

import re
from pathlib import Path


COMPONENT = Path("custom_components/omnibattery")
PANEL = COMPONENT / "frontend" / "marstek-panel.js"

# Items the plan asks to move behind "Advanced settings" (T3, diagPredictive).
ADV_KEYS = {
    "max_price_threshold",
    "discharge_price_threshold",
    "min_arbitrage_margin",
    "round_trip_efficiency",
    "predictive_safety_margin_kwh",
    "min_soc_floor_enabled",
    "predictive_min_soc_floor",
    "negative_injection_threshold",
    "predischarge_reserve_soc",
    "surplus_hold_min_saving",
    "discharge_reserve_min_saving",
}

# Function switches must stay visible even with Advanced off; only the
# discharge-reserve threshold (not its switch) is a T2 addition marked `adv`.
NOT_ADV_KEYS = {
    "predictive_charging",
    "negative_price_charging",
    "smart_predischarge",
    "surplus_price_hold",
    "high_price_discharge",
    "discharge_reserve",
}


def _sys_sections_block(panel: str) -> str:
    start = panel.index("const SYS_SECTIONS = [")
    end = panel.index("const SYS_LAYOUT", start)
    return panel[start:end]


def _item_lines(block: str):
    """One line per `{ key: "...", ... }` row (SYS_SECTIONS is one-item-per-line)."""
    for line in block.splitlines():
        m = re.search(r'key:\s*"([^"]+)"', line)
        if m:
            yield m.group(1), line


def test_listed_items_are_marked_advanced():
    panel = PANEL.read_text(encoding="utf-8")
    block = _sys_sections_block(panel)
    lines_by_key = dict(_item_lines(block))

    for key in ADV_KEYS:
        assert key in lines_by_key, f"{key} not found in SYS_SECTIONS"
        assert "adv: true" in lines_by_key[key], f"{key} missing adv: true"


def test_function_switches_are_not_marked_advanced():
    panel = PANEL.read_text(encoding="utf-8")
    block = _sys_sections_block(panel)
    lines_by_key = dict(_item_lines(block))

    for key in NOT_ADV_KEYS:
        assert key in lines_by_key, f"{key} not found in SYS_SECTIONS"
        assert "adv: true" not in lines_by_key[key], f"{key} unexpectedly marked adv"


def test_advanced_items_are_all_in_an_allowlisted_section():
    """Every `adv: true` row must sit inside some section's `items:` array."""
    panel = PANEL.read_text(encoding="utf-8")
    block = _sys_sections_block(panel)
    adv_lines = [line for line in block.splitlines() if "adv: true" in line]

    assert len(adv_lines) == len(ADV_KEYS)
    for line in adv_lines:
        assert re.search(r'key:\s*"[^"]+"', line), line


def test_toggle_label_exists_in_all_six_languages():
    panel = PANEL.read_text(encoding="utf-8")

    assert panel.count('ctlAdvanced: "') == 6
    # The button is icon-only, so the label is its title and its aria-label.
    assert panel.count('_t("ctlAdvanced")') == 2


def test_toggle_state_is_local_storage_not_an_entity():
    panel = PANEL.read_text(encoding="utf-8")

    # Per card: each section owning `adv` rows keeps its own persisted state.
    assert '_ctlAdvKey(tk) { return "omnibattery:control-advanced:" + tk; }' in panel
    assert "_loadCtlAdv(tk)" in panel
    assert "_saveCtlAdv(tk, on)" in panel


def test_render_sys_sections_hides_adv_items_when_toggle_is_off():
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index("_renderSysSections(defs, store, empty) {")
    end = panel.index("_hourlyWarnEl()", start)
    body = panel[start:end]

    # The rows stay in the DOM; the card class hides them via CSS, so the
    # button can toggle in place without rebuilding the whole tab.
    assert 'card.classList.toggle("adv-off", !this._loadCtlAdv(sec.tk));' in body
    assert 'if (rows.some((r) => r.item.adv)) this._addAdvBtn(card, head, sec.tk);' in body
    assert 'if (r.item.adv) for (const n of nodes) n.classList?.add("adv-row");' in body
    assert ".card.adv-off .adv-row { display: none; }" in panel
