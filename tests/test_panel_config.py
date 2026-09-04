"""Tests for the sidebar panel's backend configuration payload."""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery import (
    _excluded_devices_panel_config,
    _has_battery_reported_solar,
    _panel_solar_entity,
)


class _EntityRegistry:
    def __init__(self, entity_ids: dict[str, str]) -> None:
        self._entity_ids = entity_ids

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str):
        assert domain == "switch"
        assert platform == "omnibattery"
        return self._entity_ids.get(unique_id)


class _SolarEntityRegistry:
    def __init__(self, entity_id: str | None = None) -> None:
        self.entity_id = entity_id

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str):
        if (
            domain == "sensor"
            and platform == "omnibattery"
            and unique_id == "marstek_venus_system_solar_power"
        ):
            return self.entity_id
        return None


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
            "power_sensor": "sensor.wallbox_power",
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
            "power_sensor": "sensor.wallbox_power",
            "included_in_consumption": True,
            "enabled": True,
            "enabled_entity": entity_id,
        }
    ]


def test_panel_solar_capability_excludes_max_ac():
    max_ac = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=False)
    )
    compatible_anker = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=True)
    )

    assert _has_battery_reported_solar([max_ac]) is False
    assert _has_battery_reported_solar([compatible_anker]) is True


def test_panel_solar_falls_back_to_external_without_internal_pv_entity():
    max_ac = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=False)
    )
    compatible_anker = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=True)
    )

    # Max AC must not select a stale/system aggregate, and an E5000 setup must
    # still use the configured meter until its aggregate entity is registered.
    assert _panel_solar_entity(
        [max_ac],
        _SolarEntityRegistry("sensor.omnibattery_system_solar_power"),
        "sensor.pv",
    ) == "sensor.pv"
    assert _panel_solar_entity(
        [compatible_anker], _SolarEntityRegistry(), "sensor.pv"
    ) == "sensor.pv"
    assert _panel_solar_entity(
        [compatible_anker],
        _SolarEntityRegistry("sensor.omnibattery_system_solar_power"),
        "sensor.pv",
    ) == "sensor.omnibattery_system_solar_power"
