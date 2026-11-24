"""Notifications card on the Control tab (frontend only).

Static text assertions on the raw panel source, same style as the other panel
tests: the four category switches, their order, the layout slot and the six
inlined languages (labels and help).
"""

import re
from pathlib import Path

import pytest


PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")
LANGS = ["en", "es", "ca", "de", "fr", "nl"]
ITEMS = [
    ("predictive_charging_notifications", "itemNotifyPlanCharge"),
    ("notify_plan_info", "itemNotifyPlanInfo"),
    ("notify_execution", "itemNotifyExecution"),
    ("notify_modes", "itemNotifyModes"),
]


def _panel():
    return PANEL.read_text(encoding="utf-8")


def _blocks(panel, const, end_marker):
    start = panel.index(f"const {const} = {{")
    end = panel.index(end_marker, start)
    idx = [panel.index(f"\n  {code}: {{", start, end) for code in LANGS]
    return [panel[idx[i] : idx[i + 1] if i + 1 < len(idx) else end] for i in range(len(idx))]


def test_the_notifications_section_has_the_four_switches_in_order():
    panel = _panel()
    start = panel.index('tk: "secNotifications"')
    section = panel[start : panel.index("\n  },", start)]
    found = re.findall(r'key:\s*"([^"]+)", domain: "switch", lk: "(\w+)"', section)
    assert found == ITEMS
    assert "gate" not in section


def test_the_section_is_in_the_layout():
    panel = _panel()
    layout = panel[panel.index("const SYS_LAYOUT = [") : panel.index("const DEFAULT_SYS_ORDER")]
    assert '"secNotifications"' in layout


def test_the_old_predictive_row_is_gone():
    panel = _panel()
    assert "itemPredictiveNotifications" not in panel
    start = panel.index('tk: "diagPredictive"')
    assert "predictive_charging_notifications" not in panel[start : panel.index('tk: "diagChargeDelay"', start)]


@pytest.mark.parametrize("lang", LANGS)
def test_labels_exist_in_every_language(lang):
    block = _blocks(_panel(), "I18N", "\n};")[LANGS.index(lang)]
    for lk in ["secNotifications"] + [lk for _, lk in ITEMS]:
        assert re.search(rf'\b{lk}: "[^"]+"', block), (lang, lk)


@pytest.mark.parametrize("lang", LANGS)
def test_help_exists_in_every_language(lang):
    block = _blocks(_panel(), "SYS_HELP", "\n};")[LANGS.index(lang)]
    for key, _ in ITEMS:
        assert re.search(rf'\n\s+{key}: "[^"]{{40,}}"', block), (lang, key)
