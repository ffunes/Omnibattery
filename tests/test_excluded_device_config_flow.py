"""Excluded-device fields shared by the initial and options flows."""

from types import SimpleNamespace

from custom_components.omnibattery.config_flow import (
    MarstekVenusConfigFlow,
    OptionsFlowHandler,
)
from custom_components.omnibattery.tracking.consumption_profile import (
    ConsumptionProfileTracker,
)


def _schema_defaults(result) -> dict[str, object]:
    """Return the defaults keyed by field name for a flow form."""
    return {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if callable(marker.default)
    }


def _schema_fields(result) -> set[str]:
    """Return every field name exposed by a flow form."""
    return {marker.schema for marker in result["data_schema"].schema}


def _options_flow(entry: SimpleNamespace) -> OptionsFlowHandler:
    """Initialize an options flow as Home Assistant's flow manager does."""
    flow = OptionsFlowHandler(entry)
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda entry_id: (
                entry if entry_id == entry.entry_id else None
            )
        )
    )
    flow.handler = entry.entry_id
    return flow


async def test_initial_flow_exposes_and_saves_excluded_device_controls():
    flow = MarstekVenusConfigFlow()

    form = await flow.async_step_add_excluded_device()
    defaults = _schema_defaults(form)

    assert defaults["dynamic_power_control"] is False
    assert defaults["cover_home_when_active"] is False
    assert "activity_sensor" in _schema_fields(form)

    await flow.async_step_add_excluded_device(
        {
            "power_sensor": "sensor.wallbox_power",
            "activity_sensor": "binary_sensor.ev_charging",
            "dynamic_power_control": True,
            "cover_home_when_active": True,
        }
    )

    assert flow.excluded_devices[0]["dynamic_power_control"] is True
    assert flow.excluded_devices[0]["cover_home_when_active"] is True
    assert flow.excluded_devices[0]["activity_sensor"] == "binary_sensor.ev_charging"


async def test_options_flow_restores_and_saves_excluded_device_controls():
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={
            "excluded_devices": [
                {
                    "power_sensor": "sensor.wallbox_power",
                    "activity_sensor": "binary_sensor.ev_charging",
                    "dynamic_power_control": True,
                    "cover_home_when_active": True,
                }
            ]
        },
    )
    flow = _options_flow(entry)

    form = await flow.async_step_add_excluded_device()
    defaults = _schema_defaults(form)

    assert defaults["dynamic_power_control"] is True
    assert defaults["cover_home_when_active"] is True
    assert defaults["activity_sensor"] == "binary_sensor.ev_charging"

    await flow.async_step_add_excluded_device(
        {
            "power_sensor": "sensor.wallbox_power",
            "activity_sensor": "binary_sensor.ev_charging",
            "dynamic_power_control": True,
            "cover_home_when_active": True,
        }
    )

    assert flow.excluded_devices[0]["dynamic_power_control"] is True
    assert flow.excluded_devices[0]["cover_home_when_active"] is True
    assert flow.excluded_devices[0]["activity_sensor"] == "binary_sensor.ev_charging"


async def test_options_flow_prefills_legacy_no_telemetry_sensor():
    entry = SimpleNamespace(
        entry_id="legacy-entry",
        data={
            "excluded_devices": [
                {
                    "power_sensor": "sensor.ev_state",
                    "ev_charger_no_telemetry": True,
                }
            ]
        },
    )
    flow = _options_flow(entry)

    form = await flow.async_step_add_excluded_device()

    assert _schema_defaults(form)["activity_sensor"] == "sensor.ev_state"


async def test_no_telemetry_accepts_only_dedicated_activity_sensor():
    flow = MarstekVenusConfigFlow()

    form = await flow.async_step_add_excluded_device()
    validated = form["data_schema"](
        {
            "activity_sensor": "binary_sensor.ev_charging",
            "ev_charger_no_telemetry": True,
        }
    )

    await flow.async_step_add_excluded_device(validated)

    assert flow.excluded_devices[0]["power_sensor"] is None
    assert flow.excluded_devices[0]["activity_sensor"] == "binary_sensor.ev_charging"


async def test_no_telemetry_legacy_power_sensor_is_promoted_to_activity_sensor():
    flow = MarstekVenusConfigFlow()

    await flow.async_step_add_excluded_device(
        {
            "power_sensor": "sensor.ev_state",
            "ev_charger_no_telemetry": True,
        }
    )

    assert flow.excluded_devices[0]["power_sensor"] == "sensor.ev_state"
    assert flow.excluded_devices[0]["activity_sensor"] == "sensor.ev_state"


