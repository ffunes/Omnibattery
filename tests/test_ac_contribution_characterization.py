"""Characterization of a battery's AC contribution in the shared core (T7.4, #42).

The core computes "what this battery puts on / takes from the AC bus" in five places,
each with its own preference between the three power keys a driver can publish:

* ``ac_power``                 the AC-side register (+discharge / -charge)
* ``DELIVERED_AC_POWER_KEY``   the device's own AC port (+charge / -discharge)
* ``battery_power``            cell-side power (+charge / -discharge)

The five implementations, all fed the same real coordinator and driver:

* I1 ``ChargeDischargeController._coordinator_delivered_power`` (+charge): ac_power,
  then the AC port, then the cell power.
* I2 ``ChargeDischargeController._daily_operation_runtime_decision``: calls I1, then
  reads ``ac_power`` ALONE (no fallback) to rebuild the cell power when the battery has
  MPPT or aggregate PV and to decide whether the AC side is drawing from the grid. Its
  observable result is the net cell power (+charge) and the action mask, with and
  without grid charging active.
* I3 ``control.residual_load._battery_ac_power_w`` (+discharge, normalised here to
  +charge): AC port, then ac_power, then the cell power, but the cell power is refused
  on a battery with connected MPPT PV; None when the battery is unavailable.
* I4 ``tracking.consumption_tracker.coordinator_ac_power_w`` (+discharge, normalised):
  AC port, then ac_power, then the cell power; non-finite -> None.
* I5 ``PhasePowerLimiter._battery_power`` (+charge): the controller's I1, else ac_power
  then cell power, else 0.0 (never None). ``I5b`` is the same with no controller.

Every row below is a literal observed on the pre-refactor code (518a1780); if one
changes, behaviour changed. Rows are driver variants x the combinations of the three
keys that the driver can publish (absent / None / +value / -value), minus the ones a
driver cannot produce (``reach`` is False for those: they pin defensive behaviour only).
Where the five disagree on a reachable row, the difference is kept and documented in
``DifferingRows`` below (plan section 8, T7.4).
"""
from __future__ import annotations

import inspect
import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

import custom_components.omnibattery.drivers as drivers_pkg
from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control.phase_power_limit import PhasePowerLimiter
from custom_components.omnibattery.control.residual_load import _battery_ac_power_w
from custom_components.omnibattery.tracking.consumption_tracker import (
    coordinator_ac_power_w,
)
from datetime import datetime
from zoneinfo import ZoneInfo

from tests.test_driver_construction_characterization import (
    ANKER,
    BRAND_CONFIGS,
    ESPHOME,
    HOYMILES,
    HUAWEI,
    INDEVOLT,
    MARSTEK,
    SESSY,
    ZENDURE,
    _build_coordinator,
)

ABSENT = object()
AC, DEL, BP = "ac_power", "ac_delivered_power", "battery_power"
LEVEL = {"A": ABSENT, "N": None, "+": 1, "-": -1}
SIZE = {AC: 300, DEL: 200, BP: 500}
NOW = datetime(2026, 8, 23, 11, 30, tzinfo=ZoneInfo("Europe/Madrid"))


def _value(key, level):
    sign = LEVEL[level]
    return sign if sign is ABSENT or sign is None else sign * SIZE[key]


def _label(data):
    names = {AC: "ac", DEL: "del", BP: "bp"}
    return "|".join(
        f"{names[k]}={data[k]}" if k in data else f"{names[k]}=A" for k in names
    )


def _grid(keys, *, none_ok):
    """Every combination of the keys (absent / None / +v / -v)."""
    levels = "AN+-" if none_ok else "A+-"
    out = []
    for combo in itertools.product(levels, repeat=len(keys)):
        data = {}
        for key, level in zip(keys, combo):
            value = _value(key, level)
            if value is not ABSENT:
                data[key] = value
        out.append(data)
    return out


# --------------------------------------------------------------------------- #
# Variants: (config, extra coordinator setup, extra telemetry, rows, reach rule)
# --------------------------------------------------------------------------- #

MARSTEK_VA = {**MARSTEK, "battery_version": "vA", "dc_pv_connected": True}
MARSTEK_VA_NODC = {**MARSTEK, "battery_version": "vA", "dc_pv_connected": False}
ZENDURE_MIX = {**ZENDURE, "zendure_model": "4000mix_pro", "dc_pv_connected": True}


