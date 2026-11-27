"""The panel reads a per-battery ``aggregate_solar`` flag from the backend.

The flag replaces the model-name sniffing the panel used to do (#18, #60): the
driver decides, the ``battery_soc`` sensor publishes, the panel only reads it.
"""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.drivers import registry
from custom_components.omnibattery.drivers.anker import (
    _MAX_PRODUCT_CODES,
    _PRODUCT_LABELS,
    AnkerModbusDriver,
)
from custom_components.omnibattery.drivers.base import BatteryDriver
from custom_components.omnibattery.sensor import MarstekVenusSensor

PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js").resolve()


def _legacy_panel_rule(model: str) -> bool:
    """What the panel decided from the model text and manufacturer "Anker"."""
    import re

    normalized = model.strip().lower()
    if re.search(r"(?:^|\s)solarbank\s+(?:max|xe)\s+ac(?:$|\s)", normalized):
        return False
    return "solarbank 4 e5000 pro" in normalized


def _anker(product_code):
    driver = AnkerModbusDriver("1.2.3.4", 502, 1, client=SimpleNamespace())
    driver._product_code = product_code
    return driver


@pytest.mark.parametrize(
    "code", [c for c in _PRODUCT_LABELS if c not in _MAX_PRODUCT_CODES] + ["ZZZZ", None]
)
def test_anker_flag_matches_the_old_panel_rule(code):
    driver = _anker(code)
    assert driver.allows_aggregate_solar is _legacy_panel_rule(driver.model_label)
    assert driver.allows_aggregate_solar is driver.has_independent_pv


@pytest.mark.parametrize("code", sorted(_MAX_PRODUCT_CODES))
def test_solarbank_max_publishes_aggregate_solar(code):
    """The Solarbank Max postdates the old panel rule, which only knew the E5000 Pro.

    It is DC-coupled with its own PV input, so the aggregate PV value is a real
    source and the panel may read it (same as the E5000 Pro).
    """
    driver = _anker(code)
    assert driver.model_label == "Solarbank Max"
    assert driver.allows_aggregate_solar is True
    assert driver.allows_aggregate_solar is driver.has_independent_pv


def test_only_anker_overrides_the_default():
    for brand, cls in registry.DRIVERS.items():
        overridden = cls.allows_aggregate_solar is not BatteryDriver.allows_aggregate_solar
        assert overridden is (cls is AnkerModbusDriver), brand
    assert BatteryDriver.allows_aggregate_solar.fget(None) is True


@pytest.mark.parametrize("flag", [True, False])
def test_battery_soc_sensor_publishes_the_flag(flag):
    sensor = object.__new__(MarstekVenusSensor)
    sensor.definition = {"key": "battery_soc"}
    sensor.coordinator = SimpleNamespace(
        driver=SimpleNamespace(model_label="X", allows_aggregate_solar=flag),
        capabilities=SimpleNamespace(has_mppt_pv=False),
    )
    assert sensor.extra_state_attributes == {"model": "X", "aggregate_solar": flag}


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
console.log(JSON.stringify([
  inst._allowAggregateSolar({ attributes: { aggregate_solar: true } }),
  inst._allowAggregateSolar({ attributes: { aggregate_solar: false } }),
  inst._allowAggregateSolar({ attributes: { model: "Venus E" } }),
  inst._allowAggregateSolar({ attributes: {} }),
  inst._allowAggregateSolar(undefined),
]));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_panel_reads_the_flag(tmp_path):
    script = tmp_path / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(script), str(PANEL)], capture_output=True, text=True, check=True
    ).stdout
    assert json.loads(out.strip().splitlines()[-1]) == [True, False, False, False, False]
