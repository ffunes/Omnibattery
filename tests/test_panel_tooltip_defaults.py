"""Every setting tooltip ends with its default, in all six panel languages.

A setting's tooltip in SYS_HELP ends with one sentence stating its default,
"Default: …" with the label of each language. The value is checked against the code's DEFAULT_*
constant where there is one, so a changed default cannot drift from its
tooltip. Status rows and buttons get no such sentence.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from custom_components.omnibattery.const import (
    DEFAULT_CAPACITY_PROTECTION_SOC,
    DEFAULT_CHARGE_DELAY_BALANCE_DEADBAND_KWH,
    DEFAULT_DELAY_SAFETY_MARGIN_MIN,
    DEFAULT_DELAY_SOC_SETPOINT,
    DEFAULT_HOURLY_BALANCE_DEADBAND_WH,
    DEFAULT_HOURLY_BALANCE_HYSTERESIS_W,
    DEFAULT_HOURLY_BALANCE_MAX_OFFSET_W,
    DEFAULT_HOURLY_BALANCE_TARGET_NET_WH,
    DEFAULT_NO_PD_COMMAND_DELAY,
    DEFAULT_PD_DEADBAND,
    DEFAULT_PD_DIRECTION_HYSTERESIS,
    DEFAULT_PD_KD,
    DEFAULT_PD_KP,
    DEFAULT_PD_MAX_POWER_CHANGE,
    DEFAULT_PD_MIN_CHARGE_POWER,
    DEFAULT_PD_MIN_CYCLE_INTERVAL,
    DEFAULT_PD_MIN_DISCHARGE_POWER,
    DEFAULT_PD_RELAY_COOLDOWN,
    DEFAULT_SYSTEM_MAX_CHARGE_POWER,
    DEFAULT_SYSTEM_MAX_DISCHARGE_POWER,
    DEFAULT_TARGET_GRID_POWER,
    DEFAULT_TEMP_CHARGE_LIMIT_BAND_C,
    DEFAULT_TEMP_CHARGE_LIMIT_C,
    DEFAULT_TEMP_CHARGE_LIMIT_FLOOR_PCT,
    DEFAULT_NEGATIVE_INJECTION_THRESHOLD,
    DEFAULT_PREDICTIVE_MIN_SOC_FLOOR,
    DEFAULT_PREDICTIVE_SAFETY_MARGIN_PCT,
    DEFAULT_PREDISCHARGE_RESERVE_SOC,
    DEFAULT_SURPLUS_HOLD_MIN_SAVING,
)

PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")
LANGS = ["en", "es", "ca", "de", "fr", "nl"]
LABEL = {
    "en": "Default:",
    "es": "Por defecto:",
    "ca": "Per defecte:",
    "de": "Standard:",
    "fr": "Par défaut :",
    "nl": "Standaard:",
}


def _per_kwh(value):
    # The panel puts the slider's unit there (€/kWh or CHF/kWh).
    return f"{value:.2f} {{unit}}"


def _pct(value):
    return f"{value:g} %"


def _unit(value, unit, off=False):
    text = f"{value:g} {unit}".strip()
    return f"{text} (off)" if off else text


# key -> the English default sentence's value. Numbers come from the code.
DEFAULTS = {
    # predictive charging card
    "predictive_charging": "off",
    "predictive_charging_notifications": "on",
    "predictive_safety_margin_pct": _pct(DEFAULT_PREDICTIVE_SAFETY_MARGIN_PCT),
    "min_soc_floor_enabled": "off",
    "predictive_min_soc_floor": _pct(DEFAULT_PREDICTIVE_MIN_SOC_FLOOR),
    "max_price_threshold_enabled": "off (automatic)",
    "max_price_threshold": "not set (automatic)",
    "min_arbitrage_margin": "0 (losses only)",
    "round_trip_efficiency": "measured (0.85 until a battery has 50 cycles)",
    "negative_price_charging": "off",
    "price_discharge_control": "off",
    "discharge_price_threshold_enabled": "off (automatic)",
    "discharge_price_threshold": "not set (automatic)",
    "high_price_sale": "off",
    "surplus_price_hold": "off",
    "surplus_hold_min_saving": _per_kwh(DEFAULT_SURPLUS_HOLD_MIN_SAVING),
    "smart_predischarge": "off",
    "negative_injection_threshold": _per_kwh(DEFAULT_NEGATIVE_INJECTION_THRESHOLD),
    "predischarge_reserve_soc": _pct(DEFAULT_PREDISCHARGE_RESERVE_SOC),
    # weekly full charge card
    "weekly_full_charge_enabled": "off",
    "weekly_full_charge_day": "Sunday",
    "weekly_full_charge_delay": "off",
    # main switches of the other cards
    "manual_mode": "off",
    "offgrid_mode": "off",
    "no_pd_mode": "off",
    "charge_delay": "off",
    "hourly_balance": "off",
    "system_power_limits": "off",
    "capacity_protection": "off",
    "temp_charge_limit": "off",
    # control loop, three-phase, vacation, contracted power
    "three_phase_protection": "off",
    "vacation_mode": "off",
    "pd_controller_deadband": _unit(DEFAULT_PD_DEADBAND, "W"),
    "pd_min_charge_power": _unit(DEFAULT_PD_MIN_CHARGE_POWER, "W", off=True),
    "pd_min_discharge_power": _unit(DEFAULT_PD_MIN_DISCHARGE_POWER, "W", off=True),
    "pd_relay_cooldown": _unit(DEFAULT_PD_RELAY_COOLDOWN, "s", off=True),
    "pd_target_grid_power": _unit(DEFAULT_TARGET_GRID_POWER, "W"),
    "max_contracted_power": "7000 W",  # the number's own default (CONFIG_NUMBER_DEFINITIONS)
    "pd_tuning_profile": "Balanced",  # untouched Kp/Kd/max change are the Balanced preset
    "pd_controller_kp": _unit(DEFAULT_PD_KP, ""),
    "pd_controller_kd": _unit(DEFAULT_PD_KD, ""),
    "pd_controller_max_power_change": _unit(DEFAULT_PD_MAX_POWER_CHANGE, "W"),
    "pd_controller_direction_hysteresis": _unit(DEFAULT_PD_DIRECTION_HYSTERESIS, "W"),
    "pd_min_cycle_interval": _unit(DEFAULT_PD_MIN_CYCLE_INTERVAL, "s"),
    "no_pd_command_delay": _unit(DEFAULT_NO_PD_COMMAND_DELAY, "s"),
    "charge_priority": "automatic",
    "primary_battery": "automatic",
    "primary_feedforward": "off",
    # charge delay
    "delay_safety_margin_min": _unit(DEFAULT_DELAY_SAFETY_MARGIN_MIN / 60, "h"),
    "charge_delay_balance_deadband_kwh": _unit(DEFAULT_CHARGE_DELAY_BALANCE_DEADBAND_KWH, "kWh"),
    "delay_soc_setpoint_enabled": "off",
    "delay_soc_setpoint": _pct(DEFAULT_DELAY_SOC_SETPOINT),
    # hourly balance, system limits, peak shaving
    "hourly_balance_target_net_wh": _unit(DEFAULT_HOURLY_BALANCE_TARGET_NET_WH, "kWh"),
    "hourly_balance_max_offset_w": _unit(DEFAULT_HOURLY_BALANCE_MAX_OFFSET_W, "W"),
    "hourly_balance_deadband_wh": _unit(DEFAULT_HOURLY_BALANCE_DEADBAND_WH, "kWh"),
    "hourly_balance_hysteresis_w": _unit(DEFAULT_HOURLY_BALANCE_HYSTERESIS_W, "W"),
    "system_max_charge_power": _unit(DEFAULT_SYSTEM_MAX_CHARGE_POWER, "W", off=True),
    "system_max_discharge_power": _unit(DEFAULT_SYSTEM_MAX_DISCHARGE_POWER, "W", off=True),
    "capacity_protection_excluded_devices": "off",
    "grid_import_window_15min": "off (on in Belgium)",
    "capacity_protection_soc_threshold": _pct(DEFAULT_CAPACITY_PROTECTION_SOC),
    # temperature charge limit
    "temp_charge_limit_c": _unit(DEFAULT_TEMP_CHARGE_LIMIT_C, "°C"),
    "temp_charge_limit_band_c": _unit(DEFAULT_TEMP_CHARGE_LIMIT_BAND_C, "°C"),
    "temp_charge_limit_floor_pct": _pct(DEFAULT_TEMP_CHARGE_LIMIT_FLOOR_PCT),
    "temp_charge_limit_discharge": "off",
    # excluded devices (per device)
    "excluded_device_enabled": "on",
    "excluded_device_solar_surplus": "off",
    "excluded_device_dynamic_power_control": "off",
    "excluded_device_cover_home": "off",
    "excluded_device_exclusion_pct": "100 %",
}

NO_DEFAULT = (
    "curtailment_status",
    "surplus_price_hold_status",
    "high_price_discharge_status",
    "discharge_reserve_status",
    "excluded_device_status",
    "predictive_charging_active",
    "weekly_full_charge",
    "reevaluate_dynamic_pricing",
    "system_pd_control_quality",
    "temperature_charge_limit_status",
)


def _help(lang):
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index("const SYS_HELP = {")
    end = panel.index("\n};", start)
    blocks = [panel.index(f"\n  {code}: {{", start, end) for code in LANGS]
    i = LANGS.index(lang)
    block = panel[blocks[i] : blocks[i + 1] if i + 1 < len(LANGS) else end]
    entries = {}
    for m in re.finditer(r'\n\s+(\w+): ("(?:[^"\\]|\\.)*")', block):
        entries[m.group(1)] = json.loads(m.group(2))
    return entries


@pytest.mark.parametrize("key", sorted(DEFAULTS))
def test_the_english_tooltip_states_the_code_s_default(key):
    assert _help("en")[key].endswith(f"Default: {DEFAULTS[key]}.")


@pytest.mark.parametrize("lang", LANGS)
def test_every_language_ends_with_one_default_sentence(lang):
    entries = _help(lang)
    for key in DEFAULTS:
        text = entries[key]
        tail = text.rsplit(". ", 1)[-1]
        assert tail.startswith(LABEL[lang]), (lang, key, tail)
        assert text.count(LABEL[lang]) == 1, (lang, key)


def test_every_setting_row_of_the_panel_is_covered():
    # A new setting row must come with its default sentence.
    panel = PANEL.read_text(encoding="utf-8")
    block = panel[panel.index("const SYS_SECTIONS = [") : panel.index("const SYS_HELP = {")]
    keys = set(re.findall(r'\{ key: "(\w+)"', block))
    assert keys - set(DEFAULTS) - set(NO_DEFAULT) == set()


@pytest.mark.parametrize("lang", LANGS)
def test_status_rows_and_buttons_state_no_default(lang):
    entries = _help(lang)
    for key in NO_DEFAULT:
        assert LABEL[lang] not in entries[key], (lang, key)


def test_a_settings_button_shows_its_tooltip():
    # A button row has no label column, so the help has to reach the button
    # itself: hover title, plus an info mark for touch.
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index("_buildSysControl(item, id, store, multi) {")
    branch = panel[panel.index('if (domain === "button") {', start) : panel.index("return frag;", start)]
    assert "this._help(item.key)" in branch
    assert "btn.title = help" in branch
    assert "_showInfoPopover(info, help)" in branch
    assert _help("de")["reevaluate_dynamic_pricing"]


def test_a_per_kwh_default_shows_the_slider_s_unit():
    # The tooltip and the slider name the same unit: "Default: 0.05 €/kWh".
    panel = PANEL.read_text(encoding="utf-8")
    start = panel.index("_help(key, unit) {")
    body = panel[start : panel.index("\n  }", start)]
    assert '.replace("{unit}", unit || "/kWh")' in body
    row = panel[panel.index("// static options-flow help (desktop hover title") :]
    assert "this._help(item.key, state && state.attributes && state.attributes.unit_of_measurement)" in row[:400]
    for lang in LANGS:
        for key in ("surplus_hold_min_saving", "negative_injection_threshold"):
            text = _help(lang)[key]
            assert text.endswith(" {unit}."), (lang, key)
            assert "kWh." not in text.rsplit(LABEL[lang], 1)[-1], (lang, key)


def test_an_inverted_row_does_not_show_its_key_s_tooltip():
    # "Use PD controller" is no_pd_mode shown inverted: the key's "On: No-PD..."
    # would say the opposite of the switch.
    panel = PANEL.read_text(encoding="utf-8")
    assert '{ key: "no_pd_mode", domain: "switch", lk: "itemPdEnable", icon: "mdi:tune", gate: true, gateInvert: true }' in panel
    row = panel[panel.index("// static options-flow help (desktop hover title") :][:400]
    assert 'item.gateInvert ? "" : this._help(item.key' in row
