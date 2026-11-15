"""Characterization of how a battery config becomes a coordinator + driver.

Pins today's behaviour for the eight brands so the driver-registry refactor
(docs/plans/driver-registry, T2.2) can move the construction without changing it.
Every value below is a literal observed on the pre-refactor code; if one changes,
behaviour changed.

``_build_coordinator`` is the ONLY place that knows how a ``battery_config`` dict
becomes a coordinator. It mirrors ``_setup_battery`` in ``__init__.py`` (the
``MarstekVenusDataUpdateCoordinator(hass, battery_config, ...)`` call and the
assignments right after it); T2.2 moved the per-brand mapping into each driver's
``from_config``, so only this helper changed, never the assertions.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.omnibattery.const import (
    CONF_BATTERY_MANUAL_MODE_ENABLED,
    CONF_BATTERY_PHASE,
    CONF_BATTERY_VERSION,
    CONF_DC_PV_CONNECTED,
    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    CONF_RS485_GATEWAY,
    CONF_SERIAL_PORT,
    CONF_SLAVE_ID,
    DEFAULT_CHARGE_HYSTERESIS_PERCENT,
    DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    DEFAULT_SLAVE_ID,
    DEFAULT_VERSION,
    normalize_battery_phase,
)
from custom_components.omnibattery.drivers.anker import AnkerModbusDriver
from custom_components.omnibattery.drivers.esphome import EsphomeEntityDriver
from custom_components.omnibattery.drivers.hoymiles import HoymilesMqttDriver
from custom_components.omnibattery.drivers.huawei import HuaweiSolarDriver
from custom_components.omnibattery.drivers.indevolt import IndevoltDriver
from custom_components.omnibattery.drivers.marstek import MarstekModbusDriver
from custom_components.omnibattery.drivers.sessy import SessyLocalDriver
from custom_components.omnibattery.drivers.zendure import ZendureLocalDriver
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)
from custom_components.omnibattery.drivers.indevolt_client import CONF_INDEVOLT_REQUEST_SPEED, SLOT_S


@pytest.fixture(autouse=True)
def _no_ha_runtime(monkeypatch):
    """The coordinator builds without a Home Assistant loop or any network."""
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_a, **_k: None)


def _build_coordinator(battery_config, *, hass=None, mac=None, battery_index=0, **overrides):
    """Build a coordinator the way ``_setup_battery`` does (see module docstring).

    ``overrides`` replace individual constructor kwargs.
    """
    hass = hass if hass is not None else MagicMock()
    entry = SimpleNamespace(
        entry_id="entry1",
        data={"consumption_sensor": "sensor.grid_power", "batteries": [battery_config]},
    )
    kwargs = dict(
        consumption_sensor=entry.data["consumption_sensor"],
        backup_discharge_store=None,
        backup_discharge_store_key=f"{entry.entry_id}:{battery_index}",
        mac=mac,
    )
    kwargs.update(overrides)
    coordinator = MarstekVenusDataUpdateCoordinator(hass, battery_config, **kwargs)
    coordinator.hass = hass  # DataUpdateCoordinator.__init__ (stubbed here) sets it
    coordinator.phase = normalize_battery_phase(battery_config.get(CONF_BATTERY_PHASE))
    coordinator._config_entry = entry
    coordinator.rs485_user_disabled = battery_config.get("rs485_user_disabled", False)
    coordinator.battery_manual_mode_enabled = bool(
        battery_config.get(CONF_BATTERY_MANUAL_MODE_ENABLED, False)
    )
    coordinator.battery_capacity_kwh = battery_config.get("battery_capacity_kwh", 0.0)
    coordinator.configured_max_charge_power = battery_config.get(
        "user_max_charge_power",
        battery_config.get("max_charge_power", coordinator.configured_max_charge_power),
    )
    coordinator.configured_max_discharge_power = battery_config.get(
        "user_max_discharge_power",
        battery_config.get("max_discharge_power", coordinator.configured_max_discharge_power),
    )
    coordinator.manual_force_mode = battery_config.get("manual_force_mode", "None")
    coordinator.manual_set_charge_power = min(
        battery_config.get("manual_set_charge_power", 0), coordinator.effective_max_charge_power
    )
    coordinator.manual_set_discharge_power = min(
        battery_config.get("manual_set_discharge_power", 0),
        coordinator.effective_max_discharge_power,
    )
    coordinator.commanded_charge_power = coordinator.manual_set_charge_power
    coordinator.commanded_discharge_power = coordinator.manual_set_discharge_power
    coordinator._shadow_selects = {
        k[len("shadow_select_"):]: v
        for k, v in battery_config.items()
        if k.startswith("shadow_select_")
    }
    return coordinator


def _config(brand, **extra):
    base = {
        "name": f"{brand} battery", "host": "192.0.2.10", "port": 502,
        "max_charge_power": 2000, "max_discharge_power": 2000,
        "max_soc": 100, "min_soc": 12,
    }
    if brand is not None:
        base["brand"] = brand
    base.update(extra)
    return base


MARSTEK = _config("marstek", battery_version="v3", slave_id=1)
ZENDURE = _config("zendure", port=80, zendure_model="2400ac_pro")
ESPHOME = _config("esphome", host="a1b2c3d4e5f6", port=0, esphome_device_id="a1b2c3d4e5f6")
ANKER = _config("anker", slave_id=2)
SESSY = _config("sessy", port=80, username="user", password="pw",
                max_charge_power=2200, max_discharge_power=1700)
INDEVOLT = _config("indevolt", port=8080, indevolt_request_speed="fast")
HUAWEI = _config("huawei", slave_id=3, huawei_battery_device_id="dev123",
                 huawei_direct_write=True, huawei_emma_slave_id=0,
                 max_charge_power=5000, max_discharge_power=5000)
HOYMILES = _config("hoymiles", host="device-id-1", port=0, hoymiles_model="MS-A2")

BRAND_CONFIGS = {
    "marstek": MARSTEK, "zendure": ZENDURE, "esphome": ESPHOME, "anker": ANKER,
    "sessy": SESSY, "indevolt": INDEVOLT, "huawei": HUAWEI, "hoymiles": HOYMILES,
}


# --------------------------------------------------------------------------- #
# Per brand: driver type, constructor arguments, device_info, capabilities.
# --------------------------------------------------------------------------- #

# brand -> (driver class, attributes of the driver, coordinator attributes, device_info extras)
EXPECTED = {
    "marstek": (
        MarstekModbusDriver,
        {"_version": "v3", "_ems_version": None, "_slave_id": 1},
        {
            "battery_version": "v3", "dc_pv_connected": False,
            "full_charge_voltage_taper_enabled": True,
            "needs_software_power_cap": True, "needs_software_max_charge": False,
            "needs_software_max_discharge": False, "needs_software_manual_control": False,
            "device_max_charge_power": 2500, "device_max_discharge_power": 2500,
            "configured_max_charge_power": 2000, "configured_max_discharge_power": 2000,
        },
        {"manufacturer": "Marstek", "model": "Venus E v3"},
    ),
    "zendure": (
        ZendureLocalDriver,
        {"_host": "192.0.2.10", "_port": 80, "_base_url": "http://192.0.2.10",
         "_model": "2400ac_pro"},
        {
            "battery_version": None, "dc_pv_connected": False,
            "full_charge_voltage_taper_enabled": False,
            "needs_software_power_cap": False, "needs_software_max_charge": True,
            "needs_software_max_discharge": False, "needs_software_manual_control": True,
            "device_max_charge_power": 2400, "device_max_discharge_power": 2400,
            "configured_max_charge_power": 2000, "configured_max_discharge_power": 2000,
        },
        {"manufacturer": "Zendure", "model": "Zendure"},
    ),
    "esphome": (
        EsphomeEntityDriver,
        {"_device_id": "a1b2c3d4e5f6"},
        {
            "battery_version": None, "dc_pv_connected": False,
            # ESPHome is the one non-Marstek brand outside the coordinator taper-off
            # list: the stored value is kept.
            "full_charge_voltage_taper_enabled": True,
            "needs_software_power_cap": False, "needs_software_max_charge": False,
            "needs_software_max_discharge": False, "needs_software_manual_control": False,
            "device_max_charge_power": 2000, "device_max_discharge_power": 2000,
            "configured_max_charge_power": 2000, "configured_max_discharge_power": 2000,
        },
        # ESPHome falls into the "Marstek" manufacturer branch.
        {"manufacturer": "Marstek", "model": "Venus E (LilyGo RS485)"},
    ),
    "anker": (
        AnkerModbusDriver,
        {"_host": "192.0.2.10", "_port": 502, "_slave_id": 2},
        {
            "battery_version": None, "dc_pv_connected": False,
            "full_charge_voltage_taper_enabled": False,
            "needs_software_power_cap": False, "needs_software_max_charge": True,
            "needs_software_max_discharge": True, "needs_software_manual_control": True,
            # The driver ignores the user limits and declares the hardware maximum.
            "device_max_charge_power": 3500, "device_max_discharge_power": 3500,
            "configured_max_charge_power": 2000, "configured_max_discharge_power": 2000,
        },
        {"manufacturer": "Anker", "model": "Solarbank Max AC"},
    ),
    "sessy": (
        SessyLocalDriver,
        {"_base_url": "http://192.0.2.10", "_headers": {"Authorization": "Basic dXNlcjpwdw=="}},
        {
            "battery_version": None, "dc_pv_connected": False,
            # The coordinator taper-off list omits Sessy (D3): the stored True stays.
            "full_charge_voltage_taper_enabled": True,
            "needs_software_power_cap": False, "needs_software_max_charge": True,
            "needs_software_max_discharge": True, "needs_software_manual_control": True,
            "device_max_charge_power": 2200, "device_max_discharge_power": 1700,
            "configured_max_charge_power": 2200, "configured_max_discharge_power": 1700,
        },
        {"manufacturer": "Sessy", "model": "Sessy", "configuration_url": "http://192.0.2.10/"},
    ),
    "indevolt": (
        IndevoltDriver,
        {"_host": "192.0.2.10", "_port": 8080, "_slot_s": 2.0, "_name": "indevolt battery"},
        {
            "battery_version": None, "dc_pv_connected": False,
            "full_charge_voltage_taper_enabled": False,
            "needs_software_power_cap": False, "needs_software_max_charge": False,
            "needs_software_max_discharge": False, "needs_software_manual_control": True,
            "device_max_charge_power": 2000, "device_max_discharge_power": 2000,
            "configured_max_charge_power": 2000, "configured_max_discharge_power": 2000,
        },
        {"manufacturer": "Indevolt", "model": "Indevolt"},
    ),
    "huawei": (
        HuaweiSolarDriver,
        {"_battery_device_id": "dev123", "_direct_write": True},
        {
            "battery_version": None, "dc_pv_connected": False,
            "full_charge_voltage_taper_enabled": False,
            "needs_software_power_cap": False, "needs_software_max_charge": True,
            "needs_software_max_discharge": True, "needs_software_manual_control": True,
            "device_max_charge_power": 5000, "device_max_discharge_power": 5000,
            "configured_max_charge_power": 5000, "configured_max_discharge_power": 5000,
        },
        {"manufacturer": "Huawei", "model": "Huawei LUNA2000"},
    ),
    "hoymiles": (
        HoymilesMqttDriver,
        {"device_id": "device-id-1", "_model_raw": "MS-A2",
         "_configured_max_charge_w": 2000, "_configured_max_discharge_w": 2000},
        {
            "battery_version": None, "dc_pv_connected": False,
            "full_charge_voltage_taper_enabled": False,
            "needs_software_power_cap": False, "needs_software_max_charge": True,
            "needs_software_max_discharge": True, "needs_software_manual_control": True,
            "device_max_charge_power": 2000, "device_max_discharge_power": 2000,
            "configured_max_charge_power": 2000, "configured_max_discharge_power": 2000,
        },
        # The Hoymiles driver reports its device id as the serial number.
        {"manufacturer": "Hoymiles", "model": "MS-A2", "serial_number": "device-id-1"},
    ),
}


@pytest.mark.parametrize("brand", list(EXPECTED))
async def test_brand_builds_its_driver_with_the_expected_arguments(brand):
    driver_cls, driver_attrs, _, _ = EXPECTED[brand]
    coordinator = _build_coordinator(BRAND_CONFIGS[brand])
    assert type(coordinator.driver) is driver_cls
    assert coordinator.brand == brand
    for attr, expected in driver_attrs.items():
        assert getattr(coordinator.driver, attr) == expected, attr


@pytest.mark.parametrize("brand", list(EXPECTED))
async def test_brand_coordinator_attributes(brand):
    _, _, coordinator_attrs, _ = EXPECTED[brand]
    coordinator = _build_coordinator(BRAND_CONFIGS[brand])
    for attr, expected in coordinator_attrs.items():
        assert getattr(coordinator, attr) == expected, attr
    # The driver capability envelope is what device_max_* falls back on.
    capabilities = coordinator.driver.capabilities
    assert capabilities.max_charge_power_w == coordinator_attrs["device_max_charge_power"]
    assert capabilities.max_discharge_power_w == coordinator_attrs["device_max_discharge_power"]


@pytest.mark.parametrize("brand", list(EXPECTED))
async def test_brand_device_info(brand):
    _, _, _, info_expected = EXPECTED[brand]
    config = BRAND_CONFIGS[brand]
    coordinator = _build_coordinator(config)
    expected = {
        "identifiers": {("omnibattery", coordinator.device_key)},
        "name": config["name"],
        **info_expected,
    }
    assert coordinator.battery_device_info == expected


@pytest.mark.parametrize(
    ("brand", "device_key"),
    [
        ("marstek", "192.0.2.10_502"),
        ("zendure", "192.0.2.10_80"),
        ("esphome", "a1b2c3d4e5f6_0"),
        ("anker", "192.0.2.10_502_2"),
        ("sessy", "192.0.2.10_80"),
        ("indevolt", "192.0.2.10_8080"),
        ("huawei", "192.0.2.10_502_3"),
        ("hoymiles", "device-id-1_0"),
    ],
)
async def test_device_key_per_brand(brand, device_key):
    assert _build_coordinator(BRAND_CONFIGS[brand]).device_key == device_key


@pytest.mark.parametrize(
    ("brand", "shows"),
    [
        ("sessy", True), ("indevolt", True),
        # Only these two brands write the firmware line (#19).
        ("marstek", False), ("zendure", False), ("anker", False), ("esphome", False),
        ("huawei", False), ("hoymiles", False),
    ],
)
async def test_sw_version_only_for_sessy_and_indevolt(brand, shows):
    coordinator = _build_coordinator(BRAND_CONFIGS[brand])
    assert "sw_version" not in coordinator.battery_device_info  # nothing polled yet
    coordinator.data = {"software_version": "1.2.3"}
    info = coordinator.battery_device_info
    assert ("sw_version" in info) is shows
    if shows:
        assert info["sw_version"] == "1.2.3"


async def test_configuration_url_only_for_sessy_and_omits_the_default_port():
    assert _build_coordinator(SESSY).battery_device_info["configuration_url"] == "http://192.0.2.10/"
    on_8080 = _build_coordinator({**SESSY, "port": 8080})
    assert on_8080.battery_device_info["configuration_url"] == "http://192.0.2.10:8080/"
    for brand, config in BRAND_CONFIGS.items():
        if brand != "sessy":
            assert "configuration_url" not in _build_coordinator(config).battery_device_info


async def test_device_info_publishes_the_mac_only_when_given():
    assert "connections" not in _build_coordinator(MARSTEK).battery_device_info
    coordinator = _build_coordinator(MARSTEK, mac="AA:BB:CC:DD:EE:FF")
    (connection,) = coordinator.battery_device_info["connections"]
    assert connection[1] == "aa:bb:cc:dd:ee:ff"


# --------------------------------------------------------------------------- #
# battery_version: Marstek only (fixed: plan section 8 T1.1); None for the other brands.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("brand", [b for b in EXPECTED if b != "marstek"])
async def test_non_marstek_brands_report_no_battery_version(brand):
    # fixed: plan section 8 #11 #12 #14 (T1.1): a stale Marstek version on another brand is ignored.
    assert _build_coordinator(BRAND_CONFIGS[brand]).battery_version is None
    stale = _build_coordinator({**BRAND_CONFIGS[brand], "battery_version": "v3"})
    assert stale.battery_version is None
    assert type(stale.driver) is EXPECTED[brand][0]


async def test_an_unsupported_battery_version_falls_back_to_v2():
    assert _build_coordinator({**MARSTEK, "battery_version": "v9"}).battery_version == "v2"


async def test_a_battery_without_a_version_key_is_v2():
    config = {k: v for k, v in MARSTEK.items() if k != "battery_version"}
    coordinator = _build_coordinator(config)
    assert coordinator.battery_version == "v2"
    assert coordinator.driver._version == "v2"


# --------------------------------------------------------------------------- #
# Fallbacks: no brand key, unknown brand.
# --------------------------------------------------------------------------- #


async def test_an_entry_without_a_brand_key_is_marstek():
    config = {k: v for k, v in MARSTEK.items() if k != "brand"}
    coordinator = _build_coordinator(config)
    assert coordinator.brand == "marstek"
    assert type(coordinator.driver) is MarstekModbusDriver
    assert coordinator.battery_device_info["manufacturer"] == "Marstek"
    assert coordinator.needs_software_power_cap is True


async def test_an_unknown_brand_falls_into_the_marstek_driver_branch():
    coordinator = _build_coordinator({**MARSTEK, "brand": "acme"})
    assert type(coordinator.driver) is MarstekModbusDriver
    assert coordinator.driver._version == "v3"
    assert coordinator.battery_device_info["manufacturer"] == "Marstek"
    assert coordinator.battery_device_info["model"] == "Venus E v3"
    # The taper is only forced off for the named brands, so it stays on.
    assert coordinator.full_charge_voltage_taper_enabled is True
    # needs_software_power_cap compares the brand string, so an unknown brand
    # (running a Marstek v3 driver) does NOT get the Marstek software cap.
    assert coordinator.brand == "acme"
    assert coordinator.needs_software_power_cap is False


# --------------------------------------------------------------------------- #
# Taper: the coordinator forces it off for 5 brands; Sessy and ESPHome keep it.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("brand", "stored", "effective"),
    [
        ("marstek", True, True), ("marstek", False, False),
        ("esphome", True, True), ("esphome", False, False),
        ("sessy", True, True), ("sessy", False, False),
        ("zendure", True, False), ("anker", True, False),
        ("hoymiles", True, False), ("huawei", True, False), ("indevolt", True, False),
    ],
)
async def test_effective_voltage_taper(brand, stored, effective):
    config = {**BRAND_CONFIGS[brand], CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED: stored}
    coordinator = _build_coordinator(config)
    assert getattr(coordinator, CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED) is effective


async def test_a_missing_taper_key_defaults_to_enabled():
    assert getattr(_build_coordinator(MARSTEK), CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED) is True
    assert getattr(_build_coordinator(SESSY), CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED) is True


async def test_sessy_taper_is_inert_because_the_driver_never_publishes_cell_voltage():
    """D3: the coordinator keeps Sessy taper on, but the taper reads
    ``max_cell_voltage`` and Sessy has no such key, so nothing ever acts on it."""
    coordinator = _build_coordinator({**SESSY, CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED: True})
    assert coordinator.full_charge_voltage_taper_enabled is True
    driver = coordinator.driver
    polled = {key for group in driver.read_groups for key in group.keys}
    defined = {d["key"] for d in driver.all_definitions}
    assert "max_cell_voltage" not in polled
    assert "max_cell_voltage" not in defined
    source = (
        Path(__file__).resolve().parents[1]
        / "custom_components" / "omnibattery" / "drivers" / "sessy.py"
    ).read_text(encoding="utf-8")
    assert "max_cell_voltage" not in source


# --------------------------------------------------------------------------- #
# dc_pv_connected
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("version", "stored", "expected"),
    [
        ("vA", None, True), ("vD", None, True),   # no key: vA/vD default to connected
        ("vA", True, True), ("vD", True, True),
        ("vA", False, False), ("vD", False, False),
        ("v2", None, False), ("v3", None, False),
        ("v2", True, False), ("v3", True, False),  # only vA/vD can have DC PV
    ],
)
async def test_marstek_dc_pv_connected(version, stored, expected):
    config = {**MARSTEK, "battery_version": version}
    if stored is not None:
        config[CONF_DC_PV_CONNECTED] = stored
    assert _build_coordinator(config).dc_pv_connected is expected


@pytest.mark.parametrize("brand", [b for b in EXPECTED if b != "marstek"])
async def test_dc_pv_connected_is_false_for_every_other_brand_even_when_stored_true(brand):
    config = {**BRAND_CONFIGS[brand], CONF_DC_PV_CONNECTED: True}
    assert _build_coordinator(config).dc_pv_connected is False


async def test_zendure_with_mppt_has_dc_pv_connected_forced_false():
    # Plan section 8: a Zendure with MPPT hardware never reaches dc_pv_connected=True.
    config = {**ZENDURE, "zendure_model": "4000mix_pro", CONF_DC_PV_CONNECTED: True}
    coordinator = _build_coordinator(config)
    assert coordinator.driver.capabilities.has_mppt_pv is True
    assert coordinator.dc_pv_connected is False
    assert coordinator.battery_version is None  # fixed: plan section 8 T1.1
    assert coordinator.device_max_charge_power == 4000


# --------------------------------------------------------------------------- #
# Marstek: vD clamp in the constructor, vA/vD power envelopes, needs_software_*.
# --------------------------------------------------------------------------- #


async def test_marstek_vd_clamps_a_stale_device_cap_to_the_driver_envelope():
    config = {**MARSTEK, "battery_version": "vD",
              "device_max_charge_power": 2500, "device_max_discharge_power": 2500}
    coordinator = _build_coordinator(config)
    assert coordinator.driver.capabilities.max_charge_power_w == 2200
    assert coordinator.device_max_charge_power == 2200
    assert coordinator.device_max_discharge_power == 2200


async def test_marstek_vd_keeps_a_lower_probed_device_cap():
    config = {**MARSTEK, "battery_version": "vD",
              "device_max_charge_power": 1000, "device_max_discharge_power": 800}
    coordinator = _build_coordinator(config)
    assert coordinator.device_max_charge_power == 1000
    assert coordinator.device_max_discharge_power == 800


async def test_marstek_other_versions_are_not_clamped_to_the_driver_envelope():
    # Only vD is clamped: a vA with a stale higher cap keeps it.
    config = {**MARSTEK, "battery_version": "vA",
              "device_max_charge_power": 2500, "device_max_discharge_power": 2500}
    coordinator = _build_coordinator(config)
    assert coordinator.driver.capabilities.max_charge_power_w == 1500
    assert coordinator.device_max_charge_power == 2500
    assert coordinator.device_max_discharge_power == 2500


@pytest.mark.parametrize(
    ("version", "model", "cap", "software_cap"),
    [("v2", "Venus E v2", 2500, True), ("v3", "Venus E v3", 2500, True),
     ("vA", "Venus A", 1500, False), ("vD", "Venus D", 2200, False)],
)
async def test_marstek_versions(version, model, cap, software_cap):
    coordinator = _build_coordinator({**MARSTEK, "battery_version": version})
    assert coordinator.driver._version == version
    assert coordinator.battery_device_info["model"] == model
    assert coordinator.device_max_charge_power == cap
    assert coordinator.needs_software_power_cap is software_cap
    assert coordinator.needs_software_max_charge is False
    assert coordinator.needs_software_max_discharge is False
    assert coordinator.needs_software_manual_control is False


async def test_marstek_link_arguments_reach_the_driver():
    coordinator = _build_coordinator(
        {**MARSTEK, "ems_version": 2, CONF_SERIAL_PORT: "/dev/ttyUSB0", CONF_RS485_GATEWAY: True}
    )
    driver = coordinator.driver
    assert driver._ems_version == 2
    assert driver._client._serial_port == "/dev/ttyUSB0"
    assert coordinator.serial_port == "/dev/ttyUSB0"
    assert coordinator.rs485_gateway is True
    # An empty serial port means TCP.
    assert _build_coordinator({**MARSTEK, CONF_SERIAL_PORT: ""}).serial_port is None


# --------------------------------------------------------------------------- #
# Zendure: no power limits configured; the user values stay out of the driver.
# --------------------------------------------------------------------------- #


async def test_zendure_driver_ignores_the_configured_power_limits():
    coordinator = _build_coordinator({**ZENDURE, "max_charge_power": 500, "max_discharge_power": 600})
    driver = coordinator.driver
    # The driver is built with no limits, so its envelope is the model hardware one.
    assert driver._max_charge_power_override is False
    assert driver._max_discharge_power_override is False
    assert driver.capabilities.max_charge_power_w == 2400
    assert driver.capabilities.max_discharge_power_w == 2400
    # The user ceiling lives on the coordinator instead.
    assert coordinator.device_max_charge_power == 2400
    assert coordinator.configured_max_charge_power == 500
    assert coordinator.configured_max_discharge_power == 600
    assert coordinator.effective_max_charge_power == 500
    assert coordinator.effective_max_discharge_power == 600


@pytest.mark.parametrize(
    ("model", "cap"),
    [("solarflow_800", (1000, 800)), ("1600ac_plus", (1600, 1600)), ("4000mix_pro", (4000, 4000))],
)
async def test_zendure_model_sets_the_hardware_envelope(model, cap):
    coordinator = _build_coordinator({**ZENDURE, "zendure_model": model})
    assert coordinator.driver._model == model
    assert (coordinator.device_max_charge_power, coordinator.device_max_discharge_power) == cap


async def test_zendure_without_a_model_key_is_the_2400ac_pro():
    config = {k: v for k, v in ZENDURE.items() if k != "zendure_model"}
    coordinator = _build_coordinator(config)
    assert coordinator.driver._model == "2400ac_pro"
    assert coordinator.zendure_model == "2400ac_pro"


async def test_sessy_clamps_legacy_symmetric_limits_to_its_envelope():
    legacy = _build_coordinator({**SESSY, "max_charge_power": 5000, "max_discharge_power": 5000})
    # The constructor clamps configured_max_* to the envelope, but _setup_battery then
    # restores the raw stored limits over it, so the clamp only survives in the
    # effective (min of device and configured) values.
    assert (legacy.configured_max_charge_power, legacy.configured_max_discharge_power) == (5000, 5000)
    assert (legacy.device_max_charge_power, legacy.device_max_discharge_power) == (2200, 1700)
    assert (legacy.effective_max_charge_power, legacy.effective_max_discharge_power) == (2200, 1700)
    assert (legacy.max_charge_power, legacy.max_discharge_power) == (2200, 1700)


# --------------------------------------------------------------------------- #
# Indevolt: callbacks, notifier key, request speed.
# --------------------------------------------------------------------------- #


async def test_indevolt_persist_callback_is_the_coordinators_and_writes_the_battery_config():
    hass = MagicMock()
    coordinator = _build_coordinator(INDEVOLT, hass=hass)
    settings = coordinator.driver._settings
    assert settings._persist == coordinator.persist_battery_config
    settings._persist("min_soc", 33)
    hass.config_entries.async_update_entry.assert_called_once()
    (entry,), kwargs = hass.config_entries.async_update_entry.call_args
    assert entry is coordinator._config_entry
    assert kwargs["data"]["batteries"][0]["min_soc"] == 33


async def test_indevolt_stored_callback_reads_this_battery_config():
    coordinator = _build_coordinator({**INDEVOLT, "min_soc": 17})
    stored = coordinator.driver._settings._stored
    assert stored("min_soc") == 17
    assert stored("missing_key") is None
    coordinator._config_entry = None
    assert stored("min_soc") is None


async def test_indevolt_notifier_is_keyed_on_host_and_port_not_on_the_device_key():
    hass = MagicMock()
    coordinator = _build_coordinator({**INDEVOLT, "slave_id": 2}, hass=hass)
    notifier = coordinator.driver._notifier
    assert coordinator.device_key == "192.0.2.10_8080_2"
    assert notifier._device_key == "192.0.2.10_8080"
    assert notifier._name == "indevolt battery"
    assert notifier._hass is hass
    assert notifier._prefix.endswith("indevolt_192.0.2.10_8080_")


@pytest.mark.parametrize(
    ("speed", "slot_s"),
    [("maximum", 1.0), ("fast", 2.0), ("normal", 3.0), ("conservative", 5.0),
     ("warp", SLOT_S), (None, SLOT_S), (7, SLOT_S)],
)
async def test_indevolt_request_speed_becomes_the_slot(speed, slot_s):
    config = {**INDEVOLT}
    config.pop(CONF_INDEVOLT_REQUEST_SPEED, None)
    if speed is not None:
        config[CONF_INDEVOLT_REQUEST_SPEED] = speed
    coordinator = _build_coordinator(config)
    assert coordinator.indevolt_request_speed == speed
    assert coordinator.driver._slot_s == slot_s


async def test_indevolt_capabilities_and_forced_off_taper():
    coordinator = _build_coordinator(INDEVOLT)
    capabilities = coordinator.driver.capabilities
    assert capabilities.hardware_soc_cutoff is False
    assert capabilities.has_force_mode is False
    assert capabilities.has_alarm_registers is False
    assert coordinator.full_charge_voltage_taper_enabled is False
