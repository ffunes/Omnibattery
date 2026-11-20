"""The excluded devices card: one block per device, its rows in order.

Rows were sorted by their full name, so a device's rows came in alphabetical,
language-dependent order with the options before the switch they need. Now
every per-device entity carries its device name and index; the panel groups
by device (a divider between devices), keeps the item order, resolves `dep`
within the device, tags the Enabled row with the device's type (excluded or
additional) and drops Exclusion % where it does not apply (additional devices).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from custom_components.omnibattery.number import ExcludedDeviceExclusionPctNumber
from custom_components.omnibattery.switch import (
    ExcludedDeviceCoverHomeSwitch,
    ExcludedDeviceDynamicPowerControlSwitch,
    ExcludedDeviceEnabledSwitch,
    ExcludedDeviceSolarSurplusSwitch,
)

PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")
DEVICES = [
    {"power_sensor": "sensor.wallbox_power", "included_in_consumption": True},
    {"power_sensor": "sensor.heat_pump_power", "included_in_consumption": False},
]


def _hass():
    names = {"sensor.wallbox_power": "Wallbox", "sensor.heat_pump_power": "Heat pump"}
    return SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(attributes={"friendly_name": names[entity_id]})
        )
    )


def _entry():
    return SimpleNamespace(data={"excluded_devices": [dict(d) for d in DEVICES]}, entry_id="e")


def test_every_per_device_entity_names_its_device():
    hass, entry = _hass(), _entry()
    for cls in (
        ExcludedDeviceEnabledSwitch,
        ExcludedDeviceSolarSurplusSwitch,
        ExcludedDeviceDynamicPowerControlSwitch,
        ExcludedDeviceCoverHomeSwitch,
        ExcludedDeviceExclusionPctNumber,
    ):
        attrs = cls(hass, entry, 1).extra_state_attributes
        assert attrs["excluded_device"] == "Heat pump", cls.__name__
        assert attrs["excluded_device_index"] == 1, cls.__name__
        assert attrs["type"] == "additional", cls.__name__


def test_the_enabled_switch_says_excluded_or_additional():
    hass, entry = _hass(), _entry()
    assert ExcludedDeviceEnabledSwitch(hass, entry, 0).extra_state_attributes["type"] == "excluded"
    assert ExcludedDeviceEnabledSwitch(hass, entry, 1).extra_state_attributes["type"] == "additional"


def test_exclusion_pct_applies_only_to_excluded_devices():
    hass, entry = _hass(), _entry()
    assert ExcludedDeviceExclusionPctNumber(hass, entry, 0).extra_state_attributes["applies"] is True
    assert ExcludedDeviceExclusionPctNumber(hass, entry, 1).extra_state_attributes["applies"] is False


def test_the_panel_groups_rows_per_device_in_item_order():
    panel = PANEL.read_text(encoding="utf-8")
    card = panel[panel.index('tk: "secExcluded"') :]
    card = card[: card.index("],")]

    assert '{ key: "excluded_device_enabled", domain: "switch", lk: "itemExcludedDevice", icon: "mdi:power-plug-off", useName: true, typeTag: true }' in card
    for key, dep in (
        ("excluded_device_solar_surplus", "excluded_device_enabled"),
        ("excluded_device_dynamic_power_control", "excluded_device_solar_surplus"),
        ("excluded_device_cover_home", "excluded_device_solar_surplus"),
        ("excluded_device_exclusion_pct", "excluded_device_enabled"),
    ):
        row = card[card.index(f'key: "{key}"') :]
        assert f'dep: "{dep}"' in row[: row.index("}")], key

    # grouped per device, sorted by device then item order, dividers per group
    assert "if (device != null) row.grp = `device${device}`;" in panel
    assert "|| order(a) - order(b)" in panel
    assert "const g = r.grp ?? r.item.grp;" in panel
    # dep within the device, never another device's switch
    assert "const parent = rowByKey.get(rowKey(d.grp, dep));" in panel
    assert "const other = d.grp == null ? this._sysEntityId(dep) : null;" in panel
    # rows that do not apply are not rendered; the type tag in six languages
    assert "st.attributes.applies !== false" in panel
    assert panel.count("typeExcluded:") == 6
    assert panel.count("typeAdditional:") == 6
