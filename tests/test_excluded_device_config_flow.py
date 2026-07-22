"""Excluded-device fields shared by the initial and options flows."""

from types import SimpleNamespace

from custom_components.omnibattery.config_flow import (
    MarstekVenusConfigFlow,
    OptionsFlowHandler,
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
