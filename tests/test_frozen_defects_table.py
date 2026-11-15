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

from custom_components.omnibattery import _migrate_missing_battery_version
from custom_components.omnibattery import config_flow
from custom_components.omnibattery.drivers import registry
from tests.test_driver_construction_characterization import (  # noqa: F401
    BRAND_CONFIGS,
    _build_coordinator,
    _no_ha_runtime,
)

SNAPSHOT = Path(__file__).parent / "snapshots" / "frozen_defects_table.json"
ABSENT = "<absent>"
VERSIONS = (ABSENT, "v2", "v3", "vA", "vD", "zz")
SERIAL_PORTS = (ABSENT, "/dev/ttyUSB0")
BRANDS = (*registry.DRIVERS, "acme", "<missing>")


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
        "fingerprint_model": _safe(lambda: getattr(coord(), "battery_version", None)),
    }


def _migration_row(brand):
    cfg = _base_config(brand)
    cfg.pop("battery_version", None)
    entry = SimpleNamespace(data={"batteries": [cfg]})
    _migrate_missing_battery_version(entry)
    stored = entry.data["batteries"][0]
    return {
        "battery_version_added": "battery_version" in stored,
        "battery_version": stored.get("battery_version"),
    }


def _build_table():
    rows = {}
    for brand in BRANDS:
        for version in VERSIONS:
            for serial in SERIAL_PORTS:
                rows[f"{brand}|version={version}|serial={serial}"] = _row(brand, version, serial)
    return {
        "rows": rows,
        "migration": {brand: _safe(lambda b=brand: _migration_row(b)) for brand in BRANDS},
    }


def _diff(expected, actual):
    lines = []
    for section in ("rows", "migration"):
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
