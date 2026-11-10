"""A manual price threshold switched on without a value gets one.

The manual charge ceiling and discharge floor have no value until one is set,
so their number entities sit at "unknown". The panel dropped such rows as
registry leftovers, so turning the manual switch on showed nothing, and the
switch alone changed nothing either. Now the switch stores today's average
price as a starting value (the discharge floor's own fallback), kept on the
right side of the other threshold, and the panel renders an unset number
behind a `dep` switch as "—".
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    CONF_DISCHARGE_PRICE_THRESHOLD,
    CONF_DISCHARGE_PRICE_THRESHOLD_ENABLED,
    CONF_MAX_PRICE_THRESHOLD,
    CONF_MAX_PRICE_THRESHOLD_ENABLED,
)
from custom_components.omnibattery.switch import PriceThresholdEnabledSwitch

DOMAIN = "omnibattery"
PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")


class _Entries:
    def async_update_entry(self, entry, *, data):
        entry.data = data


def _switch(kind, data, average=0.4567):
    controller = SimpleNamespace(_dp_daily_avg_price=average)
    hass = SimpleNamespace(
        data={DOMAIN: {"entry": {"controller": controller}}},
        config_entries=_Entries(),
    )
    entry = SimpleNamespace(data=dict(data), entry_id="entry")
    switch = PriceThresholdEnabledSwitch(hass, entry, kind)
    switch.async_write_ha_state = lambda: None
    return switch, entry


@pytest.mark.parametrize(
    ("kind", "value_key"),
    [("charge", CONF_MAX_PRICE_THRESHOLD), ("discharge", CONF_DISCHARGE_PRICE_THRESHOLD)],
)
def test_turning_manual_on_stores_today_s_average(kind, value_key):
    switch, entry = _switch(kind, {})

    asyncio.run(switch.async_turn_on())

    assert entry.data[value_key] == 0.457
    assert switch.is_on


def test_a_stored_value_is_kept():
    switch, entry = _switch("charge", {CONF_MAX_PRICE_THRESHOLD: 0.30})

    asyncio.run(switch.async_turn_on())

    assert entry.data[CONF_MAX_PRICE_THRESHOLD] == 0.30


def test_the_charge_ceiling_starts_no_higher_than_an_active_floor():
    switch, entry = _switch(
        "charge",
        {CONF_DISCHARGE_PRICE_THRESHOLD: 0.40, CONF_DISCHARGE_PRICE_THRESHOLD_ENABLED: True},
    )

    asyncio.run(switch.async_turn_on())

    assert entry.data[CONF_MAX_PRICE_THRESHOLD] == 0.40


def test_the_discharge_floor_starts_no_lower_than_an_active_ceiling():
    switch, entry = _switch(
        "discharge",
        {CONF_MAX_PRICE_THRESHOLD: 0.50, CONF_MAX_PRICE_THRESHOLD_ENABLED: True},
    )

    asyncio.run(switch.async_turn_on())

    assert entry.data[CONF_DISCHARGE_PRICE_THRESHOLD] == 0.50


def test_without_prices_the_value_stays_unset():
    switch, entry = _switch("charge", {}, average=None)

    asyncio.run(switch.async_turn_on())

    assert CONF_MAX_PRICE_THRESHOLD not in entry.data
    assert entry.data[CONF_MAX_PRICE_THRESHOLD_ENABLED] is True


def test_turning_manual_off_keeps_the_value():
    switch, entry = _switch("charge", {CONF_MAX_PRICE_THRESHOLD: 0.30, CONF_MAX_PRICE_THRESHOLD_ENABLED: True})

    asyncio.run(switch.async_turn_off())

    assert entry.data[CONF_MAX_PRICE_THRESHOLD] == 0.30
    assert not switch.is_on


def test_the_panel_renders_an_unset_number_behind_its_switch():
    panel = PANEL.read_text(encoding="utf-8")

    assert 'const unset = item.dep && (item.domain || "number") === "number";' in panel
    assert 'const alive = item.domain === "button" || unset' in panel
