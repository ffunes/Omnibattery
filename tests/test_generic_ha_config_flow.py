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

import asyncio
from types import SimpleNamespace

import pytest
import voluptuous as vol

from custom_components.omnibattery.drivers.generic_ha import GenericHaEntityDriver
from custom_components.omnibattery.drivers.generic_ha_flow import (
    _connection,
    _mode,
    _reconfigure,
    _schema,
    _store,
)
from custom_components.omnibattery.drivers.setup import FlowKind, SetupContext

SOC = "sensor.scb_battery_soc"
POWER = "sensor.scb_battery_power"
CHARGED = "sensor.scb_battery_charge_total"
DISCHARGED = "sensor.scb_battery_discharge_total"
TARGET = "number.scb_battery_min_soc"
MODE = "select.scb_battery_mode"

OPTIONAL_KEYS = (
    "ha_charge_energy_entities",
    "ha_discharge_energy_entity",
    "ha_target_soc_entity",
    "ha_mode_entity",
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
            "ha_mode_entity": MODE,
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
        "ha_mode_entity": saved["ha_mode_entity"],
        "ha_stale_after_s": saved["ha_stale_after_s"],
    }
    again = _store(resubmitted, saved)
    assert again["ha_mode_entity"] == MODE
    # Editing is not a new battery: the running preference is not touched.
    assert "autonomous_requested_mode" not in again
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
    # Laid over the stored battery: None, not missing, is what removes it.
    assert cleared["ha_mode_entity"] is None


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


# --------------------------------------------------------------------------- mode

def test_a_battery_that_gains_a_mode_starts_in_manual():
    assert _saved()["autonomous_requested_mode"] == "manual"
    without = _store({"name": "Observed", "ha_soc_entity": SOC, "ha_power_entity": POWER})
    assert "ha_mode_entity" not in without
    assert "autonomous_requested_mode" not in without


_FORM = {
    "name": "Observed", "ha_soc_entity": SOC, "ha_power_entity": POWER,
    "ha_power_positive_is": "charge", "ha_stale_after_s": 600,
}


@pytest.fixture
def mode_flow(monkeypatch):
    """``make(kind, battery, **states) -> (flow, ctx)`` around the real handlers."""
    monkeypatch.setattr(GenericHaEntityDriver, "resolve", lambda *a: [])

    def make(kind=FlowKind.CONFIG, battery=None, states=None):
        table = {
            MODE: SimpleNamespace(state="auto", attributes={"options": ["auto", "hold", "backup"]}),
            "switch.nom": SimpleNamespace(state="on", attributes={}),
            **(states or {}),
        }
        ctx = SetupContext(kind=kind, battery=battery, entry=None, coordinator=None)
        flow = SimpleNamespace(
            hass=SimpleNamespace(states=SimpleNamespace(get=table.get)),
            battery_index=0,
            async_show_form=lambda **kwargs: kwargs,
        )

        async def _step(user_input=None):
            return await _mode(flow, user_input, ctx)

        flow.async_step_battery_mode_generic_ha = _step
        return flow, ctx

    return make


@pytest.mark.parametrize("handler", [_connection, _reconfigure])
def test_picking_a_mode_entity_asks_for_its_two_states(mode_flow, handler):
    """The options are only known once the entity is picked: a second step."""
    flow, ctx = mode_flow()
    form = asyncio.run(handler(flow, {**_FORM, "ha_mode_entity": MODE}, ctx))
    assert form["step_id"] == "battery_mode_generic_ha"
    selector = next(iter(form["data_schema"].schema.values()))
    assert selector.config["options"] == ["auto", "hold", "backup"]

    stored = asyncio.run(
        _mode(flow, {"ha_mode_nom_option": "auto", "ha_mode_manual_option": "hold"}, ctx)
    )
    assert stored["ha_mode_entity"] == MODE
    assert (stored["ha_mode_nom_option"], stored["ha_mode_manual_option"]) == ("auto", "hold")
    assert stored["autonomous_requested_mode"] == "manual"


def test_a_switch_offers_on_and_off(mode_flow):
    flow, ctx = mode_flow()
    form = asyncio.run(_connection(flow, {**_FORM, "ha_mode_entity": "switch.nom"}, ctx))
    selector = next(iter(form["data_schema"].schema.values()))
    assert selector.config["options"] == ["on", "off"]


def test_without_a_mode_entity_there_is_no_second_step(mode_flow):
    flow, ctx = mode_flow()
    stored = asyncio.run(_connection(flow, dict(_FORM), ctx))
    assert stored["brand"] == "generic_ha"


