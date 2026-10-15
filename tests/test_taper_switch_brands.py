"""The 100% charge voltage taper switch only exists for Marstek batteries.

The taper triggers on per-cell voltages, which only the Marstek drivers report.
For every other brand the coordinator forces the setting off at startup and the
config flow hides the option, so a dashboard switch would show a state that has
no effect.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import DOMAIN
from custom_components.omnibattery.switch import (
    BatteryFullChargeVoltageTaperSwitch,
    async_setup_entry,
)


def _setup_switches(brand: str) -> list:
    coordinator = SimpleNamespace(
        brand=brand,
        name="Battery",
        device_key=f"{brand}_1",
        switch_definitions=[],
    )
    controller = SimpleNamespace(weekly_full_charge_enabled=False)
    entry = SimpleNamespace(entry_id="test-entry", data={})
    hass = SimpleNamespace(
        data={DOMAIN: {"test-entry": {"coordinators": [coordinator], "controller": controller}}}
    )
    added: list = []
    asyncio.run(async_setup_entry(hass, entry, lambda ents: added.extend(ents)))
    return added


def test_taper_switch_created_for_marstek():
    added = _setup_switches("marstek")
    assert any(isinstance(e, BatteryFullChargeVoltageTaperSwitch) for e in added)


@pytest.mark.parametrize("brand", ["zendure", "anker", "sessy", "hoymiles", "huawei", "indevolt"])
def test_taper_switch_absent_for_other_brands(brand):
    added = _setup_switches(brand)
    assert not any(isinstance(e, BatteryFullChargeVoltageTaperSwitch) for e in added)
