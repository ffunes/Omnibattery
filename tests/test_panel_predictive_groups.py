"""Group dividers on the predictive-charging card (frontend only).

The card stacks ~20 rows; items carry a `grp` tag and the renderer draws a
`.sys-sep` line whenever `grp` changes between two live rows. Static text
assertions on the raw panel source, same style as the other panel tests.
"""

import re
from pathlib import Path

import pytest


PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")

# Expected group of each predictive item, in render order: one group per
# feature (its switch, its sliders and its status row), buying first, then
# using, selling and the solar-surplus features.
EXPECTED = [
    ("predictive_charging", "base"),
    ("predictive_charging_notifications", "base"),
    ("predictive_charging_active", "base"),
    ("discharge_reserve_status", "base"),
    ("predictive_safety_margin_pct", "solar"),
    ("min_soc_floor_enabled", "floor"),
    ("predictive_min_soc_floor", "floor"),
    ("max_price_threshold_enabled", "chargePrice"),
    ("max_price_threshold", "chargePrice"),
    ("min_arbitrage_margin", "chargePrice"),
    ("round_trip_efficiency", "chargePrice"),
    ("negative_price_charging", "negativePrice"),
    ("price_discharge_control", "dischargePrice"),
    ("discharge_price_threshold_enabled", "dischargePrice"),
    ("discharge_price_threshold", "dischargePrice"),
    ("high_price_sale", "highPrice"),
    ("high_price_discharge_status", "highPrice"),
    ("surplus_price_hold", "surplus"),
    ("surplus_hold_min_saving", "surplus"),
    ("surplus_price_hold_status", "surplus"),
    ("smart_predischarge", "predischarge"),
    ("negative_injection_threshold", "predischarge"),
    ("predischarge_reserve_soc", "predischarge"),
    ("curtailment_status", "predischarge"),
    ("reevaluate_dynamic_pricing", "action"),
]


def _predictive_items(panel: str):
    start = panel.index('tk: "diagPredictive"')
    end = panel.index('tk: "diagChargeDelay"', start)
    for line in panel[start:end].splitlines():
        m = re.search(r'key:\s*"([^"]+)"', line)
        if m:
            yield m.group(1), re.search(r'grp:\s*"([^"]+)"', line)


def test_every_predictive_item_is_tagged_in_the_expected_group():
    found = [(k, m.group(1) if m else None) for k, m in _predictive_items(PANEL.read_text(encoding="utf-8"))]
    assert found == EXPECTED


def test_renderer_draws_a_divider_on_group_change():
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index("_renderSysSections(defs, store, empty) {")
    body = panel[start : panel.index("_hourlyWarnEl()", start)]

    # Only between two live rows (no leading/orphan line), not between groups of
    # one heading, gated like a row, and hidden with Advanced off when the whole
    # block it opens is advanced.
    assert "if (g && lastGrp && g !== lastGrp && !(heading && heading === sec.grpTitles[lastGrp])) {" in body
    assert 'sep.className = "sys-sep";' in body
    assert "const allAdv = () => rows.filter(inBlock).every((x) => x.item.adv);" in body
    assert 'if (allAdv()) sep.classList.add("adv-row");' in body
    assert "gatedNodes.push(sep);" in body
    assert ".sys-sep { grid-column: 1 / -1;" in panel


def test_price_thresholds_hide_behind_their_manual_switch():
    # #612: each threshold row names its manual switch as `dep`, and that switch
    # is a row of the same card (the renderer only links deps within a card).
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index('tk: "diagPredictive"')
    card = panel[start : panel.index('tk: "diagChargeDelay"', start)]
    for key in ("max_price_threshold", "discharge_price_threshold"):
        row = re.search(rf'\{{ key: "{key}",[^}}]*\}}', card).group(0)
        assert f'dep: "{key}_enabled"' in row
        assert f'key: "{key}_enabled", domain: "switch"' in card
    assert 'n.classList?.toggle("sub-off", !shown)' in panel
    assert ".card .sub-off { display: none; }" in panel


