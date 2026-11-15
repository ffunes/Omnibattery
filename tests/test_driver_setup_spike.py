"""Spike for per-brand config flow steps (driver-registry T6.1).

``DriverSetup`` + steps generated onto the two flow classes, proved with ONE
fake brand registered only inside these tests. No real brand uses it yet.

The fake brand has: a connection step with an error form, a conditional step
(only when the fake probe asks for it), and a reconfigure step. It runs in the
config flow (add), the options flow and config-flow reconfigure.

Isolation: the classes are built at import, so the fixture registers the brand
with ``monkeypatch.setitem(registry.DRIVERS, ...)`` and calls the SAME install
function the module uses, with a ``setter`` that goes through
``monkeypatch.setattr(..., raising=False)``: teardown removes the methods again.

The ``hass`` tests need the HA plugin (skipped in the plain ``pytest``)::

    .venv-test/Scripts/python -m pytest -q -o addopts="" tests/test_driver_setup_spike.py
"""
from __future__ import annotations

import inspect

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.omnibattery.config_flow import (
    MarstekVenusConfigFlow,
    OptionsFlowHandler,
    _brand_selector_options,
    _install_driver_steps,
)
from custom_components.omnibattery.drivers import registry
from custom_components.omnibattery.drivers.base import BatteryDriver
from custom_components.omnibattery.drivers.setup import (
    DriverSetup,
    FlowKind,
    SetupContext,
)
from tests.flow_harness import (  # noqa: F401  (fixtures are used by name)
    CASES,
    DOMAIN,
    SYSTEM_INPUT,
    BrandCase,
    _stored,
    drive,
    flow_env,
    probes,
)

# The selector of today: values, labels and order.
TODAY_OPTIONS = [
    {"value": "marstek", "label": "Marstek Venus"},
    {"value": "zendure", "label": "Zendure SolarFlow"},
    {"value": "esphome", "label": "Marstek via LilyGo RS485 (ESPHome)"},
    {"value": "anker", "label": "Anker SOLIX Solarbank Max AC / 4 E5000 Pro"},
    {"value": "sessy", "label": "Sessy"},
    {"value": "indevolt", "label": "Indevolt SolidFlex / PowerFlex"},
    {"value": "hoymiles", "label": "Hoymiles MQTT"},
    {"value": "huawei", "label": "Huawei SUN2000 + LUNA2000"},
]

FAKE = "fakebrand"
CONNECT = "battery_connection_fake"
EXTRA = "fake_extra"
RECONFIGURE = "reconfigure_battery_fake"


# --------------------------------------------------------------------------- the fake brand


class FakeProbe:
    """What the fake brand's handlers saw and what its fake device answers."""

    def __init__(self) -> None:
        self.reachable = True
        self.wants_extra = False
        self.contexts: list[SetupContext] = []
        self.flows: list = []
        self.works: list[dict] = []


FAKE_PROBE = FakeProbe()


def _record(flow, ctx: SetupContext) -> None:
    FAKE_PROBE.flows.append(flow)
    FAKE_PROBE.contexts.append(ctx)
    FAKE_PROBE.works.append(dict(ctx.work))


async def _connection(flow, user_input, ctx):
    _record(flow, ctx)
    defaults = ctx.battery or {}
    if user_input is None:
        return flow.async_show_form(
            step_id=CONNECT,
            data_schema=vol.Schema({
                vol.Required("name", default=defaults.get("name", "Fake 1")): str,
                vol.Required("host", default=defaults.get("host", "")): str,
            }),
        )
    if not FAKE_PROBE.reachable:
        return flow.async_show_form(
            step_id=CONNECT,
            data_schema=vol.Schema({
                vol.Required("name", default="Fake 1"): str,
                vol.Required("host", default=""): str,
            }),
            errors={"base": "cannot_connect"},
        )
    ctx.work["attempts"] = ctx.work.get("attempts", 0) + 1
    data = {"name": user_input["name"], "host": user_input["host"], "port": 1, "brand": FAKE}
    if FAKE_PROBE.wants_extra:
        ctx.work["pending"] = data
        return await flow.async_step_fake_extra()
    return data


