"""Unit tests for the observation-only Home Assistant entity driver.

HA is faked with a minimal states/services stub, so no runtime is required.
The point of this driver is what it refuses to do, so most of these assert an
absence: no command, no fabricated zero, no allocation.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.drivers.generic_ha import (
    DEFAULT_STALE_AFTER_S,
    GenericHaEntityDriver,
)

SOC = "sensor.scb_battery_soc"
POWER = "sensor.scb_battery_power"
CHARGE_PV = "sensor.scb_battery_charge_from_pv_total"
CHARGE_GRID = "sensor.scb_battery_charge_from_grid_total"
DISCHARGE = "sensor.scb_battery_discharge_total"
TARGET = "number.scb_battery_min_soc"


class _State:
    def __init__(self, state, *, last_reported=None, attributes=None):
        self.state = state
        self.last_reported = last_reported if last_reported is not None else dt_util.utcnow()
        self.attributes = attributes or {}


class _States:
    def __init__(self, table):
        self._table = table

    def get(self, entity_id):
        return self._table.get(entity_id)


def _hass(table):
    hass = MagicMock()
    hass.states = _States(table)
    hass.services.async_call = AsyncMock()
    return hass


def _table(**overrides):
    table = {
        SOC: _State("34"),
        POWER: _State("-403"),
        CHARGE_PV: _State("8884.6"),
        CHARGE_GRID: _State("109.0"),
        DISCHARGE: _State("8213.5"),
        TARGET: _State("100", attributes={"min": 5, "max": 100, "step": 5}),
    }
    table.update(overrides)
    return table


def _driver(hass, **kwargs):
    kwargs.setdefault("soc_entity", SOC)
    kwargs.setdefault("power_entity", POWER)
    kwargs.setdefault("power_positive_is", "discharge")
    kwargs.setdefault("charge_energy_entities", (CHARGE_PV, CHARGE_GRID))
    kwargs.setdefault("discharge_energy_entity", DISCHARGE)
    kwargs.setdefault("target_soc_entity", TARGET)
    return GenericHaEntityDriver(hass, **kwargs)


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------

def test_capabilities_declare_an_uncommandable_battery():
    caps = _driver(_hass(_table())).capabilities
    assert caps.supports_power_setpoint is False
    # A zero envelope is the second line of defence: even a caller that forgets
    # the capability gate has no power to hand out.
    assert caps.max_charge_power_w == 0
    assert caps.max_discharge_power_w == 0
    # DC-coupled PV is already netted into the solar sensor; claiming MPPT here
    # makes residual_load drop the battery's term and silently disables the
    # second-regulator guards.
    assert caps.has_mppt_pv is False
    assert caps.has_solar_telemetry is False
    assert caps.hardware_soc_cutoff is False
    assert caps.has_force_mode is False
    assert caps.has_rs485_control is False
    assert caps.has_nominal_capacity is False
    assert caps.has_daily_energy_counters is False
    assert caps.push_telemetry is True
    assert caps.telemetry_liveness_checked is True


def test_energy_counters_are_only_claimed_when_configured():
    hass = _hass(_table())
    with_counters = _driver(hass)
    without = _driver(hass, charge_energy_entities=(), discharge_energy_entity=None)
    assert with_counters.capabilities.has_energy_counters is True
    assert without.capabilities.has_energy_counters is False


def test_number_definition_exists_only_with_a_target_entity():
    hass = _hass(_table())
    assert [d["key"] for d in _driver(hass).number_definitions] == ["target_soc"]
    assert _driver(hass, target_soc_entity=None).number_definitions == []
    # Nothing else is ever offered: no force mode, no switches, no buttons.
    driver = _driver(hass)
    assert driver.select_definitions == []
    assert driver.switch_definitions == []
    assert driver.binary_sensor_definitions == []
    assert driver.button_definitions == []


def test_energy_sensors_disappear_without_their_sources():
    hass = _hass(_table())
    keys = {d["key"] for d in _driver(hass, charge_energy_entities=(), discharge_energy_entity=None).sensor_definitions}
    assert keys == {"battery_soc", "battery_power"}


# ---------------------------------------------------------------------------
# telemetry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reads_soc_and_flips_the_power_sign():
    driver = _driver(_hass(_table()))
    snapshot = await driver.read_telemetry()
    assert snapshot["battery_soc"] == 34
    # Kostal reports charging as negative; Omnibattery's convention is positive.
    assert snapshot["battery_power"] == 403


@pytest.mark.asyncio
async def test_power_sign_is_passed_through_when_the_source_agrees():
    driver = _driver(_hass(_table()), power_positive_is="charge")
    assert (await driver.read_telemetry())["battery_power"] == -403


@pytest.mark.asyncio
async def test_charge_counters_are_summed():
    driver = _driver(_hass(_table()))
    snapshot = await driver.read_telemetry()
    assert snapshot["total_charging_energy"] == pytest.approx(8884.6 + 109.0)
    assert snapshot["total_discharging_energy"] == pytest.approx(8213.5)


@pytest.mark.asyncio
async def test_a_blinking_counter_drops_the_whole_sum():
    """A partial sum would step backwards and be discarded downstream anyway."""
    driver = _driver(_hass(_table(**{CHARGE_GRID: _State("unavailable")})))
    assert "total_charging_energy" not in await driver.read_telemetry()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["unavailable", "unknown", "", "n/a", "NaN"])
async def test_unreadable_values_are_omitted_never_zeroed(raw):
    """Zero is a valid battery power; inventing one hides a dead sensor."""
    driver = _driver(_hass(_table(**{POWER: _State(raw)})))
    snapshot = await driver.read_telemetry()
    assert "battery_power" not in snapshot
    assert snapshot["battery_soc"] == 34


@pytest.mark.asyncio
async def test_key_filter_is_honoured():
    driver = _driver(_hass(_table()))
    assert set(await driver.read_telemetry(["battery_soc"])) == {"battery_soc"}


@pytest.mark.asyncio
async def test_a_stalled_source_yields_an_empty_snapshot():
    """The coordinator's failure ladder needs something to fail on."""
    stale = dt_util.utcnow() - timedelta(seconds=DEFAULT_STALE_AFTER_S + 60)
    driver = _driver(_hass(_table(
        **{SOC: _State("34", last_reported=stale), POWER: _State("-403", last_reported=stale)}
    )))
    assert await driver.read_telemetry() == {}


