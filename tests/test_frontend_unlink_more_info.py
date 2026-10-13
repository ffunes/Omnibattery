"""Behavioural check: a row stops opening more-info once its sensor is removed."""

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

const classes = new Set();
const el = {
  title: "",
  classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) },
  listeners: [],
  addEventListener(_t, fn) { this.listeners.push(fn); },
  removeEventListener(_t, fn) { this.listeners = this.listeners.filter((l) => l !== fn); },
  click() {
    const ev = { stopped: false, stopPropagation() { this.stopped = true; } };
    this.listeners.slice().forEach((fn) => fn(ev));
    return ev.stopped;
  },
};
const inst = Object.create(ctx.__cls.prototype);
const opened = [];
Object.assign(inst, { _t: (k) => k, _moreInfo: (id) => opened.push(id) });

inst._linkMoreInfo(el, "sensor.a");
const stoppedBefore = el.click();
const clickableBefore = classes.has("clickable");
inst._linkMoreInfo(el, null);
const stoppedAfter = el.click();
console.log(JSON.stringify({
  opened, stoppedBefore, stoppedAfter, clickableBefore,
  clickableAfter: classes.has("clickable"), listeners: el.listeners.length, title: el.title,
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_row_is_unlinked_when_its_sensor_is_removed(tmp_path):
    script = tmp_path / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(script), str(PANEL)],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(out.strip().splitlines()[-1])

    assert result["opened"] == ["sensor.a"]
    assert result["clickableBefore"] is True
    assert result["stoppedBefore"] is True
    assert result["stoppedAfter"] is False
    assert result["clickableAfter"] is False
    assert result["listeners"] == 0
    assert result["title"] == ""
