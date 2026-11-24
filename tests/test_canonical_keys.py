"""T7.9: canonical key constants, ``canonical_key``, pack-fill and balance traits, D6 texts."""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import custom_components.omnibattery as package
from custom_components.omnibattery.drivers import base
from custom_components.omnibattery.drivers.base import (
    DEFAULT_BALANCE_BASELINE,
    BalanceBaseline,
    canonical_key,
)
from custom_components.omnibattery.drivers.zendure import NUMBER_DEFINITIONS as ZENDURE_NUMBERS
from custom_components.omnibattery.drivers.registry import traits_for_brand
from custom_components.omnibattery.switch import ManualModeSwitch
from custom_components.omnibattery.tracking.balance_monitor import BalanceMonitor
from tests.test_cell_voltage_profile_characterization import (  # noqa: F401 (autouse stub)
    CASES,
    IDS,
    _coordinator,
    _no_ha_runtime,
)
from tests.test_driver_construction_characterization import BRAND_CONFIGS

PACKAGE_DIR = Path(package.__file__).parent

# ---------------------------------------------------------------------------
# #34 canonical key constants
# ---------------------------------------------------------------------------

CONSTANTS = {
    "KEY_RS485_CONTROL_MODE": "rs485_control_mode",
    "KEY_FORCE_MODE": "force_mode",
    "KEY_SET_CHARGE_POWER": "set_charge_power",
    "KEY_SET_DISCHARGE_POWER": "set_discharge_power",
    "KEY_MAX_CHARGE_POWER": "max_charge_power",
    "KEY_MAX_DISCHARGE_POWER": "max_discharge_power",
    "KEY_CHARGING_CUTOFF_CAPACITY": "charging_cutoff_capacity",
    "KEY_DISCHARGING_CUTOFF_CAPACITY": "discharging_cutoff_capacity",
    "KEY_BATTERY_SOC": "battery_soc",
    "KEY_TOTAL_DISCHARGING_ENERGY": "total_discharging_energy",
}


@pytest.mark.parametrize(("name", "value"), CONSTANTS.items())
def test_constant_keeps_the_entity_key(name, value):
    assert getattr(base, name) == value