@pytest.mark.asyncio
async def test_one_live_entity_keeps_the_battery_alive():
    """It is a heartbeat, not a per-key freshness budget."""
    stale = dt_util.utcnow() - timedelta(seconds=DEFAULT_STALE_AFTER_S + 60)
    driver = _driver(_hass(_table(**{POWER: _State("-403", last_reported=stale)})))
    assert (await driver.read_telemetry())["battery_soc"] == 34


@pytest.mark.asyncio
async def test_the_guard_fails_open_without_timestamps():
    table = _table()
    for state in table.values():
        state.last_reported = None
    assert (await _driver(_hass(table)).read_telemetry())["battery_soc"] == 34


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_requires_soc_and_power():
    driver = _driver(_hass(_table()))
    assert await driver.connect() is True
    assert driver.connected is True


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [SOC, POWER])
async def test_connect_refuses_an_unreadable_required_entity(broken):
    driver = _driver(_hass(_table(**{broken: _State("unavailable")})))
    assert await driver.connect() is False
    assert driver.connected is False


def test_resolve_names_the_missing_keys_for_the_config_flow():
    hass = _hass(_table(**{POWER: _State("unavailable")}))
    missing = GenericHaEntityDriver.resolve(
        hass, {"battery_soc": SOC, "battery_power": POWER}
    )
    assert missing == ["battery_power"]
    assert GenericHaEntityDriver.resolve(hass, {}) == ["battery_power", "battery_soc"]


@pytest.mark.asyncio
async def test_connect_adopts_the_upstream_numbers_own_bounds():
    hass = _hass(_table(**{TARGET: _State("50", attributes={"min": 10, "max": 90, "step": 2})}))
    driver = _driver(hass)
    await driver.connect()
    definition = driver.number_definitions[0]
    assert (definition["min"], definition["max"], definition["step"]) == (10, 90, 2)


# ---------------------------------------------------------------------------
# control: what it refuses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("request_w", [1500, -1500, 0])
async def test_a_setpoint_is_answered_coherently_and_discarded(request_w):
    hass = _hass(_table())
    result = await _driver(hass).apply_setpoint(request_w)
    assert result.ok is True
    # It reports the idle it actually holds, not the command it was handed, so a
    # delivery check compares against the truth.
    assert result.net_power_w == 0
    hass.services.async_call.assert_not_awaited()


def test_there_is_no_command_echo_to_reconstruct():
    driver = _driver(_hass(_table()))
    assert driver.net_power_from_data({"battery_power": 403}) is None


def test_nothing_may_be_allocated_to_it():
    assert _driver(_hass(_table())).dynamic_discharge_limit_w({}) == 0


@pytest.mark.asyncio
async def test_setup_configuration_is_accepted_without_writing():
    """A False here raises ConfigEntryNotReady, and there is no failure to report."""
    hass = _hass(_table())
    ok = await _driver(hass).apply_config(
        max_soc_pct=100, min_soc_pct=0, max_charge_power_w=0, max_discharge_power_w=0
    )
    assert ok is True
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_hooks_report_failure_rather_than_pretending():
    driver = _driver(_hass(_table()))
    assert await driver.set_charge_cutoff(90) is False
    assert await driver.set_rs485_control(True) is False
    assert await driver.standby() is True