async def test_telemetry_device_requires_power_sensor():
    flow = MarstekVenusConfigFlow()

    result = await flow.async_step_add_excluded_device(
        {"activity_sensor": "binary_sensor.ev_charging"}
    )

    assert result["errors"] == {"power_sensor": "missing_power_sensor"}
    assert flow.excluded_devices == []


async def test_dynamic_power_control_requires_activity_sensor():
    flow = MarstekVenusConfigFlow()

    result = await flow.async_step_add_excluded_device(
        {
            "power_sensor": "sensor.wallbox_power",
            "dynamic_power_control": True,
        }
    )

    assert result["errors"] == {"activity_sensor": "missing_activity_sensor"}
    assert flow.excluded_devices == []


async def test_no_telemetry_device_requires_an_activity_or_legacy_sensor():
    flow = MarstekVenusConfigFlow()

    result = await flow.async_step_add_excluded_device(
        {"ev_charger_no_telemetry": True}
    )

    assert result["errors"] == {"activity_sensor": "missing_activity_sensor"}
    assert flow.excluded_devices == []


async def test_options_flow_keeps_fields_that_have_no_form_field():
    """Re-saving must not reset the Enabled switch or the Exclusion % slider."""
    entry = SimpleNamespace(
        entry_id="runtime-entry",
        data={
            "excluded_devices": [
                {
                    "power_sensor": "sensor.wallbox_power",
                    "enabled": False,
                    "exclusion_pct": 60,
                }
            ]
        },
    )
    flow = _options_flow(entry)

    await flow.async_step_add_excluded_device(
        {"power_sensor": "sensor.wallbox_power"}
    )

    assert flow.excluded_devices[0]["enabled"] is False
    assert flow.excluded_devices[0]["exclusion_pct"] == 60


async def test_options_flow_does_not_hand_runtime_fields_to_a_replacement():
    """Swapping the device at a position must not inherit its disabled state."""
    entry = SimpleNamespace(
        entry_id="replacement-entry",
        data={
            "excluded_devices": [
                {
                    "power_sensor": "sensor.wallbox_power",
                    "enabled": False,
                    "exclusion_pct": 60,
                }
            ]
        },
    )
    flow = _options_flow(entry)

    await flow.async_step_add_excluded_device(
        {"power_sensor": "sensor.heat_pump_power"}
    )

    assert flow.excluded_devices[0]["power_sensor"] == "sensor.heat_pump_power"
    assert flow.excluded_devices[0].get("enabled", True) is True
    assert "exclusion_pct" not in flow.excluded_devices[0]


async def test_options_flow_keeps_runtime_fields_when_only_activity_sensor_is_added():
    """The device is unchanged, so its Enabled state must survive the edit."""
    entry = SimpleNamespace(
        entry_id="activity-entry",
        data={
            "excluded_devices": [
                {
                    "power_sensor": "sensor.wallbox_power",
                    "enabled": False,
                    "exclusion_pct": 60,
                }
            ]
        },
    )
    flow = _options_flow(entry)

    await flow.async_step_add_excluded_device(
        {
            "power_sensor": "sensor.wallbox_power",
            "activity_sensor": "binary_sensor.ev_charging",
            "dynamic_power_control": True,
        }
    )

    assert flow.excluded_devices[0]["activity_sensor"] == "binary_sensor.ev_charging"
    assert flow.excluded_devices[0]["enabled"] is False
    assert flow.excluded_devices[0]["exclusion_pct"] == 60


async def test_options_flow_keeps_runtime_fields_when_power_sensor_is_added():
    """Adding telemetry does not replace an activity-identified device."""
    entry = SimpleNamespace(
        entry_id="power-entry",
        data={
            "excluded_devices": [
                {
                    "power_sensor": None,
                    "activity_sensor": "binary_sensor.ev_charging",
                    "ev_charger_no_telemetry": True,
                    "enabled": False,
                    "exclusion_pct": 60,
                }
            ]
        },
    )
    flow = _options_flow(entry)

    await flow.async_step_add_excluded_device(
        {
            "power_sensor": "sensor.wallbox_power",
            "activity_sensor": "binary_sensor.ev_charging",
        }
    )

    assert flow.excluded_devices[0]["power_sensor"] == "sensor.wallbox_power"
    assert flow.excluded_devices[0]["activity_sensor"] == "binary_sensor.ev_charging"
    assert flow.excluded_devices[0]["enabled"] is False
    assert flow.excluded_devices[0]["exclusion_pct"] == 60


def _profile_fingerprint(entry: SimpleNamespace) -> str:
    """Fingerprint the 28-day consumption profile reports source changes with."""
    profile = ConsumptionProfileTracker.__new__(ConsumptionProfileTracker)
    profile._config_entry = entry
    profile._hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Madrid"))
    return profile.configuration_fingerprint()