def _anker_ac_rows():
    rows = [{}]
    for bp in (500, -500):
        rows.append({BP: bp, AC: -bp})  # the driver writes ac_power = -battery_power
    return rows


def _anker_pv_rows():
    """Independent-PV SKU: ac_power = -pack, delivered = pack - pv (pv = 1000)."""
    rows = [{}]
    rows.append({BP: 800, AC: -800})  # pv unknown: no AC-port key
    for bp in (800, -500, 1500):
        rows.append({BP: bp, AC: -bp, DEL: bp - 1000})
    return rows


def _indevolt_rows():
    rows = []
    for delivered in (ABSENT, 200, -200):
        for bp in (ABSENT, 500, -500):
            data = {}
            if delivered is not ABSENT:
                data[DEL] = delivered
                data[AC] = -delivered  # core convention: + discharging
            if bp is not ABSENT:
                data[BP] = bp
            rows.append(data)
    return rows


def _zendure_reach(data):
    """battery_power is always synthesised; delivered is a number or None."""
    if not data:
        return True
    return isinstance(data.get(BP), (int, float)) and DEL in data


def _none_free(data):
    return all(v is not None for v in data.values())


VARIANTS = {
    "marstek-v3": dict(
        config=MARSTEK, extra={}, rows=_grid([AC, BP], none_ok=True), reach=_none_free,
    ),
    "marstek-vA-mppt": dict(
        config=MARSTEK_VA, extra={"mppt1_power": 400},
        rows=_grid([AC, BP], none_ok=True), reach=_none_free,
    ),
    "marstek-vA-no-dc-pv": dict(
        config=MARSTEK_VA_NODC, extra={"mppt1_power": 400},
        rows=_grid([AC, BP], none_ok=True), reach=_none_free,
    ),
    "esphome": dict(
        config=ESPHOME, extra={}, rows=_grid([AC, BP], none_ok=True), reach=_none_free,
    ),
    "zendure-2400ac_pro": dict(
        config=ZENDURE, extra={}, rows=_grid([DEL, BP], none_ok=True), reach=_zendure_reach,
    ),
    "zendure-4000mix_pro": dict(
        config=ZENDURE_MIX, extra={"mppt1_power": 400},
        rows=_grid([DEL, BP], none_ok=True), reach=_zendure_reach,
    ),
    "anker-ac-family": dict(
        config=ANKER, extra={}, rows=_anker_ac_rows(), reach=lambda d: True,
    ),
    "anker-independent-pv": dict(
        config=ANKER, extra={"solar_power": 1000}, rows=_anker_pv_rows(),
        reach=lambda d: True, product_code="DN7M",
    ),
    "sessy": dict(
        config=SESSY, extra={}, rows=_grid([BP], none_ok=True), reach=lambda d: True,
    ),
    "huawei": dict(
        config=HUAWEI, extra={}, rows=_grid([BP], none_ok=True), reach=_none_free,
    ),
    "indevolt": dict(
        config=INDEVOLT, extra={"solar_power": 300}, rows=_indevolt_rows(),
        reach=lambda d: True,
    ),
    "hoymiles": dict(
        config=HOYMILES, extra={}, rows=_grid([DEL, BP], none_ok=True), reach=_none_free,
    ),
}


# --------------------------------------------------------------------------- #
# The five implementations, driven through the real coordinator and driver.
# --------------------------------------------------------------------------- #

def _n(value):
    if value is None or isinstance(value, str):
        return value
    return round(float(value), 3) + 0.0  # also folds -0.0 into 0.0


def _decision_controller(coordinator, *, grid_active):
    controller = SimpleNamespace(
        coordinators=[coordinator],
        _consumption_tracker=None,
        grid_charging_active=grid_active,
        predictive_charging_enabled=False,
        charge_delay_enabled=False,
        previous_power=0.0,
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _coordinator_delivered_power=ChargeDischargeController._coordinator_delivered_power,
        _is_battery_manual_owned=lambda _coordinator: False,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Disabled"},
    )
    controller._daily_operation_delay_active = lambda: (
        ChargeDischargeController._daily_operation_delay_active(controller)
    )
    return controller