@pytest.mark.asyncio
async def test_control_dependency_keys_keep_the_balance_terms_polled():
    driver = _driver(_hass(_table()))
    assert driver.control_dependency_keys == frozenset({"battery_soc", "battery_power"})


# ---------------------------------------------------------------------------
# control: the one thing it writes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_target_soc_is_written_to_the_configured_number():
    hass = _hass(_table())
    driver = _driver(hass)
    await driver.connect()
    assert await driver.write_control("target_soc", 60) is True
    hass.services.async_call.assert_awaited_once_with(
        "number", "set_value", {"entity_id": TARGET, "value": 60}, blocking=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,expected", [(62, 60), (63, 65), (0, 5), (999, 100)])
async def test_target_soc_is_snapped_onto_the_grid_the_entity_accepts(requested, expected):
    """An entity showing a value its write path rejects is worse than no entity."""
    hass = _hass(_table())
    driver = _driver(hass)
    await driver.connect()
    await driver.write_control("target_soc", requested)
    assert hass.services.async_call.await_args.args[2]["value"] == expected


@pytest.mark.asyncio
async def test_no_other_control_key_is_accepted():
    hass = _hass(_table())
    driver = _driver(hass)
    for key in ("set_charge_power", "force_mode", "max_charge_power", "battery_soc"):
        assert await driver.write_control(key, 1000) is False
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_a_target_entity_even_that_write_is_refused():
    hass = _hass(_table())
    driver = _driver(hass, target_soc_entity=None)
    assert await driver.write_control("target_soc", 60) is False
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_service_call_is_reported_not_raised():
    hass = _hass(_table())
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("entity gone"))
    driver = _driver(hass)
    await driver.connect()
    assert await driver.write_control("target_soc", 60) is False


# ---------------------------------------------------------------------------
# device identity
# ---------------------------------------------------------------------------

def test_the_model_names_the_source_integration(monkeypatch):
    """The device card says where the readings come from, not a guessed vendor.

    ``MANUFACTURER`` is fixed at class level and cannot know whose battery this
    is, so the one thing that is both true and useful -- the integration that
    publishes the entities -- goes in the model line.
    """
    import custom_components.omnibattery.drivers.generic_ha as mod

    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(platform="kostal_plenticore")
    monkeypatch.setattr(mod.er, "async_get", lambda _hass: registry)

    assert _driver(_hass(_table())).model_label == "Kostal Plenticore (observed)"


def test_an_unresolvable_source_falls_back_without_raising(monkeypatch):
    """A missing registry leaves the model to ``DEFAULT_MODEL`` instead of raising."""
    import custom_components.omnibattery.drivers.generic_ha as mod

    monkeypatch.setattr(
        mod.er, "async_get", lambda _hass: (_ for _ in ()).throw(RuntimeError("no registry"))
    )
    assert _driver(_hass(_table())).model_label is None


def test_the_class_identity_is_complete_for_the_registry():
    """Every registered driver must name itself; the registry test pins this too."""
    from custom_components.omnibattery.drivers.generic_ha import GenericHaEntityDriver

    assert GenericHaEntityDriver.BRAND == "generic_ha"
    assert GenericHaEntityDriver.LABEL
    assert GenericHaEntityDriver.MANUFACTURER
    assert GenericHaEntityDriver.DEFAULT_MODEL
    assert GenericHaEntityDriver.SETUP is not None
    # The limits form has no power fields to fill and the device owns its own
    # configuration; both are declared, not special-cased on the brand.
    assert GenericHaEntityDriver.TRAITS.device_owns_initial_config is True
    assert GenericHaEntityDriver.TRAITS.capacity_field == "required"
    assert GenericHaEntityDriver.TRAITS.supports_voltage_taper is False


def test_from_config_builds_the_driver_from_stored_entity_ids():
    """The coordinator hands the driver its stored config; no brand kwarg exists."""
    from custom_components.omnibattery.drivers.base import DriverContext
    from custom_components.omnibattery.drivers.generic_ha import GenericHaEntityDriver

    hass = _hass(_table())
    driver = GenericHaEntityDriver.from_config(
        {
            "ha_soc_entity": SOC,
            "ha_power_entity": POWER,
            "ha_power_positive_is": "discharge",
            "ha_charge_energy_entities": [CHARGE_PV],
            "ha_stale_after_s": 1200,
        },
        DriverContext(
            hass=hass, name="Observed", device_key="k",
            max_charge_power_w=0, max_discharge_power_w=0,
            persist=lambda *a, **k: None, stored=lambda _key: None,
        ),
    )
    assert driver._soc_entity == SOC
    assert driver._power_sign == -1.0
    assert driver._charge_energy_entities == (CHARGE_PV,)
    assert driver._stale_after_s == 1200.0
    assert driver.capabilities.supports_power_setpoint is False
