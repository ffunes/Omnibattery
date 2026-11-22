"""Behavioural check: a status row shows "Disabled" while its feature is switched off."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js").resolve()

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const ctx = {
  HTMLElement: class {},
  customElements: { get: () => null, define: () => {} },
  document: {}, window: {}, console,
};
vm.createContext(ctx);
const src = fs.readFileSync(process.argv[2], "utf8").replace(/import\.meta\.url/g, '"file:///"');
vm.runInContext(src + "\n;this.__cls = MarstekVenusPanel;", ctx);

const [lang, rowKey, rowDomain, rowState, switchKey, switchState] = JSON.parse(process.argv[3]);
const states = {};
const entities = {};
const add = (domain, key, state) => {
  const id = `${domain}.${key}`;
  states[id] = { entity_id: id, state, attributes: {} };
  entities[id] = { entity_id: id, platform: "omnibattery", translation_key: key, device_id: "sys" };
};
add(rowDomain, rowKey, rowState);
if (switchKey != null && switchState != null) add("switch", switchKey, switchState);
const inst = Object.create(ctx.__cls.prototype);
Object.assign(inst, {
  _panelConfig: {},
  _hass: {
    states, entities, devices: {}, language: lang, locale: { language: lang },
    // Stand-in for Home Assistant's own localisation of the entity state.
    formatEntityState: (so) => `ha:${so.state}`,
  },
  _lang: () => lang,
});
const m = inst._model();
console.log(JSON.stringify(inst._diagDisplay(rowKey, m.diagStates[rowKey], m)));
"""

# Status row (translation key of its binary sensor) -> the feature's switch.
SWITCHED_ROWS = [
    ("predictive_charging_active", "predictive_charging"),
    ("curtailment_status", "smart_predischarge"),
    ("surplus_price_hold_status", "surplus_price_hold"),
    ("capacity_protection_active", "capacity_protection"),
]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _render(tmp_path, lang, row_key, row_domain, row_state, switch_key=None, switch_state=None):
    script = tmp_path / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    args = [lang, row_key, row_domain, row_state, switch_key, switch_state]
    out = subprocess.run(
        ["node", str(script), str(PANEL), json.dumps(args)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out.strip().splitlines()[-1])


@pytest.mark.parametrize(("row", "switch"), SWITCHED_ROWS)
def test_switched_off_feature_shows_disabled(tmp_path, row, switch):
    shown = _render(tmp_path, "en", row, "binary_sensor", "off", switch, "off")
    assert shown == {"text": "Disabled", "tone": "neutral"}


@pytest.mark.parametrize(("lang", "expected"), [("de", "Deaktiviert"), ("nl", "Uitgeschakeld")])
def test_disabled_is_localised(tmp_path, lang, expected):
    shown = _render(
        tmp_path, lang, "capacity_protection_active", "binary_sensor", "off", "capacity_protection", "off"
    )
    assert shown == {"text": expected, "tone": "neutral"}


@pytest.mark.parametrize(("row", "switch"), SWITCHED_ROWS)
def test_switched_on_feature_shows_the_entity_state(tmp_path, row, switch):
    idle = _render(tmp_path, "en", row, "binary_sensor", "off", switch, "on")
    assert idle == {"text": "ha:off", "tone": "neutral"}
    acting = _render(tmp_path, "en", row, "binary_sensor", "on", switch, "on")
    assert acting == {"text": "ha:on", "tone": "good"}


@pytest.mark.parametrize(("row", "switch"), SWITCHED_ROWS)
def test_row_without_a_switch_entity_shows_the_entity_state(tmp_path, row, switch):
    shown = _render(tmp_path, "en", row, "binary_sensor", "off")
    assert shown == {"text": "ha:off", "tone": "neutral"}


@pytest.mark.parametrize(("row", "switch"), SWITCHED_ROWS)
def test_a_row_still_acting_is_never_shown_as_disabled(tmp_path, row, switch):
    shown = _render(tmp_path, "en", row, "binary_sensor", "on", switch, "off")
    assert shown == {"text": "ha:on", "tone": "good"}


def test_discharge_window_without_slots_keeps_its_state(tmp_path):
    # Without slots discharge is allowed all day: "Disabled" would read as blocked.
    shown = _render(tmp_path, "en", "discharge_window", "sensor", "no_slots")
    assert shown == {"text": "ha:no_slots", "tone": "neutral"}


def test_discharge_window_with_slots_shows_its_state(tmp_path):
    outside = _render(tmp_path, "en", "discharge_window", "sensor", "inactive")
    assert outside == {"text": "ha:inactive", "tone": "neutral"}
    inside = _render(tmp_path, "en", "discharge_window", "sensor", "active")
    assert inside == {"text": "ha:active", "tone": "good"}
