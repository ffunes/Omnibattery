"""Behavioural check: forecast rows get linked when `panel` arrives after `hass`."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js").resolve()

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
let Cls = null;
const ctx = {
  HTMLElement: class {},
  customElements: { get: () => null, define: (_n, c) => { Cls = c; } },
  document: {}, window: {}, console,
};
vm.createContext(ctx);
const src = fs.readFileSync(process.argv[2], "utf8").replace(/import\.meta\.url/g, '"file:///"');
vm.runInContext(src + "\n;this.__cls = MarstekVenusPanel;", ctx);
Cls = ctx.__cls;

const row = () => ({
  classList: { add() {}, remove() {} },
  listeners: [],
  addEventListener(_t, fn) { this.listeners.push(fn); },
  removeEventListener(_t, fn) { this.listeners = this.listeners.filter((l) => l !== fn); },
});
const inst = Object.create(Cls.prototype);
const opened = [];
Object.assign(inst, {
  _panelConfig: {},
  _r: { dForecastRow: row(), dRemainingRow: row() },
  _t: (k) => k,
  _applyTheme() {}, _update() {}, _ensureHistoryStarted() {},
  _moreInfo: (id) => opened.push(id),
});

inst.hass = { states: {} };
const before = inst._r.dForecastRow._moreInfoEntity;
inst.panel = { config: { solar_forecast_entity: "sensor.f", solar_forecast_remaining_entity: "sensor.r" } };
inst.panel = { config: { solar_forecast_entity: "sensor.f2", solar_forecast_remaining_entity: "sensor.r" } };
const f = inst._r.dForecastRow, r = inst._r.dRemainingRow;
f.listeners.forEach((fn) => fn({ stopPropagation() {} }));
r.listeners.forEach((fn) => fn({ stopPropagation() {} }));
console.log(JSON.stringify({
  before: before || null, forecast: f._moreInfoEntity, remaining: r._moreInfoEntity,
  listeners: [f.listeners.length, r.listeners.length], opened,
}));

const f2 = row(), r2 = row();
inst._r = { dForecastRow: f2, dRemainingRow: r2 };
inst.panel = { config: { solar_forecast_entity: "sensor.only" } };
console.log(JSON.stringify({ forecast: f2._moreInfoEntity || null, remaining: r2._moreInfoEntity || null }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_forecast_links_refresh_when_panel_arrives_after_hass(tmp_path):
    script = tmp_path / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(script), str(PANEL)],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(out.strip().splitlines()[-2])
    only = json.loads(out.strip().splitlines()[-1])

    assert result["before"] is None
    assert result["forecast"] == "sensor.f2"
    assert result["remaining"] == "sensor.r"
    assert result["listeners"] == [1, 1]
    assert result["opened"] == ["sensor.f2", "sensor.r"]
    # A missing sensor leaves its row unlinked instead of borrowing the other.
    assert only == {"forecast": "sensor.only", "remaining": None}