async def _extra(flow, user_input, ctx):
    _record(flow, ctx)
    if user_input is None:
        return flow.async_show_form(
            step_id=EXTRA, data_schema=vol.Schema({vol.Required("extra", default="e"): str})
        )
    return {**ctx.work["pending"], "fake_extra": user_input["extra"],
            "fake_attempts": ctx.work["attempts"]}


async def _reconfigure(flow, user_input, ctx):
    _record(flow, ctx)
    if user_input is None:
        return flow.async_show_form(
            step_id=RECONFIGURE,
            data_schema=vol.Schema({
                vol.Required("name", default=ctx.battery["name"]): str,
                vol.Required("host", default=ctx.battery["host"]): str,
            }),
        )
    return {"name": user_input["name"], "host": user_input["host"]}


FAKE_SETUP = DriverSetup(
    steps={CONNECT: _connection, EXTRA: _extra, RECONFIGURE: _reconfigure},
    entry_step=CONNECT,
    reconfigure_entry_step=RECONFIGURE,
)


class FakeDriver(BatteryDriver):
    """Registered only by the ``fake_brand`` fixture; never instantiated."""

    BRAND = FAKE
    LABEL = "Fake Brand"
    SETUP = FAKE_SETUP


FAKE_CASE = BrandCase(
    id=FAKE,
    brand=FAKE,
    connection_step=CONNECT,
    reconfigure_step=RECONFIGURE,
    add_input={"name": "Fake 1", "host": "192.0.2.90"},
    options_input={"name": "Fake 1 edited", "host": "192.0.2.91"},
    reconfigure_input={"name": "Fake 1 moved", "host": "192.0.2.92"},
    existing=_stored(brand=FAKE, name="Fake 1", host="192.0.2.90", port=1),
    extra_add_steps={EXTRA: {"extra": "yes"}},
)


@pytest.fixture
def fake_brand(monkeypatch):
    """Register the fake brand and install its steps; both are undone at teardown."""
    FAKE_PROBE.__init__()
    monkeypatch.setitem(registry.DRIVERS, FAKE, FakeDriver)
    installed = {}

    def setter(cls, name, method):
        monkeypatch.setattr(cls, name, method, raising=False)
        installed.setdefault(cls, []).append(name)

    for cls in (MarstekVenusConfigFlow, OptionsFlowHandler):
        _install_driver_steps(cls, FAKE_SETUP, FAKE, setter=setter)
    return installed


def shown_values(result) -> dict:
    shown = {}
    for marker in result["data_schema"].schema:
        description = marker.description if isinstance(marker.description, dict) else {}
        if "suggested_value" in description:
            shown[str(marker.schema)] = description["suggested_value"]
        elif marker.default is not vol.UNDEFINED:
            shown[str(marker.schema)] = marker.default()
    return shown


def selector_of(result) -> tuple[list, object]:
    (marker, validator), = [
        (m, v) for m, v in result["data_schema"].schema.items() if str(m.schema) == "brand"
    ]
    return list(validator.config["options"]), marker.default()


# --------------------------------------------------------------------------- selector (no hass)


def test_selector_options_are_todays_literal_list():
    assert _brand_selector_options() == TODAY_OPTIONS


def test_only_migrated_brands_have_a_setup():
    assert {b for b, cls in registry.DRIVERS.items() if cls.SETUP is not None} == {"sessy", "zendure"}


def test_fake_brand_is_not_visible_outside_its_fixture():
    assert FAKE not in registry.DRIVERS
    for cls in (MarstekVenusConfigFlow, OptionsFlowHandler):
        assert not hasattr(cls, f"async_step_{CONNECT}")
        assert not hasattr(cls, f"async_step_{EXTRA}")
    assert _brand_selector_options() == TODAY_OPTIONS


def test_generated_steps_are_real_wrapped_coroutines(fake_brand):
    assert set(fake_brand) == {MarstekVenusConfigFlow, OptionsFlowHandler}
    for cls, names in fake_brand.items():
        assert names == [f"async_step_{s}" for s in (CONNECT, EXTRA, RECONFIGURE)]
        for name in names:
            method = getattr(cls, name)
            assert inspect.iscoroutinefunction(method)
            inner = method.__wrapped__.__code__  # what the decorator inspects
            assert "user_input" in inner.co_varnames[: inner.co_argcount]
            # The error-form wrapper of _preserve_values_in_error_forms is on it.
            assert method._preserves_submitted_values is True
            assert method.__name__ == name


