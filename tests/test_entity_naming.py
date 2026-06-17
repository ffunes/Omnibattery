"""Unit tests for the language-independent entity_id helper.

These pin the contract that ``entity_id`` slugs are built from the English
``key`` (not the localized display name), so new installs get consistent ids
regardless of the UI language. No hardware, no ``hass`` fixture.
"""
from __future__ import annotations

from custom_components.marstek_venus_energy_manager.entity_naming import (
    english_entity_id,
)


def test_per_battery_slug():
    assert (
        english_entity_id("sensor", "Marstek Venus 1", "ac_power")
        == "sensor.marstek_venus_1_ac_power"
    )


def test_system_slug():
    assert (
        english_entity_id("switch", "Marstek Venus System", "predictive_charging")
        == "switch.marstek_venus_system_predictive_charging"
    )


def test_indexed_key_keeps_index():
    assert (
        english_entity_id("switch", "Marstek Venus System", "time_slot_0_enabled")
        == "switch.marstek_venus_system_time_slot_0_enabled"
    )


def test_deterministic():
    # Same English key in -> same id out, independent of any UI language.
    assert english_entity_id("number", "Marstek Venus 2", "max_soc") == english_entity_id(
        "number", "Marstek Venus 2", "max_soc"
    )