def _guard(call):
    try:
        return call()
    except Exception as err:  # noqa: BLE001 - the exception type is the characterization
        return f"raises:{type(err).__name__}"


def _decision(coordinator, grid_active):
    return ChargeDischargeController._daily_operation_runtime_decision(
        _decision_controller(coordinator, grid_active=grid_active), NOW
    )


def compute(coordinator):
    """The five results for the coordinator's current data: charge-positive watts."""
    i1 = _guard(lambda: ChargeDischargeController._coordinator_delivered_power(coordinator))
    plain = _guard(lambda: _decision(coordinator, False))
    grid = _guard(lambda: _decision(coordinator, True))
    if isinstance(plain, str):
        i2 = (plain, None)
    else:
        net = (
            round(plain["charge_power_w"] - plain["discharge_power_w"], 3) + 0.0
            if plain["source"] == "runtime_measured"
            else None
        )
        i2 = (net, plain["action_mask"], grid["action_mask"])
    i3 = _guard(lambda: _battery_ac_power_w(coordinator))
    i4 = _guard(lambda: coordinator_ac_power_w(coordinator))
    if not isinstance(i3, str) and i3 is not None:
        i3 = -i3
    if not isinstance(i4, str) and i4 is not None:
        i4 = -i4
    with_controller = SimpleNamespace(
        controller=SimpleNamespace(
            _coordinator_delivered_power=ChargeDischargeController._coordinator_delivered_power
        )
    )
    i5 = _guard(lambda: PhasePowerLimiter._battery_power(with_controller, coordinator))
    i5b = _guard(
        lambda: PhasePowerLimiter._battery_power(SimpleNamespace(controller=None), coordinator)
    )
    return (_n(i1), i2, _n(i3), _n(i4), _n(i5), _n(i5b))


def _coordinator(variant):
    spec = VARIANTS[variant]
    coordinator = _build_coordinator(spec["config"])
    coordinator._is_connected = True
    coordinator._is_shutting_down = False
    if "product_code" in spec:
        coordinator.driver._product_code = spec["product_code"]
    return coordinator


