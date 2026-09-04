"""Regression tests for time-slot power override selector limits."""

from custom_components.omnibattery.config_flow import (
    _build_slot_step_b_schema,
    _finalize_slot,
)
from custom_components.omnibattery.const import MAX_BATTERIES, SLOT_BATTERY_SCOPE_ALL


def _power_maxes(schema) -> dict[str, int]:
    """Return selector maxima keyed by their form field."""
    return {
        marker.schema: selector.config["max"]
        for marker, selector in schema.schema.items()
        if "max_" in marker.schema
    }


def test_time_slot_power_override_uses_external_driver_limits():
    """Anker/SOLIX limits are persisted instead of defaulting to 2500 W."""
    schema = _build_slot_step_b_schema(
        needs_soc=False,
        needs_power=True,
        scope=SLOT_BATTERY_SCOPE_ALL,
        battery_configs=[
            {
                "brand": "anker",
                "max_charge_power": 3500,
                "max_discharge_power": 3500,
            }
        ],
        defaults={},
    )

    assert _power_maxes(schema) == {
        "battery_1__max_charge_power_w": 3500,
        "battery_1__max_discharge_power_w": 3500,
    }


def test_time_slot_power_override_keeps_directional_limits():
    """Asymmetric external-driver ceilings remain distinct in the form."""
    schema = _build_slot_step_b_schema(
        needs_soc=False,
        needs_power=True,
        scope=SLOT_BATTERY_SCOPE_ALL,
        battery_configs=[
            {
                "brand": "sessy",
                "max_charge_power": 2200,
                "max_discharge_power": 1700,
            }
        ],
        defaults={},
    )

    assert _power_maxes(schema) == {
        "battery_1__max_charge_power_w": 2200,
        "battery_1__max_discharge_power_w": 1700,
    }


def test_time_slot_power_override_keeps_marstek_version_envelope():
    """Versioned Marstek models continue to use their physical envelope."""
    schema = _build_slot_step_b_schema(
        needs_soc=False,
        needs_power=True,
        scope=SLOT_BATTERY_SCOPE_ALL,
        battery_configs=[
            {
                "battery_version": "v2",
                "max_charge_power": 3500,
                "max_discharge_power": 3500,
            }
        ],
        defaults={},
    )

    assert _power_maxes(schema) == {
        "battery_1__max_charge_power_w": 2500,
        "battery_1__max_discharge_power_w": 2500,
    }


def test_time_slot_power_override_renders_all_ten_batteries():
    """The dynamic per-battery form includes the tenth battery."""
    schema = _build_slot_step_b_schema(
        needs_soc=True,
        needs_power=True,
        scope=SLOT_BATTERY_SCOPE_ALL,
        battery_configs=[
            {
                "max_charge_power": 2500,
                "max_discharge_power": 2500,
            }
            for _ in range(MAX_BATTERIES)
        ],
        defaults={},
    )

    fields = {marker.schema for marker in schema.schema}
    assert len(fields) == MAX_BATTERIES * 4
    assert {
        "battery_10__soc_min",
        "battery_10__soc_max",
        "battery_10__max_charge_power_w",
        "battery_10__max_discharge_power_w",
    } <= fields


def test_finalize_slot_keeps_disabled_state_of_the_slot_it_replaces():
    """The per-slot enable switch has no form field, so it must be carried over."""
    step_a = {
        "start_time": "23:00",
        "end_time": "07:00",
        "days": ["mon"],
        "battery_scope": SLOT_BATTERY_SCOPE_ALL,
    }

    slot = _finalize_slot(step_a, None, {**step_a, "enabled": False})

    assert slot["enabled"] is False


def test_finalize_slot_defaults_to_enabled_for_a_brand_new_slot():
    step_a = {
        "start_time": "23:00",
        "end_time": "07:00",
        "days": ["mon"],
        "battery_scope": SLOT_BATTERY_SCOPE_ALL,
    }

    assert _finalize_slot(step_a, None)["enabled"] is True


def test_finalize_slot_does_not_hand_disabled_state_to_a_different_window():
    """Editing a slot into another window replaces it, so it starts enabled."""
    step_a = {
        "start_time": "11:00",
        "end_time": "15:00",
        "days": ["mon"],
        "battery_scope": SLOT_BATTERY_SCOPE_ALL,
    }
    stored = {
        "start_time": "23:00",
        "end_time": "07:00",
        "days": ["mon"],
        "battery_scope": SLOT_BATTERY_SCOPE_ALL,
        "enabled": False,
    }

    assert _finalize_slot(step_a, None, stored)["enabled"] is True