@pytest.mark.parametrize("platform", ["number", "switch", "select", "sensor"])
def test_platforms_name_no_canonical_key_literal(platform):
    """The platforms compare keys through the constants, never the string."""
    text = (PACKAGE_DIR / f"{platform}.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        if re.search(r"""(\bkey\b|\["key"\]|\['key'\])\s*(==|!=|in)\s*[(\[{]?\s*["']""", code):
            for value in [*CONSTANTS.values(), "inverse_max_power"]:
                assert f'"{value}"' not in code and f"'{value}'" not in code, line


# ---------------------------------------------------------------------------
# #23 canonical_key
# ---------------------------------------------------------------------------


def test_canonical_key_defaults_to_the_entity_key():
    assert canonical_key({"key": "soc_set"}) == "soc_set"
    assert canonical_key({"key": "inverse_max_power", "canonical_key": "max_discharge_power"}) == (
        "max_discharge_power"
    )


def test_only_the_zendure_discharge_limit_declares_an_alias():
    aliased = [d for d in ZENDURE_NUMBERS if "canonical_key" in d]
    assert [(d["key"], d["canonical_key"]) for d in aliased] == [
        ("inverse_max_power", "max_discharge_power")
    ]


@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_no_other_definition_declares_an_alias(brand, version):
    coord = _coordinator(brand, version, 3.3)
    for definition in coord.driver.all_definitions:
        if "canonical_key" in definition:
            assert (brand, definition["key"]) == ("zendure", "inverse_max_power")


async def test_coordinator_ignores_a_definition_keyed_by_the_alias_name_alone():
    """The alias is the definition's field, not a key the coordinator knows."""
    from custom_components.omnibattery.infra.coordinator import (
        MarstekVenusDataUpdateCoordinator,
    )

    plain = SimpleNamespace(number_definitions=[{"key": "inverse_max_power"}])
    assert MarstekVenusDataUpdateCoordinator.needs_software_max_discharge.fget(plain) is True


# ---------------------------------------------------------------------------
# #46 sequential_pack_fill
# ---------------------------------------------------------------------------

PACK_FILL = {"vA": True, "vD": True, "v2": False, "v3": False}


@pytest.mark.parametrize("version", PACK_FILL)
def test_marstek_pack_fill_follows_the_version(version):
    traits = traits_for_brand("marstek", {**BRAND_CONFIGS["marstek"], "battery_version": version})
    assert traits.sequential_pack_fill is PACK_FILL[version]
    assert traits.sequential_pack_fill is traits.bms_owns_top_cutoff


@pytest.mark.parametrize("brand", [b for b in BRAND_CONFIGS if b != "marstek"])
def test_other_brands_do_not_fill_packs_in_sequence(brand):
    assert traits_for_brand(brand, BRAND_CONFIGS[brand]).sequential_pack_fill is False


@pytest.mark.parametrize("brand", [b for b in BRAND_CONFIGS if b != "marstek"])
def test_a_stale_version_key_is_ignored_by_other_brands(brand):
    """Fixed: plan section 8 #12 (T1.1): only Marstek reads the stored version."""
    traits = traits_for_brand(brand, {**BRAND_CONFIGS[brand], "battery_version": "vD"})
    assert traits.sequential_pack_fill is False


# ---------------------------------------------------------------------------
# #47 balance_baseline
# ---------------------------------------------------------------------------


def test_default_baseline_is_the_factory_figures():
    assert DEFAULT_BALANCE_BASELINE == BalanceBaseline(
        offset_mv=180, yellow_mv=200, orange_mv=230, red_mv=250
    )


@pytest.mark.parametrize(("brand", "version"), CASES, ids=IDS)
async def test_every_driver_declares_the_default_baseline(brand, version):
    coord = _coordinator(brand, version, 3.3)
    assert coord.capabilities.balance_baseline == DEFAULT_BALANCE_BASELINE


def test_the_monitor_reads_the_baseline_of_the_driver():
    monitor = BalanceMonitor.__new__(BalanceMonitor)
    monitor._hass = None  # texts read hass.config.language; None falls back to English
    tight = BalanceBaseline(offset_mv=0, yellow_mv=20, orange_mv=40, red_mv=60)
    assert monitor._status_for_delta(50, tight) == "orange"
    assert monitor._status_for_delta(50, DEFAULT_BALANCE_BASELINE) == "green"
    bat = {}
    issues: list[str] = []
    assert monitor._evaluate(70, bat, issues, tight) == ("red", False)
    assert issues == ["Delta: 70 mV — high cell imbalance."]


def test_no_baseline_judges_nothing():
    monitor = BalanceMonitor.__new__(BalanceMonitor)
    bat = {"consecutive_red": 3}
    issues: list[str] = []
    assert monitor._status_for_delta(900, None) == "unknown"
    assert monitor._evaluate(900, bat, issues, None) == ("unknown", False)
    assert issues == [] and bat == {"consecutive_red": 3}
    trend = {"trend": "rising", "avg_4w": 900.0, "slope": 50.0}
    monitor._check_trend_alert(trend, issues, None)
    assert issues == []


def test_capability_can_be_switched_off_per_driver():
    from custom_components.omnibattery.drivers.base import DriverCapabilities

    assert "balance_baseline" in {f for f in DriverCapabilities.__dataclass_fields__}
    assert replace(
        _bare_capabilities(), balance_baseline=None
    ).balance_baseline is None


def _bare_capabilities():
    from custom_components.omnibattery.drivers.base import DriverCapabilities

    return DriverCapabilities(
        hardware_soc_cutoff=False, has_force_mode=False, push_telemetry=False,
        max_charge_power_w=1, max_discharge_power_w=1, has_mppt_pv=False,
        has_alarm_registers=False, has_rs485_control=False,
    )


# ---------------------------------------------------------------------------
# D6 texts
# ---------------------------------------------------------------------------

BRAND_WORDS = re.compile(r"marstek|anker|zendure|esphome|sessy|indevolt|huawei|hoymiles|solix", re.I)


async def test_manual_mode_notification_names_no_brand():
    switch = ManualModeSwitch.__new__(ManualModeSwitch)
    switch.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=Mock()),
        services=SimpleNamespace(async_call=AsyncMock()),
    )
    switch.entry = SimpleNamespace(data={})
    switch.controller = SimpleNamespace(coordinators=[], manual_mode_enabled=False)
    switch.async_write_ha_state = Mock()

    await switch.async_turn_on()

    (domain, service, payload), _ = switch.hass.services.async_call.call_args
    assert (domain, service) == ("persistent_notification", "create")
    assert payload["title"] == "Manual Mode: active"
    assert payload["message"] == (
        "Automatic charge/discharge control is paused. "
        "All batteries have been set to idle (0W). "
        "You can now manually control each battery using the "
        "'Set Forcible Charge/Discharge Power' controls, or — "
        "for batteries whose own integration offers more "
        "operating modes — select another mode there without "
        "OmniBattery reverting it.\n\n"
        "Turn off Manual Mode to resume automatic control."
    )
    assert not BRAND_WORDS.search(payload["message"])


async def test_balance_alert_names_no_brand_and_keeps_its_id():
    sent, notes = [], []
    monitor = BalanceMonitor.__new__(BalanceMonitor)
    monitor._hass = SimpleNamespace()
    monitor._controller = SimpleNamespace(
        _create_entry_background_task=lambda coro, name: sent.append(coro)
    )

    async def _notify(notification_id, title, message):
        notes.append((notification_id, title, message))

    monitor._notify = _notify
    bat = {"consecutive_red": 1}
    issues: list[str] = []
    status, severe = monitor._evaluate(400, bat, issues, DEFAULT_BALANCE_BASELINE)
    assert (status, severe) == ("red", True)
    assert issues[1] == (
        "High cell imbalance (400 mV) for 2 consecutive full charges. Consider "
        "running an active-balance blueprint for this battery to rebalance the cells."
    )
    monitor._maybe_notify("192.0.2.10_502", "Battery 1", bat, issues, severe)
    await sent[0]
    notification_id, title, message = notes[0]
    # The id is stored by the user's HA: it must not change with the wording.
    assert notification_id == "marstek_venus_marstek_balance_192.0.2.10_502"
    assert not BRAND_WORDS.search(title + message)