def test_the_discharge_floor_hides_with_its_feature():
    # The floor is read only by "Discharge only above price": it hides behind
    # its manual switch, which itself hides behind the feature (a chain).
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index('tk: "diagPredictive"')
    card = panel[start : panel.index('tk: "diagChargeDelay"', start)]
    manual = re.search(r'\{ key: "discharge_price_threshold_enabled",[^}]*\}', card).group(0)
    assert 'dep: "price_discharge_control"' in manual

    body = panel[panel.index("_renderSysSections(defs, store, empty) {") :]
    body = body[: body.index("_hourlyWarnEl()")]
    # every switch on the way up must be on, and each of them re-checks the card
    assert "dep = parent.item.dep;" in body
    assert "const shown = d.ids.every(on);" in body
    assert "w.depRefresh = refreshDeps;" in body
    assert "if (w.depRefresh) w.depRefresh();" in panel


@pytest.mark.parametrize(
    ("key", "text"),
    [("max_price_threshold", "valAutoCharge"), ("discharge_price_threshold", "valAutoDischarge")],
)
def test_a_threshold_on_automatic_says_so(key, text):
    # With its manual switch off the slider hides; an "Automatic" line takes
    # its place (only while every switch above that one is on).
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index('tk: "diagPredictive"')
    card = panel[start : panel.index('tk: "diagChargeDelay"', start)]
    row = re.search(rf'\{{ key: "{key}",[^}}]*\}}', card).group(0)
    assert f'auto: "{text}"' in row
    assert panel.count(f"{text}: ") == 6

    body = panel[panel.index("_renderSysSections(defs, store, empty) {") :]
    body = body[: body.index("_hourlyWarnEl()")]
    assert "const auto = r.item.auto ? this._sysAutoLine(nodes[0], r.item) : null;" in body
    assert "gatedNodes.push(...nodes, ...(auto || []));" in body
    assert "const autoShown = !shown && d.ids.slice(1).every(on);" in body
    assert 'for (const n of d.auto || []) n.classList.toggle("sub-off", !autoShown);' in body
    assert "val.textContent = this._t(item.auto);" in panel


@pytest.mark.parametrize(
    ("setting", "feature"),
    [
        ("predictive_min_soc_floor", "min_soc_floor_enabled"),
        ("surplus_hold_min_saving", "surplus_price_hold"),
        ("negative_injection_threshold", "smart_predischarge"),
        ("predischarge_reserve_soc", "smart_predischarge"),
    ],
)
def test_a_feature_s_settings_show_only_while_it_is_on(setting, feature):
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index('tk: "diagPredictive"')
    card = panel[start : panel.index('tk: "diagChargeDelay"', start)]
    row = re.search(rf'\{{ key: "{setting}",[^}}]*\}}', card).group(0)
    assert f'dep: "{feature}"' in row
    assert f'key: "{feature}", domain: "switch"' in card


@pytest.mark.parametrize(
    ("key", "lo", "hi"),
    [
        ("max_price_threshold", -0.5, 1.0),
        ("discharge_price_threshold", -0.5, 1.0),
        ("min_arbitrage_margin", 0, 0.3),
        ("negative_injection_threshold", -0.5, 0.5),
        ("surplus_hold_min_saving", 0, 0.2),
    ],
)
def test_per_kwh_sliders_have_a_draggable_range(key, lo, hi):
    # The entities span up to -2..2 in 0.001 steps (thousands of positions);
    # the panel slider gets a narrow range, the typed value keeps the full one.
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index('tk: "diagPredictive"')
    card = panel[start : panel.index('tk: "diagChargeDelay"', start)]
    row = re.search(rf'\{{ key: "{key}",[^}}]*\}}', card).group(0)
    assert f"range: [{lo}, {hi}, 0.01]" in row
    assert "store[sk] = { type: \"number\", el: range, val: valEl, def: defEl, step, unit, range: item.range };" in panel
    assert "const [lo, hi, sliderStep] = w.range;" in panel


def test_status_rows_are_named_as_status():
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index('tk: "diagPredictive"')
    card = panel[start : panel.index('tk: "diagChargeDelay"', start)]
    for key, lk in (
        ("surplus_price_hold_status", "statusSurplusHold"),
        ("discharge_reserve_status", "statusDischargeReserve"),
        ("curtailment_status", "statusPredischarge"),
        ("high_price_discharge_status", "diagHighPriceDischarge"),
    ):
        row = re.search(rf'\{{ key: "{key}",[^}}]*\}}', card).group(0)
        assert f'lk: "{lk}"' in row
    assert panel.count("statusSurplusHold:") == 6