def test_missing_attribute_keeps_the_default(fake_brand):
    # No __getattr__ anywhere: the same lookups the flows rely on.
    assert getattr(MarstekVenusConfigFlow(), "_no_existe", 42) == 42
    assert getattr(MarstekVenusConfigFlow(), "_huawei_candidates", []) == []
    assert not hasattr(MarstekVenusConfigFlow, "async_step_battery_connection_nada")


# --------------------------------------------------------------------------- selector (hass)


async def test_selector_in_both_flows_is_identical_to_today(flow_env):
    hass = flow_env.hass
    manager = hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": SOURCE_USER})
    result, _ = await drive(hass, manager, result, {
        "user": dict(SYSTEM_INPUT), "batteries": {"num_batteries": 1},
    }, stop_at="battery_brand")
    options, default = selector_of(result)
    assert options == TODAY_OPTIONS
    assert default == "marstek"  # the config flow always starts on Marstek

    entry = flow_env.make_entry([dict(CASES["zendure"].existing)])
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result, _ = await drive(hass, manager, result, {
        "init": "batteries", "battery_count": {"num_batteries": 1},
    }, stop_at="battery_brand")
    options, default = selector_of(result)
    assert options == TODAY_OPTIONS
    assert default == "zendure"  # the options flow starts on the stored brand


async def test_selector_lists_the_registered_fake_brand_last(flow_env, fake_brand):
    hass = flow_env.hass
    manager = hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": SOURCE_USER})
    result, _ = await drive(hass, manager, result, {
        "user": dict(SYSTEM_INPUT), "batteries": {"num_batteries": 1},
    }, stop_at="battery_brand")
    options, _ = selector_of(result)
    assert options == TODAY_OPTIONS + [{"value": FAKE, "label": "Fake Brand"}]


# --------------------------------------------------------------------------- config flow (add)


async def test_config_flow_add_resolves_the_step_and_stores_in_limits(flow_env, fake_brand):
    run = await flow_env.add_via_config_flow(FAKE_CASE)

    assert run.steps[2:5] == ["battery_brand", CONNECT, "battery_limits"]
    battery = run.batteries[0]
    assert battery["brand"] == FAKE
    assert battery["name"] == "Fake 1" and battery["host"] == "192.0.2.90"
    assert battery["max_soc"] and "enable_charge_hysteresis" in battery  # went through battery_limits
    ctx = FAKE_PROBE.contexts[0]
    assert ctx.kind is FlowKind.CONFIG
    assert ctx.battery is None and ctx.entry is None and ctx.coordinator is None
    assert getattr(FAKE_PROBE.flows[0], "_no_existe", 42) == 42


async def test_config_flow_error_form_keeps_the_submitted_values(flow_env, fake_brand):
    FAKE_PROBE.reachable = False
    hass = flow_env.hass
    manager = hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": SOURCE_USER})
    result, _ = await drive(hass, manager, result, {
        "user": dict(SYSTEM_INPUT), "batteries": {"num_batteries": 1},
        "battery_brand": {"brand": FAKE},
    }, stop_at=CONNECT)

    submitted = {"name": "Typed name", "host": "10.0.0.9"}
    result = await manager.async_configure(result["flow_id"], submitted)

    assert result["type"] is FlowResultType.FORM and result["step_id"] == CONNECT
    assert result["errors"] == {"base": "cannot_connect"}
    assert shown_values(result) == submitted  # not the form's own defaults


async def test_conditional_step_appears_only_when_asked_and_work_survives(flow_env, fake_brand):
    FAKE_PROBE.wants_extra = True
    run = await flow_env.add_via_config_flow(FAKE_CASE)

    assert run.steps[2:6] == ["battery_brand", CONNECT, EXTRA, "battery_limits"]
    battery = run.batteries[0]
    assert battery["fake_extra"] == "yes"
    assert battery["fake_attempts"] == 1  # written by step 1, read by step 2
    assert FAKE_PROBE.works[-1]["attempts"] == 1  # the extra step saw the dict of the first

    FAKE_PROBE.wants_extra = False
    run = await flow_env.add_via_config_flow(FAKE_CASE)
    assert EXTRA not in run.steps
    assert "fake_extra" not in run.batteries[0]


# --------------------------------------------------------------------------- options flow


