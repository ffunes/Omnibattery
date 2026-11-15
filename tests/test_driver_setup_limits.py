"""Extension points of the shared limits step (driver-registry T6.2).

The shared ``battery_limits`` step (config flow and options flow) and the
time-slot forms ask ONE place for everything brand-specific: the brand's
``DriverSetup`` when it has one, otherwise (an unregistered brand) the ``SETUP`` of
``DEFAULT_BRAND``. The five hooks are
``power_ceilings``, ``limits_schema`` and ``finalize_limits`` (the time-slot
ceiling is ``BatteryDriver.slot_power_ceiling``); the SOC ranges and the capacity field come from
``DriverTraits``.

Without Home Assistant: the traits, ``_soc_selector_limits`` against the function
it replaced, the traits against ``DriverCapabilities``, the slot ceiling.
With the ``hass`` fixture: a fake brand that sets ceilings, removes a field from
the form and writes a key on save, in the config flow and the options flow::

    .venv-test/Scripts/python -m pytest -q -o addopts="" tests/test_driver_setup_limits.py
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import voluptuous as vol

from custom_components.omnibattery.config_flow import (
    MarstekVenusConfigFlow,
    OptionsFlowHandler,
    _battery_hardware_max,
    _build_slot_step_b_schema,
    _install_driver_steps,
    _limits_setup,
    _soc_selector_limits,
)
from custom_components.omnibattery.const import REGISTER_MAP
from custom_components.omnibattery.drivers import registry
from custom_components.omnibattery.drivers.base import (
    BatteryDriver,
    DriverCapabilities,
    DriverContext,
)
from custom_components.omnibattery.drivers.setup import (
    DriverSetup,
    FlowKind,
    SetupContext,
)
from tests.flow_harness import (  # noqa: F401  (fixtures are used by name)
    CASES,
    BrandCase,
    _stored,
    drive,
    flow_env,
    probes,
)
from tests.test_driver_construction_characterization import BRAND_CONFIGS

BRANDS = list(registry.DRIVERS)


# --------------------------------------------------------------------------- traits


def _old_soc_selector_limits(brand, battery_version="v2"):
    """``_soc_selector_limits`` as it was before T6.2: the oracle of the rewrite."""
    max_lo = 80
    if brand == "zendure":
        min_lo, min_hi, min_default = 5, 50, 12
    elif brand == "anker":
        min_lo, min_hi, min_default = 0, 20, 10
    elif brand == "sessy":
        min_lo, min_hi, min_default = 0, 30, 5
    elif brand == "indevolt":
        min_lo, min_hi, min_default = 5, 50, 20
    elif brand == "hoymiles":
        min_lo, min_hi, min_default = 0, 30, 10
    elif brand == "huawei":
        min_lo, min_hi, min_default = 0, 30, 10
    else:
        min_lo, min_hi, min_default = 12, 30, 12
        # fixed: plan section 8 T1.1: an unregistered brand is served by the Marstek driver.
        if (brand == "marstek" or brand not in BRANDS) and REGISTER_MAP.get(battery_version, {}).get("discharging_cutoff_capacity") is None:
            min_hi = int(DriverCapabilities.discharge_cutoff_range[1])
            max_lo = int(DriverCapabilities.charge_cutoff_range[0])
    return min_lo, min_hi, min_default, max_lo, 100, 100


@pytest.mark.parametrize(
    "brand", BRANDS + ["not_registered", "", None]
)
@pytest.mark.parametrize("version", ["v2", "v3", "vA", "vD", "v9", "", None])
def test_soc_selector_limits_read_from_traits_match_the_old_function(brand, version):
    assert _soc_selector_limits(brand, version) == _old_soc_selector_limits(brand, version)


def test_soc_selector_limits_default_version_is_the_old_default():
    for brand in BRANDS:
        assert _soc_selector_limits(brand) == _old_soc_selector_limits(brand)


# brand -> (capacity_field, min_soc_range); max_soc_low is 80 everywhere but Marstek v3/vA/vD.
TRAIT_TABLE = {
    "marstek": ("none", (12, 30, 12)),   # (12, 50, 12) and 50 on v3/vA/vD, see below
    "zendure": ("optional", (5, 50, 12)),
    "esphome": ("none", (12, 30, 12)),
    "anker": ("none", (0, 20, 10)),
    "sessy": ("required", (0, 30, 5)),
    "indevolt": ("none", (5, 50, 20)),
    "hoymiles": ("optional", (0, 30, 10)),
    "huawei": ("none", (0, 30, 10)),
}


@pytest.mark.parametrize("brand", list(TRAIT_TABLE))
def test_traits_of_every_brand(brand):
    capacity_field, min_range = TRAIT_TABLE[brand]
    traits = registry.DRIVERS[brand].TRAITS
    assert traits.capacity_field == capacity_field
    assert traits.min_soc_range == min_range
    assert traits.max_soc_low == 80


@pytest.mark.parametrize("version", ["v3", "vA", "vD"])
def test_marstek_software_soc_versions_widen_the_ranges(version):
    traits = registry.DRIVERS["marstek"].traits_for({"battery_version": version})
    assert traits.min_soc_range == (12, 50, 12)
    assert traits.max_soc_low == 50


@pytest.mark.parametrize("config", [{"battery_version": "v2"}, {}, {"battery_version": "v9"}])
def test_marstek_v2_keeps_the_register_ranges(config):
    # v2 has the cutoff registers; an unsupported version does not (the form has
    # always read the raw version), so v9 is the software case like v3.
    traits = registry.DRIVERS["marstek"].traits_for(config)
    if config.get("battery_version") == "v9":
        assert (traits.min_soc_range, traits.max_soc_low) == ((12, 50, 12), 50)
    else:
        assert (traits.min_soc_range, traits.max_soc_low) == ((12, 30, 12), 80)


def test_other_brands_ignore_the_stored_version_for_the_ranges():
    for brand in BRANDS:
        if brand == "marstek":
            continue
        assert registry.DRIVERS[brand].traits_for({"battery_version": "vD"}).min_soc_range == (
            registry.DRIVERS[brand].TRAITS.min_soc_range
        )


# What the traits say vs what the driver tells the entities (#56, plan 3.10).
# (min range lo, hi) == discharge_cutoff_range and (max_soc_low, 100) ==
# charge_cutoff_range are compared on a live driver. They agree for Marstek
# v3/vA/vD (both) and for Indevolt's minimum (it overrides the discharge range
# itself). They do NOT agree elsewhere; nothing is changed here, the difference
# is pinned so that a later task that aligns them has to say so.
#   discharge: traits (lo, hi) vs capabilities
#   charge: traits (max_soc_low, 100) vs capabilities
CAPABILITY_MATCH = {
    "marstek": (True, True),     # v3 config in BRAND_CONFIGS
    "zendure": (False, False),
    "esphome": (False, False),
    "anker": (False, False),
    "sessy": (False, False),
    "indevolt": (True, False),   # discharge (5, 50) agrees; charge keeps the (50, 100) default
    "hoymiles": (False, False),
    "huawei": (False, False),    # driver says (0, 20) and (90, 100)
}


@pytest.mark.parametrize("brand", list(CAPABILITY_MATCH))
async def test_traits_vs_driver_capabilities(brand):
    config = BRAND_CONFIGS[brand]
    context = DriverContext(
        hass=MagicMock(), name="t", device_key="k", max_charge_power_w=2000,
        max_discharge_power_w=2000, persist=lambda k, v: None, stored=lambda k: None,
    )
    driver = registry.DRIVERS[brand].from_config(config, context)
    caps = driver.capabilities
    traits = registry.DRIVERS[brand].traits_for(config)
    discharge_ok = (float(traits.min_soc_range[0]), float(traits.min_soc_range[1])) == caps.discharge_cutoff_range
    charge_ok = (float(traits.max_soc_low), 100.0) == caps.charge_cutoff_range
    assert (discharge_ok, charge_ok) == CAPABILITY_MATCH[brand]


# --------------------------------------------------------------------------- the legacy capacity field


def _field_names(fields: dict) -> dict:
    return {str(k.schema): k for k in fields}


@pytest.mark.parametrize("brand", BRANDS)
@pytest.mark.parametrize("kind", [FlowKind.CONFIG, FlowKind.OPTIONS])
def test_legacy_capacity_field_follows_the_trait(brand, kind):
    ctx = SetupContext(kind=kind, battery=None, entry=None, coordinator=None)
    fields = _field_names(_limits_setup(brand).limits_schema({"brand": brand}, {}, ctx))
    field = fields.get("battery_capacity_kwh")
    expected = registry.DRIVERS[brand].TRAITS.capacity_field
    if expected == "none":
        assert field is None
    elif expected == "required":
        assert isinstance(field, vol.Required)
    else:
        assert isinstance(field, vol.Optional)


# --------------------------------------------------------------------------- the slot ceiling


def test_slot_ceiling_of_a_brand_without_setup_is_todays_rule():
    # Marstek version table, whatever the brand; else the configured max; else 2500.
    assert _battery_hardware_max({"brand": "marstek", "battery_version": "v3"}) == 2500
    assert _battery_hardware_max({"brand": "zendure", "battery_version": "v3"}) == 2500  # stale key
    assert _battery_hardware_max({"brand": "zendure", "max_charge_power": 1800}) == 1800
    assert _battery_hardware_max({"brand": "zendure"}) == 2500
    assert _battery_hardware_max({}) == 2500


# --------------------------------------------------------------------------- the fake brand

FAKE = "limitsbrand"
CONNECT = "battery_connection_limits"
SEEN: list[tuple[str, FlowKind]] = []
BEHAVIOUR = {"remove_power": False}


async def _connection(flow, user_input, ctx):
    if user_input is None:
        return flow.async_show_form(
            step_id=CONNECT,
            data_schema=vol.Schema({
                vol.Required("name", default=(ctx.battery or {}).get("name", "L 1")): str,
                vol.Required("host", default=(ctx.battery or {}).get("host", "")): str,
            }),
        )
    return {"name": user_input["name"], "host": user_input["host"], "port": 1, "brand": FAKE}


class LimitsSetup(DriverSetup):
    def power_ceilings(self, battery_data):
        return 1500, 1200

    def limits_schema(self, battery_data, base_fields, ctx):
        SEEN.append(("schema", ctx.kind))
        drop = {"backup_offgrid_threshold"}
        if BEHAVIOUR["remove_power"]:
            drop |= {"max_charge_power", "max_discharge_power"}
        fields = {k: v for k, v in base_fields.items() if str(k.schema) not in drop}
        fields[vol.Optional("limits_note", default="n/a")] = str
        return fields

    def finalize_limits(self, merged, user_input, battery_data, ctx):
        SEEN.append(("finalize", ctx.kind))
        merged["limits_note_saved"] = user_input["limits_note"]
        merged["finalized_from"] = ctx.kind.value
        if BEHAVIOUR["remove_power"]:
            merged["max_charge_power"] = 777
            merged["max_discharge_power"] = 666


LIMITS_SETUP = LimitsSetup(steps={CONNECT: _connection}, entry_step=CONNECT)


class LimitsDriver(BatteryDriver):
    BRAND = FAKE
    LABEL = "Limits Brand"
    SETUP = LIMITS_SETUP

    @classmethod
    def slot_power_ceiling(cls, battery_config):
        return 1234


LIMITS_CASE = BrandCase(
    id=FAKE,
    brand=FAKE,
    connection_step=CONNECT,
    reconfigure_step="",
    add_input={"name": "L 1", "host": "192.0.2.95"},
    options_input={"name": "L 1 edited", "host": "192.0.2.96"},
    reconfigure_input={},
    existing=_stored(brand=FAKE, name="L 1", host="192.0.2.95", port=1),
    limits_input={"limits_note": "hello"},
    options_limits_input={"limits_note": "hello"},
)


@pytest.fixture
def limits_brand(monkeypatch):
    SEEN.clear()
    BEHAVIOUR["remove_power"] = False
    monkeypatch.setitem(registry.DRIVERS, FAKE, LimitsDriver)
    for cls in (MarstekVenusConfigFlow, OptionsFlowHandler):
        _install_driver_steps(
            cls, LIMITS_SETUP, FAKE, setter=lambda c, n, m: monkeypatch.setattr(c, n, m, raising=False)
        )


def _slider_max(form, name) -> float:
    (selector,) = [v for k, v in form["data_schema"].schema.items() if str(k.schema) == name]
    return selector.config["max"]


def _names(form) -> list[str]:
    return [str(k.schema) for k in form["data_schema"].schema]


def test_slot_ceiling_of_a_brand_comes_from_its_driver_class(limits_brand):
    assert _battery_hardware_max({"brand": FAKE, "battery_version": "v3", "max_charge_power": 900}) == 1234
    schema = _build_slot_step_b_schema(
        False, True, "all", [{"brand": FAKE, "max_charge_power": 900}], {}
    )
    maxima = {str(k.schema): v.config["max"] for k, v in schema.schema.items()}
    assert maxima == {
        "battery_1__max_charge_power_w": 1234,
        "battery_1__max_discharge_power_w": 1234,
    }


async def test_config_flow_limits_use_the_hooks(flow_env, limits_brand):
    hass = flow_env.hass
    manager = hass.config_entries.flow
    from homeassistant.config_entries import SOURCE_USER
    from tests.flow_harness import DOMAIN, SYSTEM_INPUT

    result = await manager.async_init(DOMAIN, context={"source": SOURCE_USER})
    form, _ = await drive(hass, manager, result, {
        "user": dict(SYSTEM_INPUT), "batteries": {"num_batteries": 1},
        "battery_brand": {"brand": FAKE}, CONNECT: LIMITS_CASE.add_input,
    }, stop_at="battery_limits")

    assert _slider_max(form, "max_charge_power") == 1500   # power_ceilings
    assert _slider_max(form, "max_discharge_power") == 1200
    names = _names(form)
    assert "backup_offgrid_threshold" not in names          # field removed
    assert "limits_note" in names                           # field added
    assert "full_charge_voltage_taper_enabled" not in names  # no legacy brand field leaks in
    assert ("schema", FlowKind.CONFIG) in SEEN

    run = await flow_env.add_via_config_flow(LIMITS_CASE)
    battery = run.batteries[0]
    assert battery["limits_note_saved"] == "hello"          # finalize_limits
    assert battery["finalized_from"] == "config"
    assert battery["max_charge_power"] == 1500 and battery["max_discharge_power"] == 1200
    assert battery["backup_offgrid_threshold"] == 50        # shared default, field was removed
    assert "dc_pv_connected" not in battery                 # nothing legacy runs for it


async def test_config_flow_hook_can_force_the_power_limits(flow_env, limits_brand):
    BEHAVIOUR["remove_power"] = True
    hass = flow_env.hass
    manager = hass.config_entries.flow
    from homeassistant.config_entries import SOURCE_USER
    from tests.flow_harness import DOMAIN, SYSTEM_INPUT

    result = await manager.async_init(DOMAIN, context={"source": SOURCE_USER})
    form, _ = await drive(hass, manager, result, {
        "user": dict(SYSTEM_INPUT), "batteries": {"num_batteries": 1},
        "battery_brand": {"brand": FAKE}, CONNECT: LIMITS_CASE.add_input,
    }, stop_at="battery_limits")
    assert "max_charge_power" not in _names(form)
    assert "max_discharge_power" not in _names(form)

    run = await flow_env.add_via_config_flow(LIMITS_CASE)
    assert run.batteries[0]["max_charge_power"] == 777
    assert run.batteries[0]["max_discharge_power"] == 666


async def test_options_flow_limits_use_the_hooks(flow_env, limits_brand):
    entry = flow_env.make_entry([dict(LIMITS_CASE.existing)])
    hass = flow_env.hass
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    form, _ = await drive(hass, manager, result, {
        "init": "batteries", "battery_count": {"num_batteries": 1},
        "battery_brand": {"brand": FAKE}, CONNECT: LIMITS_CASE.options_input,
    }, stop_at="battery_limits")

    assert _slider_max(form, "max_charge_power") == 1500
    assert _slider_max(form, "max_discharge_power") == 1200
    names = _names(form)
    assert "backup_offgrid_threshold" not in names and "limits_note" in names
    assert ("schema", FlowKind.OPTIONS) in SEEN

    run = await flow_env.edit_via_options_flow(LIMITS_CASE, entry)
    stored = run.batteries[0]
    assert stored["limits_note_saved"] == "hello"
    assert stored["finalized_from"] == "options"
    assert stored["max_charge_power"] == 1500  # the stored 1500 is within the ceiling
    assert stored["kept_runtime_key"] == "kept"
    assert "dc_pv_connected" in stored  # kept from the stored battery, not rewritten


async def test_options_flow_hook_can_force_the_power_limits(flow_env, limits_brand):
    BEHAVIOUR["remove_power"] = True
    run = await flow_env.edit_via_options_flow(LIMITS_CASE)
    stored = run.batteries[0]
    assert stored["max_charge_power"] == 777 and stored["max_discharge_power"] == 666



# --------------------------------------------------------------------------- an unregistered brand


def test_unregistered_brand_uses_the_default_brand_setup():
    assert _limits_setup("not_registered") is registry.DRIVERS[registry.DEFAULT_BRAND].SETUP
    assert _limits_setup("not_registered").power_ceilings({"battery_version": "v3"}) == (2500, 2500)


@pytest.mark.parametrize("brand,has_dc", [("marstek", True), (None, True), ("not_registered", False)])
def test_dc_field_is_offered_only_to_a_marstek_on_va_vd(brand, has_dc):
    ctx = SetupContext(kind=FlowKind.CONFIG, battery=None, entry=None, coordinator=None)
    data = {"battery_version": "vA"} | ({} if brand is None else {"brand": brand})
    names = _field_names(_limits_setup(brand or "marstek").limits_schema(data, {}, ctx))
    assert ("dc_pv_connected" in names) is has_dc
    assert "full_charge_voltage_taper_enabled" in names