def test_the_weekly_delay_shows_only_with_charge_delay_on():
    # It acts only inside the Charge Delay checks; that switch is on another
    # card, so the chain looks it up globally and every patch re-checks it.
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index('tk: "secWeeklyFull"')
    card = panel[start : panel.index("],", start)]
    row = re.search(r'\{ key: "weekly_full_charge_delay",[^}]*\}', card).group(0)
    assert 'dep: "charge_delay"' in row
    assert "const other = d.grp == null ? this._sysEntityId(dep) : null;" in panel
    assert "store[DEP_REFRESH].push(refreshDeps);" in panel
    assert "for (const refresh of this[storeKey][DEP_REFRESH] || []) refresh();" in panel


def test_predictive_and_weekly_cards_show_their_status():
    panel = PANEL.read_text(encoding="utf-8")
    assert '{ key: "predictive_charging_active", domain: "binary_sensor", lk: "statusPredictive"' in panel
    weekly = panel[panel.index('tk: "secWeeklyFull"') :]
    weekly = weekly[: weekly.index("],")]
    assert '{ key: "weekly_full_charge", domain: "sensor", lk: "statusWeekly"' in weekly
    assert panel.count("statusPredictive:") == 6
    assert panel.count("statusWeekly:") == 6


# Groups whose first row turns the whole group on or off (bold label), and the
# three headings by intent, each over the groups that name it.
LEADS = {
    "base": "predictive_charging",
    "floor": "min_soc_floor_enabled",
    "negativePrice": "negative_price_charging",
    "dischargePrice": "price_discharge_control",
    "highPrice": "high_price_sale",
    "surplus": "surplus_price_hold",
    "predischarge": "smart_predischarge",
}
TITLES = {
    "solar": "grpPlanning", "floor": "grpPlanning",
    "chargePrice": "grpPriceTrade", "negativePrice": "grpPriceTrade", "dischargePrice": "grpPriceTrade",
    "highPrice": "grpGridSale", "surplus": "grpGridSale", "predischarge": "grpGridSale",
}


def test_every_feature_group_starts_with_a_bold_line():
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index('tk: "diagPredictive"')
    card = panel[start : panel.index('tk: "diagChargeDelay"', start)]
    leads = {k for k, _ in _predictive_items(panel) if "lead: true" in re.search(rf'\{{ key: "{k}",[^}}]*\}}', card).group(0)}
    assert leads == set(LEADS.values())
    # each lead is its group's first row
    first = {}
    for key, grp in EXPECTED:
        first.setdefault(grp, key)
    for grp, key in LEADS.items():
        assert first[grp] == key
    # every group but the base and the action button sits under a heading,
    # and the headings come in order: no group of another heading in between
    block = card[card.index("grpTitles: {") : card.index("},", card.index("grpTitles: {"))]
    assert dict(re.findall(r'(\w+): "(grp\w+)"', block)) == TITLES
    assert set(first) - set(TITLES) == {"base", "action"}
    order = [TITLES[grp] for grp in first if grp in TITLES]
    assert [lk for i, lk in enumerate(order) if i == 0 or order[i - 1] != lk] == ["grpPlanning", "grpPriceTrade", "grpGridSale"]
    for lk in set(TITLES.values()):
        assert panel.count(f" {lk}: ") == 6
    assert "grpSolar" not in panel and "grpChargePrice" not in panel


def test_renderer_draws_the_heading_and_the_bold_lead():
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index("_renderSysSections(defs, store, empty) {")
    body = panel[start : panel.index("_hourlyWarnEl()", start)]
    # once per heading, above the first of its groups that has a row; gated
    # like a row, hidden with Advanced off when every row under it is advanced
    assert "const title = g && g !== lastGrp && sec.grpTitles && sec.grpTitles[g];" in body
    assert "if (title && title !== lastTitle) {" in body
    assert "if (title) lastTitle = title;" in body
    assert 'h.className = "sys-grp-title";' in body
    assert "const under = rows.filter((x) => sec.grpTitles[x.grp ?? x.item.grp] === title);" in body
    assert 'if (under.every((x) => x.item.adv)) h.classList.add("adv-row");' in body
    assert "gatedNodes.push(h);" in body
    assert 'if (item.lead || item.gate) k.classList.add("ctl-k-lead");' in panel
    # a sub-switch's value hides with it, as on the predictive card
    assert '{ key: "delay_soc_setpoint", lk: "itemDelaySoc", icon: "mdi:battery-charging-50", dep: "delay_soc_setpoint_enabled" }' in panel
    assert ".sys-grp-title { grid-column: 1 / -1;" in panel
    assert ".sys-grid .ctl-k-lead {" in panel
