"""Behavioural check (#716): the overview sums one max-power value per battery.

Anker registers both a read-only sensor and a soft-limit number with the
translation_key ``max_charge_power`` / ``max_discharge_power``. The overview
bar must not count that single battery twice.
"""

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

const inst = Object.create(ctx.__cls.prototype);
const ent = (id, dev, tk) => [id, { entity_id: id, device_id: dev, translation_key: tk, platform: "omnibattery" }];
inst._hass = {
  entities: Object.fromEntries([
    ent("sensor.anker_max_charge_power", "anker", "max_charge_power"),
    ent("number.anker_max_charge_power", "anker", "max_charge_power"),
    ent("sensor.anker_max_discharge_power", "anker", "max_discharge_power"),
    ent("number.anker_max_discharge_power", "anker", "max_discharge_power"),
    ent("number.venus_max_charge_power", "venus", "max_charge_power"),
  ]),
  states: {
    "sensor.anker_max_charge_power": { state: "3500", attributes: {} },
    "number.anker_max_charge_power": { state: "3500", attributes: {} },
    "sensor.anker_max_discharge_power": { state: "3500", attributes: {} },
    "number.anker_max_discharge_power": { state: "2000", attributes: {} },
    "number.venus_max_charge_power": { state: "2500", attributes: {} },
  },
};
const byKey = new Map();
for (const e of Object.values(inst._hass.entities)) {
  if (!byKey.has(e.translation_key)) byKey.set(e.translation_key, []);
  byKey.get(e.translation_key).push(e.entity_id);
}
console.log(JSON.stringify({
  charge: inst._sum(byKey, "max_charge_power"),
  discharge: inst._sum(byKey, "max_discharge_power"),
  missing: inst._sum(byKey, "inverse_max_power"),
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_sum_counts_each_battery_once(tmp_path):
    script = tmp_path / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(script), str(PANEL)],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(out.strip().splitlines()[-1])

    # Anker 3500 (sensor + number, one battery) + Venus 2500 — not 9500.
    assert result["charge"] == 6000
    # Anker cap 3500 with a 2000 W soft limit: the effective ceiling is 2000.
    assert result["discharge"] == 2000
    assert result["missing"] is None
