"""Regression coverage for Venus A/D installations without connected panels."""
from pathlib import Path


PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")


def test_panel_keeps_ac_power_when_mppt_inputs_are_declared_unused():
    source = PANEL.read_text(encoding="utf-8")

    assert "socObj.attributes?.dc_pv_connected" in source
    assert "dcPvConnected !== false && mppt.some" in source
    assert "dcPvConnected === false && batteryW != null" not in source
    assert ": acW != null\n            ? -acW" in source