# --------------------------------------------------------------------------- #
# GOLDEN: filled in from the pre-refactor code (see module docstring).
# key: (variant, row label) -> (reach, (I1, I2, I3, I4, I5, I5b))
#      I2 = (net cell power, action mask, action mask with grid charging active)
# --------------------------------------------------------------------------- #
GOLDEN: dict = {
    ('marstek-v3', 'ac=A|del=A|bp=A'): (True, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('marstek-v3', 'ac=A|del=A|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('marstek-v3', 'ac=A|del=A|bp=500'): (True, (500.0, (500.0, 2, 2), 500.0, 500.0, 500.0, 500.0)),
    ('marstek-v3', 'ac=A|del=A|bp=-500'): (True, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('marstek-v3', 'ac=None|del=A|bp=A'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('marstek-v3', 'ac=None|del=A|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('marstek-v3', 'ac=None|del=A|bp=500'): (False, (500.0, (500.0, 2, 2), 500.0, 500.0, 500.0, 500.0)),
    ('marstek-v3', 'ac=None|del=A|bp=-500'): (False, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('marstek-v3', 'ac=300|del=A|bp=A'): (True, (-300.0, (-300.0, 4, 4), -300.0, -300.0, -300.0, -300.0)),
    ('marstek-v3', 'ac=300|del=A|bp=None'): (False, (-300.0, (-300.0, 4, 4), -300.0, -300.0, -300.0, -300.0)),
    ('marstek-v3', 'ac=300|del=A|bp=500'): (True, (-300.0, (-300.0, 4, 4), -300.0, -300.0, -300.0, -300.0)),
    ('marstek-v3', 'ac=300|del=A|bp=-500'): (True, (-300.0, (-300.0, 4, 4), -300.0, -300.0, -300.0, -300.0)),
    ('marstek-v3', 'ac=-300|del=A|bp=A'): (True, (300.0, (300.0, 2, 2), 300.0, 300.0, 300.0, 300.0)),
    ('marstek-v3', 'ac=-300|del=A|bp=None'): (False, (300.0, (300.0, 2, 2), 300.0, 300.0, 300.0, 300.0)),
    ('marstek-v3', 'ac=-300|del=A|bp=500'): (True, (300.0, (300.0, 2, 2), 300.0, 300.0, 300.0, 300.0)),
    ('marstek-v3', 'ac=-300|del=A|bp=-500'): (True, (300.0, (300.0, 2, 2), 300.0, 300.0, 300.0, 300.0)),
    ('marstek-vA-mppt', 'ac=A|del=A|bp=A'): (True, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('marstek-vA-mppt', 'ac=A|del=A|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('marstek-vA-mppt', 'ac=A|del=A|bp=500'): (True, (500.0, (500.0, 1, 3), None, 500.0, 500.0, 500.0)),
    ('marstek-vA-mppt', 'ac=A|del=A|bp=-500'): (True, (-500.0, (-500.0, 4, 4), None, -500.0, -500.0, -500.0)),
    ('marstek-vA-mppt', 'ac=None|del=A|bp=A'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('marstek-vA-mppt', 'ac=None|del=A|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('marstek-vA-mppt', 'ac=None|del=A|bp=500'): (False, (500.0, (500.0, 1, 3), None, 500.0, 500.0, 500.0)),
    ('marstek-vA-mppt', 'ac=None|del=A|bp=-500'): (False, (-500.0, (-500.0, 4, 4), None, -500.0, -500.0, -500.0)),
    ('marstek-vA-mppt', 'ac=300|del=A|bp=A'): (True, (-300.0, (100.0, 1, 1), -300.0, -300.0, -300.0, -300.0)),
    ('marstek-vA-mppt', 'ac=300|del=A|bp=None'): (False, (-300.0, (100.0, 1, 1), -300.0, -300.0, -300.0, -300.0)),
    ('marstek-vA-mppt', 'ac=300|del=A|bp=500'): (True, (-300.0, (100.0, 1, 1), -300.0, -300.0, -300.0, -300.0)),
    ('marstek-vA-mppt', 'ac=300|del=A|bp=-500'): (True, (-300.0, (100.0, 1, 1), -300.0, -300.0, -300.0, -300.0)),
    ('marstek-vA-mppt', 'ac=-300|del=A|bp=A'): (True, (300.0, (700.0, 1, 3), 300.0, 300.0, 300.0, 300.0)),
    ('marstek-vA-mppt', 'ac=-300|del=A|bp=None'): (False, (300.0, (700.0, 1, 3), 300.0, 300.0, 300.0, 300.0)),
    ('marstek-vA-mppt', 'ac=-300|del=A|bp=500'): (True, (300.0, (700.0, 1, 3), 300.0, 300.0, 300.0, 300.0)),
    ('marstek-vA-mppt', 'ac=-300|del=A|bp=-500'): (True, (300.0, (700.0, 1, 3), 300.0, 300.0, 300.0, 300.0)),
    ('marstek-vA-no-dc-pv', 'ac=A|del=A|bp=A'): (True, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('marstek-vA-no-dc-pv', 'ac=A|del=A|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('marstek-vA-no-dc-pv', 'ac=A|del=A|bp=500'): (True, (500.0, (500.0, 2, 2), 500.0, 500.0, 500.0, 500.0)),
    ('marstek-vA-no-dc-pv', 'ac=A|del=A|bp=-500'): (True, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('marstek-vA-no-dc-pv', 'ac=None|del=A|bp=A'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('marstek-vA-no-dc-pv', 'ac=None|del=A|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('marstek-vA-no-dc-pv', 'ac=None|del=A|bp=500'): (False, (500.0, (500.0, 2, 2), 500.0, 500.0, 500.0, 500.0)),
    ('marstek-vA-no-dc-pv', 'ac=None|del=A|bp=-500'): (False, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('marstek-vA-no-dc-pv', 'ac=300|del=A|bp=A'): (True, (-300.0, (-300.0, 4, 4), -300.0, -300.0, -300.0, -300.0)),
    ('marstek-vA-no-dc-pv', 'ac=300|del=A|bp=None'): (False, (-300.0, (-300.0, 4, 4), -300.0, -300.0, -300.0, -300.0)),
    ('marstek-vA-no-dc-pv', 'ac=300|del=A|bp=500'): (True, (-300.0, (-300.0, 4, 4), -300.0, -300.0, -300.0, -300.0)),
    ('marstek-vA-no-dc-pv', 'ac=300|del=A|bp=-500'): (True, (-300.0, (-300.0, 4, 4), -300.0, -300.0, -300.0, -300.0)),
    ('marstek-vA-no-dc-pv', 'ac=-300|del=A|bp=A'): (True, (300.0, (300.0, 2, 2), 300.0, 300.0, 300.0, 300.0)),
    ('marstek-vA-no-dc-pv', 'ac=-300|del=A|bp=None'): (False, (300.0, (300.0, 2, 2), 300.0, 300.0, 300.0, 300.0)),
    ('marstek-vA-no-dc-pv', 'ac=-300|del=A|bp=500'): (True, (300.0, (300.0, 2, 2), 300.0, 300.0, 300.0, 300.0)),
    ('marstek-vA-no-dc-pv', 'ac=-300|del=A|bp=-500'): (True, (300.0, (300.0, 2, 2), 300.0, 300.0, 300.0, 300.0)),
    ('esphome', 'ac=A|del=A|bp=A'): (True, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('esphome', 'ac=A|del=A|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('esphome', 'ac=A|del=A|bp=500'): (True, (500.0, (500.0, 2, 2), 500.0, 500.0, 500.0, 500.0)),
    ('esphome', 'ac=A|del=A|bp=-500'): (True, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('esphome', 'ac=None|del=A|bp=A'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('esphome', 'ac=None|del=A|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('esphome', 'ac=None|del=A|bp=500'): (False, (500.0, (500.0, 2, 2), 500.0, 500.0, 500.0, 500.0)),
    ('esphome', 'ac=None|del=A|bp=-500'): (False, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('esphome', 'ac=300|del=A|bp=A'): (True, (-300.0, (-300.0, 4, 4), -300.0, -300.0, -300.0, -300.0)),
    ('esphome', 'ac=300|del=A|bp=None'): (False, (-300.0, (-300.0, 4, 4), -300.0, -300.0, -300.0, -300.0)),
    ('esphome', 'ac=300|del=A|bp=500'): (True, (-300.0, (-300.0, 4, 4), -300.0, -300.0, -300.0, -300.0)),
    ('esphome', 'ac=300|del=A|bp=-500'): (True, (-300.0, (-300.0, 4, 4), -300.0, -300.0, -300.0, -300.0)),
    ('esphome', 'ac=-300|del=A|bp=A'): (True, (300.0, (300.0, 2, 2), 300.0, 300.0, 300.0, 300.0)),
    ('esphome', 'ac=-300|del=A|bp=None'): (False, (300.0, (300.0, 2, 2), 300.0, 300.0, 300.0, 300.0)),
    ('esphome', 'ac=-300|del=A|bp=500'): (True, (300.0, (300.0, 2, 2), 300.0, 300.0, 300.0, 300.0)),
    ('esphome', 'ac=-300|del=A|bp=-500'): (True, (300.0, (300.0, 2, 2), 300.0, 300.0, 300.0, 300.0)),
    ('zendure-2400ac_pro', 'ac=A|del=A|bp=A'): (True, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('zendure-2400ac_pro', 'ac=A|del=A|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('zendure-2400ac_pro', 'ac=A|del=A|bp=500'): (False, (500.0, (500.0, 2, 2), 500.0, 500.0, 500.0, 500.0)),
    ('zendure-2400ac_pro', 'ac=A|del=A|bp=-500'): (False, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('zendure-2400ac_pro', 'ac=A|del=None|bp=A'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('zendure-2400ac_pro', 'ac=A|del=None|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('zendure-2400ac_pro', 'ac=A|del=None|bp=500'): (True, (500.0, (500.0, 2, 2), 500.0, 500.0, 500.0, 500.0)),
    ('zendure-2400ac_pro', 'ac=A|del=None|bp=-500'): (True, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('zendure-2400ac_pro', 'ac=A|del=200|bp=A'): (False, (200.0, (200.0, 2, 2), 200.0, 200.0, 200.0, 0.0)),
    ('zendure-2400ac_pro', 'ac=A|del=200|bp=None'): (False, (200.0, (200.0, 2, 2), 200.0, 200.0, 200.0, 0.0)),
    ('zendure-2400ac_pro', 'ac=A|del=200|bp=500'): (True, (200.0, (200.0, 2, 2), 200.0, 200.0, 200.0, 500.0)),
    ('zendure-2400ac_pro', 'ac=A|del=200|bp=-500'): (True, (200.0, (200.0, 2, 2), 200.0, 200.0, 200.0, -500.0)),
    ('zendure-2400ac_pro', 'ac=A|del=-200|bp=A'): (False, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, 0.0)),
    ('zendure-2400ac_pro', 'ac=A|del=-200|bp=None'): (False, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, 0.0)),
    ('zendure-2400ac_pro', 'ac=A|del=-200|bp=500'): (True, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, 500.0)),
    ('zendure-2400ac_pro', 'ac=A|del=-200|bp=-500'): (True, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, -500.0)),
    ('zendure-4000mix_pro', 'ac=A|del=A|bp=A'): (True, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('zendure-4000mix_pro', 'ac=A|del=A|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('zendure-4000mix_pro', 'ac=A|del=A|bp=500'): (False, (500.0, (500.0, 1, 3), None, 500.0, 500.0, 500.0)),
    ('zendure-4000mix_pro', 'ac=A|del=A|bp=-500'): (False, (-500.0, (-500.0, 4, 4), None, -500.0, -500.0, -500.0)),
    ('zendure-4000mix_pro', 'ac=A|del=None|bp=A'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('zendure-4000mix_pro', 'ac=A|del=None|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('zendure-4000mix_pro', 'ac=A|del=None|bp=500'): (True, (500.0, (500.0, 1, 3), None, 500.0, 500.0, 500.0)),
    ('zendure-4000mix_pro', 'ac=A|del=None|bp=-500'): (True, (-500.0, (-500.0, 4, 4), None, -500.0, -500.0, -500.0)),
    ('zendure-4000mix_pro', 'ac=A|del=200|bp=A'): (False, (200.0, (200.0, 1, 3), 200.0, 200.0, 200.0, 0.0)),
    ('zendure-4000mix_pro', 'ac=A|del=200|bp=None'): (False, (200.0, (200.0, 1, 3), 200.0, 200.0, 200.0, 0.0)),
    ('zendure-4000mix_pro', 'ac=A|del=200|bp=500'): (True, (200.0, (200.0, 1, 3), 200.0, 200.0, 200.0, 500.0)),
    ('zendure-4000mix_pro', 'ac=A|del=200|bp=-500'): (True, (200.0, (200.0, 1, 3), 200.0, 200.0, 200.0, -500.0)),
    ('zendure-4000mix_pro', 'ac=A|del=-200|bp=A'): (False, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, 0.0)),
    ('zendure-4000mix_pro', 'ac=A|del=-200|bp=None'): (False, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, 0.0)),
    ('zendure-4000mix_pro', 'ac=A|del=-200|bp=500'): (True, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, 500.0)),
    ('zendure-4000mix_pro', 'ac=A|del=-200|bp=-500'): (True, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, -500.0)),
    ('anker-ac-family', 'ac=A|del=A|bp=A'): (True, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('anker-ac-family', 'ac=-500|del=A|bp=500'): (True, (500.0, (500.0, 2, 2), 500.0, 500.0, 500.0, 500.0)),
    ('anker-ac-family', 'ac=500|del=A|bp=-500'): (True, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('anker-independent-pv', 'ac=A|del=A|bp=A'): (True, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('anker-independent-pv', 'ac=-800|del=A|bp=800'): (True, (800.0, (800.0, 1, 3), 800.0, 800.0, 800.0, 800.0)),
    ('anker-independent-pv', 'ac=-800|del=-200|bp=800'): (True, (800.0, (800.0, 1, 3), -200.0, -200.0, 800.0, 800.0)),
    ('anker-independent-pv', 'ac=500|del=-1500|bp=-500'): (True, (-500.0, (-500.0, 4, 4), -1500.0, -1500.0, -500.0, -500.0)),
    ('anker-independent-pv', 'ac=-1500|del=500|bp=1500'): (True, (1500.0, (1500.0, 1, 3), 500.0, 500.0, 1500.0, 1500.0)),
    ('sessy', 'ac=A|del=A|bp=A'): (True, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('sessy', 'ac=A|del=A|bp=None'): (True, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('sessy', 'ac=A|del=A|bp=500'): (True, (500.0, (500.0, 2, 2), 500.0, 500.0, 500.0, 500.0)),
    ('sessy', 'ac=A|del=A|bp=-500'): (True, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('huawei', 'ac=A|del=A|bp=A'): (True, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('huawei', 'ac=A|del=A|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('huawei', 'ac=A|del=A|bp=500'): (True, (500.0, (500.0, 2, 2), 500.0, 500.0, 500.0, 500.0)),
    ('huawei', 'ac=A|del=A|bp=-500'): (True, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('indevolt', 'ac=A|del=A|bp=A'): (True, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('indevolt', 'ac=A|del=A|bp=500'): (True, (500.0, (500.0, 1, 3), 500.0, 500.0, 500.0, 500.0)),
    ('indevolt', 'ac=A|del=A|bp=-500'): (True, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('indevolt', 'ac=-200|del=200|bp=A'): (True, (200.0, (200.0, 1, 3), 200.0, 200.0, 200.0, 200.0)),
    ('indevolt', 'ac=-200|del=200|bp=500'): (True, (200.0, (200.0, 1, 3), 200.0, 200.0, 200.0, 200.0)),
    ('indevolt', 'ac=-200|del=200|bp=-500'): (True, (200.0, (200.0, 1, 3), 200.0, 200.0, 200.0, 200.0)),
    ('indevolt', 'ac=200|del=-200|bp=A'): (True, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, -200.0)),
    ('indevolt', 'ac=200|del=-200|bp=500'): (True, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, -200.0)),
    ('indevolt', 'ac=200|del=-200|bp=-500'): (True, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, -200.0)),
    ('hoymiles', 'ac=A|del=A|bp=A'): (True, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('hoymiles', 'ac=A|del=A|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('hoymiles', 'ac=A|del=A|bp=500'): (True, (500.0, (500.0, 2, 2), 500.0, 500.0, 500.0, 500.0)),
    ('hoymiles', 'ac=A|del=A|bp=-500'): (True, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('hoymiles', 'ac=A|del=None|bp=A'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('hoymiles', 'ac=A|del=None|bp=None'): (False, (None, (None, 0, 0), None, None, 0.0, 0.0)),
    ('hoymiles', 'ac=A|del=None|bp=500'): (False, (500.0, (500.0, 2, 2), 500.0, 500.0, 500.0, 500.0)),
    ('hoymiles', 'ac=A|del=None|bp=-500'): (False, (-500.0, (-500.0, 4, 4), -500.0, -500.0, -500.0, -500.0)),
    ('hoymiles', 'ac=A|del=200|bp=A'): (True, (200.0, (200.0, 2, 2), 200.0, 200.0, 200.0, 0.0)),
    ('hoymiles', 'ac=A|del=200|bp=None'): (False, (200.0, (200.0, 2, 2), 200.0, 200.0, 200.0, 0.0)),
    ('hoymiles', 'ac=A|del=200|bp=500'): (True, (200.0, (200.0, 2, 2), 200.0, 200.0, 200.0, 500.0)),
    ('hoymiles', 'ac=A|del=200|bp=-500'): (True, (200.0, (200.0, 2, 2), 200.0, 200.0, 200.0, -500.0)),
    ('hoymiles', 'ac=A|del=-200|bp=A'): (True, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, 0.0)),
    ('hoymiles', 'ac=A|del=-200|bp=None'): (False, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, 0.0)),
    ('hoymiles', 'ac=A|del=-200|bp=500'): (True, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, 500.0)),
    ('hoymiles', 'ac=A|del=-200|bp=-500'): (True, (-200.0, (-200.0, 4, 4), -200.0, -200.0, -200.0, -500.0)),
}
# GOLDEN-END


def _rows(variant):
    spec = VARIANTS[variant]
    for data in spec["rows"]:
        yield _label(data), data, spec["reach"](data)


@pytest.fixture(autouse=True)
def _no_ha_runtime(monkeypatch):
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_a, **_k: None)


@pytest.mark.parametrize("variant", list(VARIANTS))
async def test_ac_contribution_table(variant):
    coordinator = _coordinator(variant)
    extra = VARIANTS[variant]["extra"]
    mismatches = []
    for label, data, reach in _rows(variant):
        coordinator.data = {**extra, **data}
        observed = (reach, compute(coordinator))
        if GOLDEN.get((variant, label)) != observed:
            mismatches.append((label, GOLDEN.get((variant, label)), observed))
    assert not mismatches, mismatches


def test_every_variant_is_in_the_table():
    assert {v for v, _ in GOLDEN} == set(VARIANTS)
    assert {BRAND for BRAND in BRAND_CONFIGS} == {
        "marstek", "zendure", "esphome", "anker", "sessy", "indevolt", "huawei", "hoymiles",
    }


# --------------------------------------------------------------------------- #
# Which driver can publish which key (the rows above only vary those keys).
# --------------------------------------------------------------------------- #

def test_drivers_that_publish_ac_power_and_the_ac_port():
    base = Path(inspect.getfile(drivers_pkg)).parent
    publishes_ac = set()
    publishes_port = set()
    for path in base.glob("*.py"):
        if path.name in ("base.py", "registry.py", "__init__.py"):
            continue
        text = path.read_text(encoding="utf-8")
        if '"ac_power"' in text:
            publishes_ac.add(path.stem)
        if "DELIVERED_AC_POWER_KEY" in text:
            publishes_port.add(path.stem)
    # Marstek's ``ac_power`` is a register in ``const/registers_*.py``; huawei names
    # the key only to say it does NOT publish it.
    assert publishes_ac - {"huawei", "marstek"} == {"esphome", "anker", "indevolt"}
    registers = base.parent / "const"
    assert all(
        '"ac_power"' in (registers / f"registers_{v}.py").read_text(encoding="utf-8")
        for v in ("v2", "v3", "va")
    )
    assert publishes_port >= {"zendure", "hoymiles", "anker", "indevolt"}
    assert not publishes_port & {"marstek", "esphome", "sessy", "huawei"}


# --------------------------------------------------------------------------- #
# The reachable differences, spelled out (the table above holds all of them).
# --------------------------------------------------------------------------- #

class TestDifferingRows:
    """Reachable rows where the five implementations disagree (plan section 8)."""

    async def test_anker_independent_pv_prefers_ac_power_in_i1_and_the_port_in_i3_i4(self):
        coordinator = _coordinator("anker-independent-pv")
        coordinator.data = {"solar_power": 1000, BP: 800, AC: -800, DEL: -200}
        i1, _i2, i3, i4, i5, _i5b = compute(coordinator)
        # Pack charging 800 W from 1000 W of PV: the AC port exports 200 W.
        assert i1 == 800.0 and i5 == 800.0  # ac_power first: reads a charge
        assert i3 == -200.0 and i4 == -200.0  # the port first: reads a 200 W discharge

    async def test_marstek_with_mppt_refuses_the_cell_fallback_only_in_i3(self):
        coordinator = _coordinator("marstek-vA-mppt")
        coordinator.data = {BP: 500, "mppt1_power": 400}
        i1, _i2, i3, i4, i5, _i5b = compute(coordinator)
        assert (i1, i3, i4, i5) == (500.0, None, 500.0, 500.0)

    async def test_an_unavailable_battery_reads_none_only_in_i3(self):
        coordinator = _coordinator("marstek-v3")
        coordinator._is_connected = False
        coordinator.data = {AC: 300, BP: 500}
        i1, _i2, i3, i4, i5, _i5b = compute(coordinator)
        assert (i1, i3, i4, i5) == (-300.0, None, -300.0, -300.0)

    async def test_nothing_reported_is_none_except_i5_which_reads_zero(self):
        coordinator = _coordinator("marstek-v3")
        coordinator.data = {}
        i1, _i2, i3, i4, i5, i5b = compute(coordinator)
        assert (i1, i3, i4, i5, i5b) == (None, None, None, 0.0, 0.0)

    @pytest.mark.parametrize("variant", ["marstek-v3", "zendure-2400ac_pro", "sessy"])
    async def test_a_zero_reading_is_a_reading_not_an_absence(self, variant):
        coordinator = _coordinator(variant)
        key = {"marstek-v3": AC, "zendure-2400ac_pro": DEL, "sessy": BP}[variant]
        coordinator.data = {key: 0, BP: 500}
        i1 = compute(coordinator)[0]
        assert i1 == (0.0 if variant != "sessy" else 500.0)
