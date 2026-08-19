"""Tests for the sidebar panel's backend configuration payload."""
from __future__ import annotations

from custom_components.omnibattery import _excluded_devices_panel_config


class _EntityRegistry:
    def __init__(self, entity_ids: dict[str, str]) -> None:
        self._entity_ids = entity_ids

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str):
        assert domain == "switch"
        assert platform == "omnibattery"
        return self._entity_ids.get(unique_id)


def test_excluded_devices_panel_config_survives_disabled_switch():
    """Persisted flow data must not depend on the switch having a HA state."""
    data = {
        "excluded_devices": [
            {
                "power_sensor": "sensor.wallbox_power",
                "included_in_consumption": False,
                "enabled": True,
            }
        ]
    }

    assert _excluded_devices_panel_config(data, _EntityRegistry({})) == [
        {
            "name": None,
            "power_sensor": "sensor.wallbox_power",
            "activity_sensor": None,
            "ev_charger_no_telemetry": False,
            "allow_solar_surplus": False,
            "dynamic_power_control": False,
            "cover_home_when_active": False,
            "included_in_consumption": False,
            "enabled": True,
            "enabled_entity": None,
        }
    ]


def test_excluded_devices_panel_config_resolves_live_switch_entity():
    """The payload identifies the switch used for immediate runtime toggles."""
    unique_id = "marstek_venus_system_excluded_device_enabled_0"
    entity_id = "switch.garage_wallbox_enabled"

    result = _excluded_devices_panel_config(
        {"excluded_devices": [{"power_sensor": "sensor.wallbox_power"}]},
        _EntityRegistry({unique_id: entity_id}),
    )

    assert result == [
        {
            "name": None,
            "power_sensor": "sensor.wallbox_power",
            "activity_sensor": None,
            "ev_charger_no_telemetry": False,
            "allow_solar_surplus": False,
            "dynamic_power_control": False,
            "cover_home_when_active": False,
            "included_in_consumption": True,
            "enabled": True,
            "enabled_entity": entity_id,
        }
    ]
