"""The observed battery's form and what it stores.

The three snapshots in ``tests/snapshots/config_entry/`` already pin the result
of driving the real flow. What they cannot show is the *shape* of the form and
the editing behaviour behind it, which is where this brand's one field-reported
regression lived: an options flow clicked straight through used to erase every
optional entity, because the optional selectors rendered empty and submitted
nothing. These tests drive ``_schema`` / ``_store`` directly, so they need no
Home Assistant runtime.
"""
from __future__ import annotations

import voluptuous as vol

from custom_components.omnibattery.drivers.generic_ha_flow import _schema, _store

SOC = "sensor.scb_battery_soc"
POWER = "sensor.scb_battery_power"
CHARGED = "sensor.scb_battery_charge_total"
DISCHARGED = "sensor.scb_battery_discharge_total"
TARGET = "number.scb_battery_min_soc"

OPTIONAL_KEYS = (
    "ha_charge_energy_entities",
    "ha_discharge_energy_entity",
    "ha_target_soc_entity",
)


def _markers(defaults: dict) -> dict:
    """``{field name: marker}`` for the form built from ``defaults``."""
    return {str(marker): marker for marker in _schema(defaults)}


def _saved() -> dict:
    """A battery as an earlier run of the flow stored it, fully populated."""
    return _store(
        {
            "name": "Observed",
            "ha_soc_entity": SOC,
            "ha_power_entity": POWER,
            "ha_power_positive_is": "discharge",
            "ha_charge_energy_entities": [CHARGED],
            "ha_discharge_energy_entity": DISCHARGED,
            "ha_target_soc_entity": TARGET,
            "ha_stale_after_s": 600,
        }
    )


# --------------------------------------------------------------------------- form

def test_the_form_asks_only_for_entities_and_never_for_an_address():
    """No host, port or credential: there is no device to reach."""
    fields = _markers({})
    assert not {"host", "port", "username", "password", "slave_id"} & set(fields)
    assert {"ha_soc_entity", "ha_power_entity"} <= set(fields)


def test_only_state_of_charge_and_power_are_mandatory():
    """Without those two the driver fixes none of the measurement errors."""
    fields = _markers({})
    required = {name for name, m in fields.items() if isinstance(m, vol.Required)}
    optional = {name for name, m in fields.items() if isinstance(m, vol.Optional)}
    assert {"ha_soc_entity", "ha_power_entity"} <= required
    assert set(OPTIONAL_KEYS) == optional


def test_a_first_time_form_suggests_nothing_for_the_optional_fields():
    fields = _markers({})
    for key in OPTIONAL_KEYS:
        marker = fields[key]
        assert marker.default is vol.UNDEFINED
        assert not getattr(marker, "description", None)


def test_saved_optional_entities_come_back_as_suggestions_not_defaults():
    """The distinction is the whole fix.

    A ``default`` on an optional selector renders a value the user cannot clear;
    no pre-fill at all means clicking through erases the setting. The answer is
    ``description={"suggested_value": ...}``: shown, editable, clearable.
    """
    fields = _markers(_saved())
    for key in OPTIONAL_KEYS:
        marker = fields[key]
        assert marker.default is vol.UNDEFINED, f"{key} must not pin a default"
        assert marker.description["suggested_value"], f"{key} must suggest the saved value"


def test_required_entities_come_back_as_defaults_when_editing():
    fields = _markers(_saved())
    assert fields["ha_soc_entity"].default() == SOC
    assert fields["ha_power_entity"].default() == POWER


# --------------------------------------------------------------------------- storing

def test_clicking_through_the_options_flow_does_not_erase_optional_settings():
    """The field-reported regression, pinned.

    The first installation to re-run the options flow lost both energy counters
    and the target-SOC entity this way, and ``has_energy_counters`` then flipped
    to False, restarting the synthetic lifetime counters against ~9000 kWh of
    real history.
    """
    saved = _saved()
    # What Home Assistant submits when the user accepts the suggested values.
    resubmitted = {
        "name": saved["name"],
        "ha_soc_entity": saved["ha_soc_entity"],
        "ha_power_entity": saved["ha_power_entity"],
        "ha_power_positive_is": saved["ha_power_positive_is"],
        "ha_charge_energy_entities": saved["ha_charge_energy_entities"],
        "ha_discharge_energy_entity": saved["ha_discharge_energy_entity"],
        "ha_target_soc_entity": saved["ha_target_soc_entity"],
        "ha_stale_after_s": saved["ha_stale_after_s"],
    }
    again = _store(resubmitted, saved)
    assert again["ha_charge_energy_entities"] == [CHARGED]
    assert again["ha_discharge_energy_entity"] == DISCHARGED
    assert again["ha_target_soc_entity"] == TARGET


def test_an_explicitly_cleared_field_is_still_cleared():
    """Pre-filling must not become a value the user cannot get rid of."""
    saved = _saved()
    cleared = _store(
        {
            "name": saved["name"],
            "ha_soc_entity": SOC,
            "ha_power_entity": POWER,
            "ha_power_positive_is": "charge",
            "ha_stale_after_s": 600,
        },
        saved,
    )
    assert cleared["ha_charge_energy_entities"] == []
    assert cleared["ha_discharge_energy_entity"] is None
    assert cleared["ha_target_soc_entity"] is None


def test_the_identity_survives_the_source_entities_changing():
    """``device_key`` hangs off the generated host, so it must not move.

    Repointing the battery at renamed entities is an edit, not a new battery;
    if the id changed, every entity and stored value would be orphaned.
    """
    saved = _saved()
    moved = _store(
        {
            "name": "Observed",
            "ha_soc_entity": "sensor.renamed_soc",
            "ha_power_entity": "sensor.renamed_power",
            "ha_power_positive_is": "charge",
            "ha_stale_after_s": 600,
        },
        saved,
    )
    assert moved["host"] == saved["host"]
    assert moved["ha_soc_entity"] == "sensor.renamed_soc"


def test_two_batteries_do_not_share_an_identity():
    first, second = _saved(), _saved()
    assert first["host"] != second["host"]


def test_a_single_energy_entity_is_accepted_as_a_list():
    """The selector is multiple=True, but a stored scalar must not break setup."""
    stored = _store(
        {
            "name": "Observed",
            "ha_soc_entity": SOC,
            "ha_power_entity": POWER,
            "ha_charge_energy_entities": CHARGED,
            "ha_stale_after_s": 600,
        }
    )
    assert stored["ha_charge_energy_entities"] == [CHARGED]


def test_the_stored_config_names_the_brand_and_a_zero_port():
    """Port 0 keeps it off the IP-addressed path: no MAC tracking, no drift."""
    stored = _saved()
    assert stored["brand"] == "generic_ha"
    assert stored["port"] == 0
    assert stored["host"]