async def test_options_flow_resolves_the_step_with_the_stored_battery(flow_env, fake_brand):
    entry = flow_env.make_entry([dict(FAKE_CASE.existing)])
    run = await flow_env.edit_via_options_flow(FAKE_CASE, entry)

    assert run.steps[2:5] == ["battery_brand", CONNECT, "battery_limits"]
    stored = run.batteries[0]
    assert stored["name"] == "Fake 1 edited" and stored["host"] == "192.0.2.91"
    assert stored["kept_runtime_key"] == "kept"  # the options flow lays data over the stored one
    ctx = FAKE_PROBE.contexts[0]
    assert ctx.kind is FlowKind.OPTIONS
    assert ctx.battery["host"] == "192.0.2.90"
    assert ctx.entry is entry and ctx.coordinator is None
    assert getattr(FAKE_PROBE.flows[0], "_no_existe", 42) == 42


async def test_options_flow_error_form_keeps_the_submitted_values(flow_env, fake_brand):
    FAKE_PROBE.reachable = False
    entry = flow_env.make_entry([dict(FAKE_CASE.existing)])
    hass = flow_env.hass
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result, _ = await drive(hass, manager, result, {
        "init": "batteries", "battery_count": {"num_batteries": 1},
        "battery_brand": {"brand": FAKE},
    }, stop_at=CONNECT)
    assert shown_values(result)["name"] == "Fake 1"  # opened on the stored values

    submitted = {"name": "Typed name", "host": "10.0.0.9"}
    result = await manager.async_configure(result["flow_id"], submitted)

    assert result["errors"] == {"base": "cannot_connect"}
    assert shown_values(result) == submitted


async def test_options_flow_conditional_step(flow_env, fake_brand):
    FAKE_PROBE.wants_extra = True
    entry = flow_env.make_entry([dict(FAKE_CASE.existing)])
    run = await flow_env.edit_via_options_flow(FAKE_CASE, entry)

    assert run.steps[2:6] == ["battery_brand", CONNECT, EXTRA, "battery_limits"]
    stored = run.batteries[0]
    assert stored["fake_extra"] == "yes" and stored["fake_attempts"] == 1


# --------------------------------------------------------------------------- reconfigure


async def test_reconfigure_dispatches_and_saves_into_the_entry(flow_env, fake_brand):
    entry = flow_env.make_entry([dict(FAKE_CASE.existing)])
    run = await flow_env.reconfigure(FAKE_CASE, entry)

    assert run.steps == [RECONFIGURE]
    stored = run.batteries[0]
    assert stored["name"] == "Fake 1 moved" and stored["host"] == "192.0.2.92"
    assert stored["kept_runtime_key"] == "kept" and stored["brand"] == FAKE
    ctx = FAKE_PROBE.contexts[0]
    assert ctx.kind is FlowKind.RECONFIGURE
    assert ctx.entry is entry and ctx.battery["host"] == "192.0.2.90"
    assert getattr(FAKE_PROBE.flows[0], "_no_existe", 42) == 42


async def test_reconfigure_walks_every_battery_before_saving(flow_env, fake_brand, probes):
    zendure = dict(CASES["zendure"].existing)
    entry = flow_env.make_entry([dict(FAKE_CASE.existing), zendure])
    hass = flow_env.hass
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result, steps = await drive(hass, manager, result, {
        RECONFIGURE: FAKE_CASE.reconfigure_input,
        CASES["zendure"].reconfigure_step: CASES["zendure"].reconfigure_input,
    })
    await hass.async_block_till_done()

    assert steps == [RECONFIGURE, CASES["zendure"].reconfigure_step]
    assert result["reason"] == "reconfigure_successful"
    assert [b["name"] for b in entry.data["batteries"]] == ["Fake 1 moved", "Zendure 1 moved"]


def test_setup_module_imports_neither_config_flow_nor_registry():
    """The import direction that keeps brand flows from creating a cycle."""
    import ast
    from pathlib import Path

    from custom_components.omnibattery.drivers import setup

    tree = ast.parse(Path(setup.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append("." * node.level + (node.module or ""))
            imported += ["." * node.level + (node.module or "") + "." + a.name for a in node.names]
        elif isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
    assert not [m for m in imported if "config_flow" in m or "registry" in m], imported
