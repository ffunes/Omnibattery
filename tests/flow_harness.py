"""Shared harness for the config-flow characterisation tests (driver-registry T1.2).

Drives the REAL ``MarstekVenusConfigFlow`` / ``OptionsFlowHandler`` through Home
Assistant's flow manager and captures ``entry.data["batteries"][i]`` as it comes
out of the flow, per brand. It exists so the "everything in drivers/" refactor
can move the per-brand flow code and prove that the stored shape did not change.

Not a test module (no ``test_`` prefix). Used by ``test_config_entry_snapshots.py``
and meant to be reused by the follow-up characterisation tests (Huawei two-step
and back-jump, Indevolt with/without the mode notice, DHCP, backup restore,
error forms, section 2.3 drifts).

These tests need the HA plugin; they do NOT run under the plain ``pytest``::

    .venv-test/Scripts/python -m pytest -q -o addopts="" tests/<file>.py

Pieces
------
* ``BRANDS`` / ``CASES``: one ``BrandCase`` per brand (plus a Marstek vD variant),
  holding the form inputs for add, options and reconfigure and the battery as
  it would already be stored (the starting point of options/reconfigure).
* ``Probes``: deterministic, scriptable replacement for every network/registry
  probe the flows make. Mutate its fields to script a failure or another
  device; ``probes.calls`` records what was probed and with which arguments.
* ``FlowEnv`` (fixture ``flow_env``): hass + probes + patched setup/unload, with
  ``add_via_config_flow``, ``edit_via_options_flow`` and ``reconfigure`` (each
  returns ``Run`` with the final result, the visited step ids and the entry).
  ``drive`` is the lower-level loop they share.
* ``assert_snapshot``: exact comparison against a JSON file under
  ``tests/snapshots/config_entry``. Missing file fails, unless
  ``OMNIBATTERY_UPDATE_SNAPSHOTS=1``, which (re)writes it.

Determinism: the flows add no random ids or timestamps to a battery dict (the
entry id lives outside ``data["batteries"]``), so snapshots are written as-is.
``normalize`` is the single place to scrub a value if one ever appears.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omnibattery.drivers.anker import AnkerModbusDriver
from custom_components.omnibattery.drivers.esphome import EsphomeEntityDriver
from custom_components.omnibattery.drivers.generic_ha import GenericHaEntityDriver
from custom_components.omnibattery.drivers.hoymiles import HoymilesMqttDriver
from custom_components.omnibattery.drivers.huawei import HuaweiSolarDriver
from custom_components.omnibattery.drivers.indevolt import IndevoltDriver
from custom_components.omnibattery.drivers.marstek import MarstekModbusDriver
from custom_components.omnibattery.drivers.sessy import SessyLocalDriver
from custom_components.omnibattery.drivers.zendure import ZendureLocalDriver

DOMAIN = "omnibattery"
ENTRY_VERSION = 16  # MarstekVenusConfigFlow.VERSION: no migration runs on a fresh MockConfigEntry

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "config_entry"
UPDATE_ENV = "OMNIBATTERY_UPDATE_SNAPSHOTS"

BRANDS = (
    "marstek", "zendure", "esphome", "anker", "sessy", "indevolt", "huawei", "hoymiles",
    "generic_ha",
)

# Global (system) part of the entry. Only here so the flow can be driven from `user`
# to the end and so an OF/reconfigure entry looks like a real one.
SYSTEM_INPUT = {
    "consumption_sensor": "sensor.grid_power",
    "max_contracted_power": 7000,
}


# --------------------------------------------------------------------------- cases


@dataclass(frozen=True)
class BrandCase:
    """Everything needed to push one brand through the three paths.

    ``add_input`` / ``options_input`` / ``reconfigure_input`` are what the user
    submits on the connection step; ``limits_input`` what they submit on
    ``battery_limits`` (``{}`` = accept every default; only fields that have no
    default must be given). ``connection_step`` is the add/options step id,
    ``reconfigure_step`` the first reconfigure step id. ``extra_*_steps`` are the
    steps that follow the connection step with their own inputs (Indevolt's
    request speed), as ``{step_id: user_input}``.
    """

    id: str
    brand: str
    connection_step: str
    reconfigure_step: str
    add_input: dict
    options_input: dict
    reconfigure_input: dict
    existing: dict
    limits_input: dict = field(default_factory=dict)
    options_limits_input: dict = field(default_factory=dict)
    extra_add_steps: dict = field(default_factory=dict)
    extra_reconfigure_steps: dict = field(default_factory=dict)


def _stored(**brand_fields: Any) -> dict:
    """A battery as an earlier run of the flow stored it, plus the persisted extras.

    Values differ from what the tests submit, so a snapshot shows what the flow
    overwrites and what it keeps (``kept_runtime_key`` is not a form field).
    """
    return {
        "max_charge_power": 1500,
        "max_discharge_power": 1500,
        "max_soc": 95,
        "min_soc": 15,
        "enable_charge_hysteresis": True,
        "charge_hysteresis_percent": 5,
        "dc_pv_connected": False,
        "full_charge_voltage_taper_enabled": False,
        "track_mac": False,
        "mac": "",
        "kept_runtime_key": "kept",
        **brand_fields,
    }


CASES: dict[str, BrandCase] = {
    "marstek": BrandCase(
        id="marstek",
        brand="marstek",
        connection_step="battery_connection",
        reconfigure_step="reconfigure_battery",
        add_input={"name": "Venus 1", "host": "192.0.2.10", "port": 502,
                   "slave_id": 1, "battery_version": "v3"},
        options_input={"name": "Venus 1 edited", "host": "192.0.2.11", "port": 502,
                       "slave_id": 1, "battery_version": "v3"},
        reconfigure_input={"name": "Venus 1 moved", "host": "192.0.2.12", "port": 502,
                           "slave_id": 1, "battery_version": "v3"},
        existing=_stored(
            brand="marstek", name="Venus 1", host="192.0.2.10", port=502,
            serial_port="", rs485_gateway=False, slave_id=1, battery_version="v3",
            ems_version=None,
        ),
    ),
    # vD: probe_details returns the EMS firmware; vA/vD add the DC-PV field.
    "marstek_vd": BrandCase(
        id="marstek_vd",
        brand="marstek",
        connection_step="battery_connection",
        reconfigure_step="reconfigure_battery",
        add_input={"name": "Venus D", "host": "192.0.2.20", "port": 502,
                   "slave_id": 1, "battery_version": "vD"},
        options_input={"name": "Venus D edited", "host": "192.0.2.21", "port": 502,
                       "slave_id": 1, "battery_version": "vD"},
        reconfigure_input={"name": "Venus D moved", "host": "192.0.2.22", "port": 502,
                           "slave_id": 1, "battery_version": "vD"},
        existing=_stored(
            brand="marstek", name="Venus D", host="192.0.2.20", port=502,
            serial_port="", rs485_gateway=False, slave_id=1, battery_version="vD",
            ems_version=140, dc_pv_connected=True,
        ),
    ),
    "zendure": BrandCase(
        id="zendure",
        brand="zendure",
        connection_step="battery_connection_zendure",
        reconfigure_step="reconfigure_battery_zendure",
        add_input={"name": "Zendure 1", "host": "192.0.2.30", "port": 80},
        options_input={"name": "Zendure 1 edited", "host": "192.0.2.31", "port": 80},
        reconfigure_input={"name": "Zendure 1 moved", "host": "192.0.2.32", "port": 80},
        existing=_stored(
            brand="zendure", name="Zendure 1", host="192.0.2.30", port=80,
            zendure_model="2400ac_pro", user_max_charge_power=1500,
            battery_capacity_kwh=1.92,
        ),
    ),
    "esphome": BrandCase(
        id="esphome",
        brand="esphome",
        connection_step="battery_connection_esphome",
        reconfigure_step="reconfigure_battery_esphome",
        add_input={"name": "LilyGo 1", "esphome_device": "esphome_device_id_1"},
        options_input={"name": "LilyGo 1 edited", "esphome_device": "esphome_device_id_2"},
        reconfigure_input={"name": "LilyGo 1 moved", "esphome_device": "esphome_device_id_3"},
        existing=_stored(
            brand="esphome", name="LilyGo 1", host="esphome_device_id_1", port=0,
            esphome_device_id="esphome_device_id_1",
        ),
    ),
    "anker": BrandCase(
        id="anker",
        brand="anker",
        connection_step="battery_connection_anker",
        reconfigure_step="reconfigure_battery_anker",
        add_input={"name": "Anker 1", "host": "192.0.2.40", "port": 502, "slave_id": 1},
        options_input={"name": "Anker 1 edited", "host": "192.0.2.41", "port": 502, "slave_id": 1},
        reconfigure_input={"name": "Anker 1 moved", "host": "192.0.2.42", "port": 502, "slave_id": 2},
        existing=_stored(
            brand="anker", name="Anker 1", host="192.0.2.40", port=502, slave_id=1,
            device_max_charge_power=3000, device_max_discharge_power=3000,
        ),
    ),
    "sessy": BrandCase(
        id="sessy",
        brand="sessy",
        connection_step="battery_connection_sessy",
        reconfigure_step="reconfigure_battery_sessy",
        add_input={"name": "Sessy 1", "host": "192.0.2.50", "port": 80,
                   "username": "sessy-user", "password": "sessy-pass"},
        options_input={"name": "Sessy 1 edited", "host": "192.0.2.51", "port": 80,
                       "username": "sessy-user", "password": "sessy-pass"},
        reconfigure_input={"name": "Sessy 1 moved", "host": "192.0.2.52", "port": 80,
                           "username": "sessy-user2", "password": "sessy-pass2"},
        # The capacity has no default (required): add must give it. In options it
        # is prefilled from the stored value, so {} is enough there.
        limits_input={"battery_capacity_kwh": 5.4},
        existing=_stored(
            brand="sessy", name="Sessy 1", host="192.0.2.50", port=80,
            username="sessy-user", password="sessy-pass", battery_capacity_kwh=5.2,
        ),
    ),
    "indevolt": BrandCase(
        id="indevolt",
        brand="indevolt",
        connection_step="battery_connection_indevolt",
        reconfigure_step="reconfigure_battery_indevolt",
        add_input={"name": "Indevolt 1", "host": "192.0.2.60", "port": 8080},
        options_input={"name": "Indevolt 1 edited", "host": "192.0.2.61", "port": 8080},
        reconfigure_input={"name": "Indevolt 1 moved", "host": "192.0.2.62", "port": 8080},
        extra_add_steps={"indevolt_request_speed": {}},
        extra_reconfigure_steps={"reconfigure_battery_indevolt_request_speed": {}},
        existing=_stored(
            brand="indevolt", name="Indevolt 1", host="192.0.2.60", port=8080,
            device_max_charge_power=2400, device_max_discharge_power=2400,
            indevolt_request_speed="normal",
        ),
    ),
    "huawei": BrandCase(
        id="huawei",
        brand="huawei",
        connection_step="battery_connection_huawei",
        reconfigure_step="reconfigure_battery_huawei",
        # direct_write + an explicit slave id: one step, no device selector, no scan.
        add_input={"name": "Huawei 1", "host": "192.0.2.70", "port": 502,
                   "slave_id": 1, "huawei_direct_write": True},
        options_input={"name": "Huawei 1 edited", "host": "192.0.2.71", "port": 502,
                       "slave_id": 1, "huawei_direct_write": True},
        reconfigure_input={"name": "Huawei 1 moved", "host": "192.0.2.72", "port": 502,
                           "slave_id": 3, "huawei_direct_write": True},
        existing=_stored(
            brand="huawei", name="Huawei 1", host="192.0.2.70", port=502, slave_id=1,
            huawei_battery_device_id="", huawei_direct_write=True,
            huawei_model="SUN2000-5KTL-L1", huawei_emma_slave_id=7,
            device_max_charge_power=5000, device_max_discharge_power=5000,
        ),
    ),
    "hoymiles": BrandCase(
        id="hoymiles",
        brand="hoymiles",
        connection_step="battery_connection_hoymiles",
        reconfigure_step="reconfigure_battery_hoymiles",
        add_input={"name": "Hoymiles 1", "device_id": "hm_dev_1", "hoymiles_model": "auto"},
        options_input={"name": "Hoymiles 1 edited", "device_id": "hm_dev_2", "hoymiles_model": "auto"},
        reconfigure_input={"name": "Hoymiles 1 moved", "device_id": "hm_dev_3", "hoymiles_model": "auto"},
        existing=_stored(
            brand="hoymiles", name="Hoymiles 1", host="hm_dev_1", port=0,
            device_id="hm_dev_1", hoymiles_model="ms_a2", hoymiles_model_label="MS-A2",
            device_max_charge_power=1000, device_max_discharge_power=1000,
            battery_capacity_kwh=2.24,
        ),
    ),
    "generic_ha": BrandCase(
        id="generic_ha",
        brand="generic_ha",
        connection_step="battery_connection_generic_ha",
        reconfigure_step="reconfigure_battery_generic_ha",
        # No host, port or credential anywhere: the whole form is entity ids.
        add_input={
            "name": "Observed 1",
            "ha_soc_entity": "sensor.byd_soc",
            "ha_power_entity": "sensor.byd_power",
            "ha_power_positive_is": "charge",
            "ha_stale_after_s": 600,
        },
        options_input={
            "name": "Observed 1 edited",
            "ha_soc_entity": "sensor.byd_soc_2",
            "ha_power_entity": "sensor.byd_power_2",
            "ha_power_positive_is": "discharge",
            "ha_charge_energy_entities": ["sensor.byd_charged"],
            "ha_discharge_energy_entity": "sensor.byd_discharged",
            "ha_target_soc_entity": "number.byd_min_soc",
            "ha_stale_after_s": 900,
        },
        reconfigure_input={
            "name": "Observed 1 moved",
            "ha_soc_entity": "sensor.byd_soc_3",
            "ha_power_entity": "sensor.byd_power_3",
            "ha_power_positive_is": "charge",
            "ha_stale_after_s": 600,
        },
        # The battery reports no capacity of its own, so the limits step asks
        # for one and it has no default to accept.
        limits_input={"battery_capacity_kwh": 6.0},
        options_limits_input={"battery_capacity_kwh": 6.0},
        existing=_stored(
            brand="generic_ha", name="Observed 1",
            host="0123456789abcdef0123456789abcdef", port=0,
            ha_soc_entity="sensor.byd_soc", ha_power_entity="sensor.byd_power",
            ha_power_positive_is="charge", ha_charge_energy_entities=[],
            ha_discharge_energy_entity=None, ha_target_soc_entity=None,
            ha_stale_after_s=600,
            battery_capacity_kwh=6.0,
        ),
    ),
}


# --------------------------------------------------------------------------- probes


class Probes:
    """Scriptable replacement for every probe the config flows make. Zero network.

    Defaults describe a healthy device of every brand. Attributes are plain data
    so a test can change them before driving the flow:

    * ``ok_<brand>``: False makes that brand's probe fail (error form).
    * ``indevolt_info``: dict returned by ``IndevoltDriver.probe`` (or an
      exception instance, which is raised; ``None`` = unreachable).
    * ``huawei_probes``: queue of probe tuples, consumed one per call (the last
      one repeats), so a mismatch followed by success can be scripted.
    * ``huawei_scan``: ``[(slave_id, model, has_battery), ...]``;
      ``huawei_emma``: slave id or None.
    * ``esphome_missing``: list of missing entity names.
    * ``generic_ha_missing``: list of logical keys whose entity is unreadable.
    * ``marstek_ok`` / ``marstek_ems_version``.
    * ``calls``: ``[(name, *args)]`` in call order.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.ok_marstek = True
        self.marstek_ems_version: int | None = 149
        self.ok_zendure = True
        self.zendure_product = "SolarFlow 2400 AC+"
        self.ok_sessy = True
        self.ok_anker = True
        self.anker_caps = {"device_max_charge_power": 3200, "device_max_discharge_power": 3000}
        self.indevolt_info: Any = {
            "type": "SolidFlex",
            "model": "SF2000",
            "known_model": True,
            "serial": "SN-INDEVOLT-1",
            "firmware": "1.0.0",
            "mac": "AA:BB:CC:00:00:01",
            "energy_mode": 5,
            "realtime_mode": True,
            "max_charge_power": 2400,
            "max_discharge_power": 2400,
            "native_min_soc": 5,
            "battery_capacity_kwh": 2.0,
            "settings": {"min_soc": 10, "max_charge_power": 2200, "max_discharge_power": 2300},
        }
        self.ok_hoymiles = True
        self.hoymiles_caps = {
            "hoymiles_model": "ms_a2",
            "hoymiles_model_label": "MS-A2",
            "device_max_charge_power": 1000,
            "device_max_discharge_power": 1000,
            "battery_capacity_kwh": 2.24,
        }
        # (ok, model, max_charge, max_discharge, serial, inverter_max)
        self.huawei_probes: list[tuple] = [
            (True, "SUN2000-5KTL-L1", 5000, 4500, "HV0000000001", 6000)
        ]
        self.huawei_scan: list[tuple] = [(1, "SUN2000-5KTL-L1", True)]
        self.huawei_emma: int | None = None
        self.esphome_missing: list[str] = []
        self.generic_ha_missing: list[str] = []

    # -- helpers used by the patches --
    def _record(self, *call: Any) -> None:
        self.calls.append(call)

    def calls_named(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        probes = self

        async def marstek_probe(host, port, version="v2", slave_id=1, serial_port=None):
            probes._record("marstek.probe", host, port, version, slave_id, serial_port)
            return probes.ok_marstek

        async def marstek_probe_details(host, port, version, slave_id=1, serial_port=None):
            probes._record("marstek.probe_details", host, port, version, slave_id, serial_port)
            return probes.ok_marstek, probes.marstek_ems_version if probes.ok_marstek else None

        async def zendure_probe(host, port=80):
            probes._record("zendure.probe", host, port)
            return probes.ok_zendure, probes.zendure_product

        async def sessy_probe(host, port=80, username="", password=""):
            probes._record("sessy.probe", host, port, username, password)
            return probes.ok_sessy

        async def anker_probe(host, port=502, slave_id=1):
            probes._record("anker.probe", host, port, slave_id)
            return probes.ok_anker, dict(probes.anker_caps) if probes.ok_anker else {}

        async def indevolt_probe(host, port=8080, **_):
            probes._record("indevolt.probe", host, port)
            info = probes.indevolt_info
            if isinstance(info, BaseException):
                raise info
            return None if info is None else json.loads(json.dumps(info))

        async def hoymiles_probe(hass, device_id, timeout=5.0, *, model_hint=None):
            probes._record("hoymiles.probe", device_id, model_hint)
            if not probes.ok_hoymiles:
                return False, {}
            return True, dict(probes.hoymiles_caps)

        async def huawei_probe(hass, host, port=502, slave_id=1):
            probes._record("huawei.probe", host, port, slave_id)
            queue = probes.huawei_probes
            return queue.pop(0) if len(queue) > 1 else queue[0]

        async def huawei_scan(hass, host, port=502):
            probes._record("huawei.scan_slave_ids", host, port)
            return list(probes.huawei_scan)

        async def huawei_emma(hass, host, port=502):
            probes._record("huawei.find_emma_slave_id", host, port)
            return probes.huawei_emma

        def esphome_resolve(hass, device_id):
            probes._record("esphome.resolve", device_id)
            return {}, list(probes.esphome_missing)

        def generic_ha_resolve(hass, entities):
            probes.calls.append(("generic_ha_resolve", tuple(sorted(entities.items()))))
            return list(probes.generic_ha_missing)

        # Plain functions set on a class are not bound when read from the class,
        # which is how the flows call them (``Driver.probe(...)``).
        monkeypatch.setattr(MarstekModbusDriver, "probe", marstek_probe)
        monkeypatch.setattr(MarstekModbusDriver, "probe_details", marstek_probe_details)
        monkeypatch.setattr(ZendureLocalDriver, "probe", zendure_probe)
        monkeypatch.setattr(SessyLocalDriver, "probe", sessy_probe)
        monkeypatch.setattr(AnkerModbusDriver, "probe", anker_probe)
        monkeypatch.setattr(IndevoltDriver, "probe", indevolt_probe)
        monkeypatch.setattr(HoymilesMqttDriver, "probe", hoymiles_probe)
        monkeypatch.setattr(HuaweiSolarDriver, "probe", huawei_probe)
        monkeypatch.setattr(HuaweiSolarDriver, "scan_slave_ids", huawei_scan)
        monkeypatch.setattr(HuaweiSolarDriver, "find_emma_slave_id", huawei_emma)
        monkeypatch.setattr(EsphomeEntityDriver, "resolve", esphome_resolve)
        monkeypatch.setattr(GenericHaEntityDriver, "resolve", generic_ha_resolve)


# --------------------------------------------------------------------------- driving


@dataclass
class Run:
    """Outcome of ``drive``: final flow result, step ids visited in order, the entry."""

    result: dict
    steps: list[str]
    entry: Any = None

    @property
    def batteries(self) -> list[dict]:
        return self.entry.data["batteries"]


async def drive(
    hass,
    manager,
    result: dict,
    inputs: dict[str, Any] | None = None,
    *,
    stop_at: str | None = None,
    max_steps: int = 40,
) -> tuple[dict, list[str]]:
    """Feed a flow until it stops being a form/menu (or reaches ``stop_at``).

    ``inputs`` maps ``step_id`` -> user input (``{}`` when absent = accept the form
    defaults). For a menu step the value is the ``next_step_id`` string. Returns
    ``(last_result, visited_step_ids)``; a form that came back with errors is
    visited again, so a repeated id in ``steps`` means a re-shown form. If
    ``stop_at`` is given, returns as soon as that step's form is shown (not
    submitted), which lets a test inspect the form or submit it by hand.
    """
    inputs = inputs or {}
    steps: list[str] = []
    for _ in range(max_steps):
        kind = result["type"]
        if kind not in (FlowResultType.FORM, FlowResultType.MENU):
            return result, steps
        step_id = result["step_id"]
        steps.append(step_id)
        if stop_at is not None and step_id == stop_at:
            return result, steps
        if kind is FlowResultType.MENU:
            user_input: Any = {"next_step_id": inputs[step_id]}
        else:
            user_input = inputs.get(step_id, {})
            if callable(user_input):
                user_input = user_input(result)
        result = await manager.async_configure(result["flow_id"], user_input)
    raise AssertionError(f"flow did not finish in {max_steps} steps: {steps}")


class FlowEnv:
    """hass + probes + the three paths, ready to run (built by the ``flow_env`` fixture)."""

    def __init__(self, hass, probes: Probes) -> None:
        self.hass = hass
        self.probes = probes

    # -- entries --

    def make_entry(self, batteries: list[dict], **extra: Any) -> MockConfigEntry:
        """An existing, not-loaded entry (setup is patched out anyway) holding ``batteries``."""
        data = {**SYSTEM_INPUT, "num_batteries": len(batteries), "batteries": batteries, **extra}
        entry = MockConfigEntry(domain=DOMAIN, title="Omnibattery", data=data, version=ENTRY_VERSION)
        entry.add_to_hass(self.hass)
        return entry

    # -- path a: config flow, add --

    async def add_via_config_flow(
        self,
        case: BrandCase,
        *,
        extra_inputs: dict[str, Any] | None = None,
        connection_input: dict | None = None,
        limits_input: dict | None = None,
    ) -> Run:
        """``user`` -> batteries -> battery_brand -> connection -> limits -> ... -> entry created."""
        hass = self.hass
        manager = hass.config_entries.flow
        result = await manager.async_init(DOMAIN, context={"source": SOURCE_USER})
        inputs: dict[str, Any] = {
            "user": dict(SYSTEM_INPUT),
            "batteries": {"num_batteries": 1},
            "battery_brand": {"brand": case.brand},
            case.connection_step: connection_input or case.add_input,
            "battery_limits": limits_input if limits_input is not None else case.limits_input,
            **case.extra_add_steps,
            **(extra_inputs or {}),
        }
        result, steps = await drive(hass, manager, result, inputs)
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY, (steps, result)
        entry = hass.config_entries.async_get_entry(result["result"].entry_id)
        return Run(result, steps, entry)

    # -- path b: options flow, edit the battery at slot 0 --

    async def edit_via_options_flow(
        self,
        case: BrandCase,
        entry: MockConfigEntry | None = None,
        *,
        extra_inputs: dict[str, Any] | None = None,
        connection_input: dict | None = None,
        limits_input: dict | None = None,
        brand: str | None = None,
    ) -> Run:
        """init -> batteries -> battery_count -> battery_brand -> connection -> limits -> saved.

        ``brand`` overrides the brand picked in ``battery_brand`` (a brand change
        on an entry whose stored battery is ``case.existing``).
        """
        hass = self.hass
        if entry is None:
            entry = self.make_entry([dict(case.existing)])
        manager = hass.config_entries.options
        result = await manager.async_init(entry.entry_id)
        inputs: dict[str, Any] = {
            "init": "batteries",
            "battery_count": {"num_batteries": 1},
            "battery_brand": {"brand": brand or case.brand},
            case.connection_step: connection_input or case.options_input,
            "battery_limits": (
                limits_input if limits_input is not None else case.options_limits_input
            ),
            **case.extra_add_steps,
            **(extra_inputs or {}),
        }
        result, steps = await drive(hass, manager, result, inputs)
        # _save_and_finish reloads the entry in a background task.
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY, (steps, result)
        return Run(result, steps, entry)

    # -- path c: config flow, reconfigure --

    async def reconfigure(
        self,
        case: BrandCase,
        entry: MockConfigEntry | None = None,
        *,
        extra_inputs: dict[str, Any] | None = None,
        connection_input: dict | None = None,
    ) -> Run:
        """``reconfigure`` -> ``reconfigure_battery[_<brand>]`` [-> ...] -> abort(reconfigure_successful)."""
        hass = self.hass
        if entry is None:
            entry = self.make_entry([dict(case.existing)])
        manager = hass.config_entries.flow
        result = await manager.async_init(
            DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        inputs: dict[str, Any] = {
            case.reconfigure_step: connection_input or case.reconfigure_input,
            **case.extra_reconfigure_steps,
            **(extra_inputs or {}),
        }
        result, steps = await drive(hass, manager, result, inputs)
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.ABORT, (steps, result)
        assert result["reason"] == "reconfigure_successful", (steps, result)
        return Run(result, steps, entry)


@pytest.fixture
def probes() -> Probes:
    return Probes()


@pytest.fixture
def flow_env(hass, enable_custom_integrations, monkeypatch, probes):
    """The real flows on a real ``hass``, with every probe scripted and setup patched out.

    ``async_setup_entry`` / ``async_unload_entry`` of the integration are replaced
    so creating, updating or reloading an entry never connects to a battery.
    """
    probes.install(monkeypatch)
    # manifest.json depends on http/frontend/panel_custom; the test venv has no
    # frontend package, so mark them as already set up instead of loading them.
    hass.config.components.update({"http", "frontend", "panel_custom"})
    with patch(
        "custom_components.omnibattery.async_setup_entry", return_value=True
    ), patch(
        "custom_components.omnibattery.async_unload_entry", return_value=True
    ):
        yield FlowEnv(hass, probes)


# --------------------------------------------------------------------------- snapshots


def normalize(data: Any) -> Any:
    """JSON-safe, deterministic form of a stored battery dict.

    Mostly a round trip (tuples -> lists, key order is applied at dump time).

    The one scrub: ``generic_ha`` has no device, host or serial to identify a
    battery by -- it is a set of entity ids the user may repoint at will -- so
    its flow mints a uuid4 as the stable ``host``. That id is genuinely random
    per run, and ``device_key`` and every stored value hang off it, so it is
    replaced with a fixed placeholder here rather than in the tests, keeping
    every snapshot on the same treatment.
    """
    scrubbed = json.loads(json.dumps(data, sort_keys=True))
    return _scrub_generated_ids(scrubbed)


_GENERATED_HOST = "<generated>"


def _scrub_generated_ids(value: Any) -> Any:
    """Replace ``generic_ha``'s minted host id wherever it appears."""
    if isinstance(value, list):
        return [_scrub_generated_ids(item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {key: _scrub_generated_ids(item) for key, item in value.items()}
    host = out.get("host")
    if (
        out.get("brand") == "generic_ha"
        and isinstance(host, str)
        and re.fullmatch(r"[0-9a-f]{32}", host)
    ):
        out["host"] = _GENERATED_HOST
    return out


def snapshot_path(name: str) -> Path:
    return SNAPSHOT_DIR / f"{name}.json"


def assert_snapshot(name: str, data: Any) -> None:
    """Compare ``data`` with ``tests/snapshots/config_entry/<name>.json`` exactly.

    A missing file is a failure, unless ``OMNIBATTERY_UPDATE_SNAPSHOTS=1`` is set:
    then the file is (re)written and the comparison is skipped for that call.
    """
    actual = normalize(data)
    path = snapshot_path(name)
    if os.environ.get(UPDATE_ENV) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(actual, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return
    assert path.exists(), (
        f"missing snapshot {path.name}; review the output and create it with {UPDATE_ENV}=1"
    )
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert actual == expected, f"{path.name} differs from the stored snapshot"