async def test_resaving_a_device_unchanged_is_not_a_source_change():
    """A no-op pass through the options flow must not read as a new source."""
    entry = SimpleNamespace(
        entry_id="profile-entry",
        data={
            "consumption_sensor": "sensor.grid",
            "excluded_devices": [
                {
                    "power_sensor": "sensor.wallbox_power",
                    "included_in_consumption": True,
                    "enabled": False,
                    "exclusion_pct": 60,
                }
            ],
        },
    )
    before = _profile_fingerprint(entry)

    flow = _options_flow(entry)
    form = await flow.async_step_add_excluded_device()
    await flow.async_step_add_excluded_device(_schema_defaults(form))

    resaved = SimpleNamespace(
        entry_id=entry.entry_id,
        data={**entry.data, "excluded_devices": flow.excluded_devices},
    )
    assert _profile_fingerprint(resaved) == before

    changed = SimpleNamespace(
        entry_id=entry.entry_id,
        data={**entry.data, "excluded_devices": [
            {**flow.excluded_devices[0], "exclusion_pct": 40}
        ]},
    )
    assert _profile_fingerprint(changed) != before


async def test_initial_flow_saves_remaining_demand_sensor():
    flow = MarstekVenusConfigFlow()

    form = await flow.async_step_add_excluded_device()
    assert "remaining_demand_sensor" in _schema_fields(form)

    await flow.async_step_add_excluded_device(
        {
            "power_sensor": "sensor.wallbox_power",
            "remaining_demand_sensor": "sensor.evcc_charge_remaining_energy",
        }
    )

    assert (
        flow.excluded_devices[0]["remaining_demand_sensor"]
        == "sensor.evcc_charge_remaining_energy"
    )


async def test_initial_flow_stores_no_remaining_demand_sensor_by_default():
    flow = MarstekVenusConfigFlow()

    await flow.async_step_add_excluded_device({"power_sensor": "sensor.wallbox_power"})

    assert flow.excluded_devices[0]["remaining_demand_sensor"] is None


async def test_options_flow_prefills_remaining_demand_sensor():
    entry = SimpleNamespace(
        entry_id="claim-entry",
        data={
            "excluded_devices": [
                {
                    "power_sensor": "sensor.wallbox_power",
                    "remaining_demand_sensor": "sensor.evcc_charge_remaining_energy",
                }
            ]
        },
    )
    flow = _options_flow(entry)

    form = await flow.async_step_add_excluded_device()

    assert (
        _schema_defaults(form)["remaining_demand_sensor"]
        == "sensor.evcc_charge_remaining_energy"
    )


async def test_options_flow_can_clear_remaining_demand_sensor():
    """Clearing the field must survive the merge with the stored device."""
    entry = SimpleNamespace(
        entry_id="claim-entry",
        data={
            "excluded_devices": [
                {
                    "power_sensor": "sensor.wallbox_power",
                    "remaining_demand_sensor": "sensor.evcc_charge_remaining_energy",
                    "enabled": False,
                }
            ]
        },
    )
    flow = _options_flow(entry)

    await flow.async_step_add_excluded_device({"power_sensor": "sensor.wallbox_power"})

    assert flow.excluded_devices[0]["remaining_demand_sensor"] is None
    # The switch-only key still survives the same merge.
    assert flow.excluded_devices[0]["enabled"] is False


async def test_options_flow_legacy_device_without_the_field_round_trips():
    entry = SimpleNamespace(
        entry_id="legacy-claim-entry",
        data={"excluded_devices": [{"power_sensor": "sensor.wallbox_power"}]},
    )
    flow = _options_flow(entry)

    form = await flow.async_step_add_excluded_device()
    assert "remaining_demand_sensor" not in _schema_defaults(form)

    await flow.async_step_add_excluded_device({"power_sensor": "sensor.wallbox_power"})

    assert flow.excluded_devices[0]["remaining_demand_sensor"] is None


async def test_initial_flow_saves_presence_sensor():
    flow = MarstekVenusConfigFlow()

    form = await flow.async_step_add_excluded_device()
    assert "remaining_demand_presence_sensor" in _schema_fields(form)

    await flow.async_step_add_excluded_device(
        {
            "power_sensor": "sensor.wallbox_power",
            "remaining_demand_sensor": "sensor.evcc_charge_remaining_energy",
            "remaining_demand_presence_sensor": "binary_sensor.evcc_connected",
        }
    )

    assert (
        flow.excluded_devices[0]["remaining_demand_presence_sensor"]
        == "binary_sensor.evcc_connected"
    )


async def test_initial_flow_stores_no_presence_sensor_by_default():
    flow = MarstekVenusConfigFlow()

    await flow.async_step_add_excluded_device({"power_sensor": "sensor.wallbox_power"})

    assert flow.excluded_devices[0]["remaining_demand_presence_sensor"] is None
