"""Round-trip efficiency: the measured value instead of a hidden 0.85.

A stored 0.85 (the default) used to mean "use the measured value": the slider
showed 0.85 while about 0.82 was used, and a fixed 0.85 could not be set. Now
no stored value means measured, any stored value is used as given, and the
v20 -> v21 migration drops a stored 0.85 so existing installs keep behaving as
before. A number cannot be cleared, so the way back from a fixed value is an
action, which the panel's value editor calls.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.omnibattery import _async_register_services, async_migrate_entry
from custom_components.omnibattery.const import CONF_ROUND_TRIP_EFFICIENCY
from custom_components.omnibattery.energy import effective_round_trip_efficiency
from custom_components.omnibattery.number import ArbitrageNumber

DOMAIN = "omnibattery"
ACTION = "use_measured_round_trip_efficiency"
COMPONENT = Path("custom_components/omnibattery")
PANEL = COMPONENT / "frontend/marstek-panel.js"


def _battery(charged, discharged, capacity):
    return SimpleNamespace(
        data={
            "total_charging_energy": charged,
            "total_discharging_energy": discharged,
            "battery_total_energy": capacity,
        },
        capabilities=SimpleNamespace(independent_power_planes=False),
    )


# A Marstek with enough cycles (81.6 %), a Zendure without (excluded).
FLEET = [_battery(3442.8, 2808.0, 5.12), _battery(326.9, 283.6, 8.16)]


def _controller(stored=None):
    return SimpleNamespace(round_trip_efficiency=stored, coordinators=FLEET)


def test_no_stored_value_means_measured():
    value, source = effective_round_trip_efficiency(_controller())

    assert source == "measured"
    assert round(value, 4) == round(2808.0 / 3442.8, 4)


def test_a_stored_0_85_is_used_as_given():
    assert effective_round_trip_efficiency(_controller(0.85)) == (0.85, "configured")


class _Entries:
    def __init__(self, entries=()):
        self._entries = list(entries)

    def async_entries(self, domain):
        return list(self._entries)

    def async_update_entry(self, entry, *, data, version=None, title=None, options=None):
        entry.data = data
        if version is not None:
            entry.version = version


class _Services:
    def __init__(self):
        self.handlers = {}

    def has_service(self, domain, name):
        return (domain, name) in self.handlers

    def async_register(self, domain, name, handler, schema=None):
        self.handlers[(domain, name)] = handler


class _Controller:
    """Re-reads the entry on every access, as update_pd_parameters does on an update."""

    coordinators = FLEET

    def __init__(self, entry):
        self._entry = entry

    @property
    def round_trip_efficiency(self):
        return self._entry.data.get(CONF_ROUND_TRIP_EFFICIENCY)


def _slider(data):
    entry = SimpleNamespace(data=dict(data), entry_id="entry")
    controller = _Controller(entry)
    hass = SimpleNamespace(
        data={DOMAIN: {"entry": {"controller": controller}}},
        config_entries=_Entries([entry]),
        services=_Services(),
    )
    slider = ArbitrageNumber(hass, entry, "efficiency")
    slider.async_write_ha_state = lambda: None
    return hass, entry, slider


def test_the_slider_shows_the_measured_value_in_use():
    _hass, _entry, slider = _slider({})

    assert slider.native_value == round(2808.0 / 3442.8, 4)
    assert slider.extra_state_attributes == {"uses_default": True, "default_kind": "measured"}


async def test_a_value_set_on_the_slider_is_fixed_0_85_included():
    _hass, entry, slider = _slider({})

    await slider.async_set_native_value(0.85)

    assert entry.data[CONF_ROUND_TRIP_EFFICIENCY] == 0.85
    assert slider.native_value == 0.85
    assert slider.extra_state_attributes == {"uses_default": False, "default_kind": None}


async def test_the_action_drops_a_fixed_value():
    hass, entry, slider = _slider({CONF_ROUND_TRIP_EFFICIENCY: 0.85, "other": 1})
    _async_register_services(hass)

    await hass.services.handlers[(DOMAIN, ACTION)](SimpleNamespace(data={}))

    assert entry.data == {"other": 1}
    assert slider.extra_state_attributes == {"uses_default": True, "default_kind": "measured"}


async def test_the_action_leaves_a_measured_entry_alone():
    hass, entry, _slider_entity = _slider({"other": 1})
    untouched = entry.data
    _async_register_services(hass)

    await hass.services.handlers[(DOMAIN, ACTION)](SimpleNamespace(data={}))

    assert entry.data is untouched


def test_the_measured_value_is_polled_the_margin_is_not():
    # The measured efficiency moves with the batteries, not with the entry: an
    # entity refreshed only on entry updates showed a stale value and tag.
    entry = SimpleNamespace(data={}, entry_id="entry")
    hass = SimpleNamespace(data={})

    assert ArbitrageNumber(hass, entry, "efficiency").should_poll is True
    assert ArbitrageNumber(hass, entry, "margin").should_poll is False


def _no_registry():
    return patch.multiple(
        "homeassistant.helpers.entity_registry",
        async_get=lambda hass: SimpleNamespace(
            async_get_entity_id=lambda *args: None,
            async_remove=lambda *args: None,
        ),
        async_entries_for_config_entry=lambda reg, entry_id: [],
    )


async def _migrate(data, version=16):
    hass = SimpleNamespace(config_entries=_Entries())
    entry = SimpleNamespace(version=version, entry_id="entry", data=dict(data), options={})
    with _no_registry():
        assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 21
    return entry.data


async def test_the_migration_turns_the_hidden_0_85_into_measured():
    assert CONF_ROUND_TRIP_EFFICIENCY not in await _migrate({CONF_ROUND_TRIP_EFFICIENCY: 0.85})


async def test_the_migration_is_its_own_step_after_v20():
    # v20 is the solar safety margin's; an entry already there still gets this one.
    assert CONF_ROUND_TRIP_EFFICIENCY not in await _migrate(
        {CONF_ROUND_TRIP_EFFICIENCY: 0.85}, version=20
    )


async def test_the_migration_keeps_any_other_value():
    assert (await _migrate({CONF_ROUND_TRIP_EFFICIENCY: 0.9}))[CONF_ROUND_TRIP_EFFICIENCY] == 0.9


def test_the_panel_resets_in_the_value_editor_not_with_a_switch():
    panel = PANEL.read_text(encoding="utf-8")

    # One row for the setting: the slider, with the way back in its editor.
    assert panel.count('{ key: "round_trip_efficiency"') == 1
    assert "round_trip_efficiency_use_measured" not in panel
    assert f'reset: {{ service: "{ACTION}", lk: "actUseMeasured" }}' in panel
    assert "if (item.reset) valEl.__reset = item.reset;" in panel
    edit = panel[panel.index("_wireValueEdit(valEl, range, id, fmt) {"):]
    edit = edit[: edit.index("\n  }\n")]
    # offered only while a fixed value is set, and pressing it must not apply
    # the typed value first (the input's blur does that)
    assert "uses_default === false" in edit
    assert 'resetBtn.addEventListener("mousedown", (e) => e.preventDefault());' in edit
    assert 'this._hass.callService("omnibattery", reset.service, {});' in edit
    assert 'a.default_kind === "measured" ? "tagMeasured" : "tagDefault"' in panel
    for label in ("actUseMeasured:", "tagMeasured:", "tagDefault:"):
        assert panel.count(label) == 6


@pytest.mark.parametrize("name", ["strings.json"] + [f"translations/{lang}.json" for lang in ("ca", "de", "en", "es", "fr", "nl")])
def test_the_action_is_described_in_every_language(name):
    service = json.loads((COMPONENT / name).read_text(encoding="utf-8"))["services"][ACTION]

    assert service["name"] and service["description"]
    assert f"\n{ACTION}:" in (COMPONENT / "services.yaml").read_text(encoding="utf-8")


def test_an_unset_arbitrage_margin_reads_as_0_so_the_panel_shows_it():
    # unknown would be dropped by the panel; 0 and unset both mean off
    entry = SimpleNamespace(data={}, entry_id="entry")
    assert ArbitrageNumber(SimpleNamespace(data={}), entry, "margin").native_value == 0.0
    entry.data = {"min_arbitrage_margin": 0.05}
    assert ArbitrageNumber(SimpleNamespace(data={}), entry, "margin").native_value == 0.05
