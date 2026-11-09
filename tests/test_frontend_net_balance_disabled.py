"""Behavioural check: the net-balance row shows "Disabled" while its switch is off."""

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

const [lang, switchState, value] = JSON.parse(process.argv[3]);
const states = { "sensor.net_balance": { entity_id: "sensor.net_balance", state: value, attributes: {} } };
const entities = {
  "sensor.net_balance": { entity_id: "sensor.net_balance", platform: "omnibattery", translation_key: "balance_neto", device_id: "sys" },
};
if (switchState != null) {
  states["switch.hourly_balance"] = { entity_id: "switch.hourly_balance", state: switchState, attributes: {} };
  entities["switch.hourly_balance"] = { entity_id: "switch.hourly_balance", platform: "omnibattery", translation_key: "hourly_balance", device_id: "sys" };
}
const inst = Object.create(ctx.__cls.prototype);
Object.assign(inst, {
  _panelConfig: {},
  _hass: { states, entities, devices: {}, language: lang, locale: { language: lang } },
  _lang: () => lang,
});
const m = inst._model();
const row = inst._diagDisplay("balance_neto", m.diagStates.balance_neto, m);
console.log(JSON.stringify(row));
"""


def _render(tmp_path, lang, switch_state, value):
    script = tmp_path / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(script), str(PANEL), json.dumps([lang, switch_state, value])],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize(
    ("lang", "expected"),
    [("en", "Disabled"), ("nl", "Uitgeschakeld"), ("es", "Desactivado")],
)
def test_switched_off_net_balance_shows_disabled(tmp_path, lang, expected):
    assert _render(tmp_path, lang, "off", "0.0") == {"text": expected, "tone": "neutral"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_switched_on_net_balance_shows_the_hour_balance(tmp_path):
    assert _render(tmp_path, "en", "on", "0.0") == {"text": "+0.00 kWh", "tone": "good"}
    assert _render(tmp_path, "en", "on", "-0.25") == {"text": "-0.25 kWh", "tone": "warn"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_net_balance_without_a_switch_shows_the_value(tmp_path):
    assert _render(tmp_path, "en", None, "0.12") == {"text": "+0.12 kWh", "tone": "good"}
