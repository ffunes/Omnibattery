"""Group dividers on the predictive-charging card (frontend only).

The card stacks ~20 rows; items carry a `grp` tag and the renderer draws a
`.sys-sep` line whenever `grp` changes between two live rows. Static text
assertions on the raw panel source, same style as the other panel tests.
"""

import re
from pathlib import Path


PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")

# Expected group of each predictive item, in render order.
EXPECTED = [
    ("predictive_charging", "base"),
    ("predictive_safety_margin_kwh", "base"),
    ("min_soc_floor_enabled", "base"),
    ("predictive_min_soc_floor", "base"),
    ("price_discharge_control", "price"),
    ("max_price_threshold", "price"),
    ("discharge_price_threshold", "price"),
    ("min_arbitrage_margin", "price"),
    ("round_trip_efficiency", "price"),
    ("negative_price_charging", "price"),
    ("smart_predischarge", "predischarge"),
    ("negative_injection_threshold", "predischarge"),
    ("predischarge_reserve_soc", "predischarge"),
    ("surplus_price_hold", "export"),
    ("surplus_hold_min_saving", "export"),
    ("high_price_discharge", "export"),
    ("discharge_reserve", "export"),
    ("discharge_reserve_min_saving", "export"),
    ("curtailment_status", "status"),
    ("surplus_price_hold_status", "status"),
    ("high_price_discharge_status", "status"),
    ("discharge_reserve_status", "status"),
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

    # Only between two live rows (no leading/orphan line), gated like a row, and
    # hidden with Advanced off when the whole group it opens is advanced.
    assert "if (g && lastGrp && g !== lastGrp) {" in body
    assert 'sep.className = "sys-sep";' in body
    assert 'rows.filter((x) => x.item.grp === g).every((x) => x.item.adv)) sep.classList.add("adv-row")' in body
    assert "gatedNodes.push(sep);" in body
    assert ".sys-sep { grid-column: 1 / -1;" in panel