def test_the_two_modes_cannot_be_the_same_state(mode_flow):
    flow, ctx = mode_flow()
    ctx.work["pending"] = {"ha_mode_entity": MODE}
    form = asyncio.run(
        _mode(flow, {"ha_mode_nom_option": "auto", "ha_mode_manual_option": "auto"}, ctx)
    )
    assert form["errors"] == {"base": "generic_ha_mode_same"}


def test_an_unreadable_mode_entity_stays_on_the_form(mode_flow):
    flow, ctx = mode_flow()
    form = asyncio.run(_connection(flow, {**_FORM, "ha_mode_entity": "select.gone"}, ctx))
    assert form["errors"] == {"ha_mode_entity": "generic_ha_mode_unreadable"}


def test_editing_while_the_mode_entity_is_offline_keeps_the_saved_pair(mode_flow):
    saved = {**_saved(), "ha_mode_nom_option": "auto", "ha_mode_manual_option": "hold"}
    flow, ctx = mode_flow(FlowKind.RECONFIGURE, saved, {MODE: None})
    form = asyncio.run(_reconfigure(flow, {**_FORM, "ha_mode_entity": MODE}, ctx))
    assert [marker.default() for marker in form["data_schema"].schema] == ["auto", "hold"]


# --------------------------------------------------------------------------- limits

def _limits(kind, battery, base=("max_charge_power", "max_discharge_power", "max_soc",
                                 "min_soc", "charge_hysteresis_percent")):
    ctx = SetupContext(kind=kind, battery=battery, entry=None, coordinator=None)
    fields = GenericHaEntityDriver.SETUP.limits_schema(
        dict(battery or {}), {vol.Required(key): int for key in base}, ctx
    )
    return [str(marker) for marker in fields]


@pytest.mark.parametrize("kind", [FlowKind.CONFIG, FlowKind.OPTIONS])
def test_the_limits_form_asks_only_for_the_capacity(kind):
    """Every other limit shapes a command this battery never receives."""
    assert _limits(kind, {"battery_capacity_kwh": 6.0}) == ["battery_capacity_kwh"]


def test_the_limits_it_does_not_ask_for_are_still_stored():
    """The coordinator of every battery is built from them."""
    merged: dict = {}
    GenericHaEntityDriver.SETUP.finalize_limits(
        merged, {"battery_capacity_kwh": 6.0}, {}, None
    )
    assert {"max_charge_power", "max_discharge_power", "max_soc", "min_soc"} <= set(merged)
    kept = {"max_charge_power": 1500, "max_discharge_power": 1500, "max_soc": 95, "min_soc": 15}
    merged = dict(kept)
    GenericHaEntityDriver.SETUP.finalize_limits(
        merged, {"battery_capacity_kwh": 6.0}, {}, None
    )
    assert {key: merged[key] for key in kept} == kept


def test_its_stored_limits_are_not_watts_of_the_fleet():
    from custom_components.omnibattery.const.integration_const import (
        default_high_price_discharge_max_power,
        total_battery_power,
    )

    watts = {"max_charge_power": 2500, "max_discharge_power": 2500}
    data = {"batteries": [{"brand": "marstek", **watts}, {"brand": "generic_ha", **watts}]}
    assert total_battery_power(data) == (2500, 2500)
    assert default_high_price_discharge_max_power(data) == 2500


def test_the_target_soc_has_a_name_in_every_language():
    import json
    from pathlib import Path

    base = Path("custom_components/omnibattery")
    for path in [base / "strings.json", *sorted((base / "translations").glob("*.json"))]:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["entity"]["number"]["target_soc"]["name"], path


def test_the_panel_offers_the_target_soc_in_six_languages():
    """It is the one control of an observed battery without a mode."""
    from pathlib import Path

    panel = Path("custom_components/omnibattery/frontend/marstek-panel.js").read_text(
        encoding="utf-8"
    )
    start = panel.index("const BAT_CONTROLS = [")
    assert '{ key: "target_soc", domain: "number", lk: "bcTargetSoc"' in (
        panel[start:panel.index("];", start)]
    )
    assert panel.count("bcTargetSoc: ") == 6


def test_no_text_says_the_sources_must_be_in_watts_or_kwh():
    """They are converted from whatever unit they carry."""
    import json
    from pathlib import Path

    base = Path("custom_components/omnibattery")
    for path in [base / "strings.json", *sorted((base / "translations").glob("*.json"))]:
        data = json.loads(path.read_text(encoding="utf-8"))
        for flow, step in (
            ("config", "battery_connection_generic_ha"),
            ("config", "reconfigure_battery_generic_ha"),
            ("options", "battery_connection_generic_ha"),
        ):
            texts = data[flow]["step"][step]["data_description"]
            assert "kW" in texts["ha_power_entity"], path
            for key in ("ha_charge_energy_entities", "ha_discharge_energy_entity"):
                assert "MWh" in texts[key], path
