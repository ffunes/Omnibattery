"""Characterization table of the behaviours the driver-registry section-8 fixes will change.

Pins today's output per (brand, stored battery_version, serial_port) so each later
fix shows exactly which cells it changes. Regenerate the snapshot with
``OMNIBATTERY_UPDATE_SNAPSHOTS=1``; otherwise the table must equal the snapshot.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace

from custom_components.omnibattery import config_flow
from custom_components.omnibattery.drivers import registry
from custom_components.omnibattery.tracking import solar_profile
from custom_components.omnibattery.tracking.solar_profile import SolarProfileTracker
from tests.test_driver_construction_characterization import (  # noqa: F401
    BRAND_CONFIGS,
    _build_coordinator,
    _no_ha_runtime,
)

SNAPSHOT = Path(__file__).parent / "snapshots" / "frozen_defects_table.json"
ABSENT = "<absent>"
VERSIONS = (ABSENT, "v2", "v3", "vA", "vD", "zz")
SERIAL_PORTS = (ABSENT, "/dev/ttyUSB0")
# The eight brands of the refactor, fixed: a brand registered later (plan T8.3)
# must not have to edit this table.
BRANDS = (
    "marstek", "zendure", "esphome", "anker", "sessy", "indevolt", "hoymiles", "huawei",
    "acme", "<missing>",
)


def _base_config(brand):
    if brand in BRAND_CONFIGS:
        return copy.deepcopy(BRAND_CONFIGS[brand])
    cfg = copy.deepcopy(BRAND_CONFIGS["marstek"])
    if brand == "acme":
        cfg["brand"] = "acme"
    else:
        cfg.pop("brand", None)
    return cfg


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _safe(fn):
    try:
        return _jsonable(fn())
    except Exception as err:  # noqa: BLE001 - the exception class is the pinned value
        return f"raises {type(err).__name__}"


def _fingerprint_model(real):
    """The ``model`` the real ``configuration_fingerprint`` hashes for this coordinator.

    Runs the real method on a stand-in carrying the coordinator's own version
    attributes, with a connected PV input forced so the source is included, and
    reads the model out of the payload it is about to hash.
    """
    coordinator = SimpleNamespace(
        device_key="x",
        capabilities=SimpleNamespace(has_mppt_pv=True),
        dc_pv_connected=True,
        data={},
        battery_version=real.battery_version,
    )
    if hasattr(real, "stored_battery_version"):
        coordinator.stored_battery_version = real.stored_battery_version
    tracker = SolarProfileTracker.__new__(SolarProfileTracker)
    tracker._hass = SimpleNamespace(config=SimpleNamespace(time_zone="UTC"))
    tracker._config_entry = SimpleNamespace(data={})
    tracker._controller = SimpleNamespace(solar_production_sensor=None, coordinators=[coordinator])
    captured = []
    real_dumps = solar_profile.json.dumps

    def spy(payload, *args, **kwargs):
        if isinstance(payload, dict) and "mppt" in payload:
            captured.append(payload["mppt"][0]["model"])
        return real_dumps(payload, *args, **kwargs)

    solar_profile.json.dumps = spy
    try:
        tracker.configuration_fingerprint()
    finally:
        solar_profile.json.dumps = real_dumps
    return captured[0]


def _row(brand, version, serial):
    cfg = _base_config(brand)
    for key, val in (("battery_version", version), ("serial_port", serial)):
        if val == ABSENT:
            cfg.pop(key, None)
        else:
            cfg[key] = val
    reg_brand = cfg.get("brand")
    coordinator = {}

    def coord():
        if "c" not in coordinator:
            coordinator["c"] = _build_coordinator(copy.deepcopy(cfg))
        return coordinator["c"]

    return {
        "traits": _safe(lambda: dataclasses.asdict(registry.traits_for_brand(reg_brand, cfg))),
        "link_key": _safe(lambda: registry.driver_class(reg_brand).link_key(cfg)),
        "is_ip_addressed": _safe(lambda: registry.is_ip_addressed(cfg)),
        "slot_ceiling_charge": _safe(lambda: config_flow._battery_hardware_max(cfg, "max_charge_power")),
        "slot_ceiling_discharge": _safe(lambda: config_flow._battery_hardware_max(cfg, "max_discharge_power")),
        "coordinator_battery_version": _safe(lambda: coord().battery_version),
        "fingerprint_model": _safe(lambda: _fingerprint_model(coord())),
    }


def _build_table():
    rows = {}
    for brand in BRANDS:
        for version in VERSIONS:
            for serial in SERIAL_PORTS:
                rows[f"{brand}|version={version}|serial={serial}"] = _row(brand, version, serial)
    # fixed: plan section 8 #20: the battery_version migration is gone, so no "migration" section.
    return {"rows": rows}


def _diff(expected, actual):
    lines = []
    for section in ("rows",):
        exp, act = expected.get(section, {}), actual.get(section, {})
        for row in sorted(set(exp) | set(act)):
            e, a = exp.get(row), act.get(row)
            if e == a:
                continue
            if not isinstance(e, dict) or not isinstance(a, dict):
                lines.append(f"{section}/{row}: {e!r} -> {a!r}")
                continue
            for col in sorted(set(e) | set(a)):
                if e.get(col) != a.get(col):
                    lines.append(f"{section}/{row}/{col}: {e.get(col)!r} -> {a.get(col)!r}")
    return lines


async def test_frozen_defects_table():
    table = _build_table()
    if os.environ.get("OMNIBATTERY_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    actual = json.loads(json.dumps(table))
    diff = _diff(expected, actual)
    assert not diff, "frozen-defects table changed:\n" + "\n".join(diff)
