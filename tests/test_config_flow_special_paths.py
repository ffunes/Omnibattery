"""Special config-flow paths, pinned as they are today (driver-registry T1.2, part 2/2).

Part 1 (``test_config_entry_snapshots.py``) snapshots the stored battery per brand
on the straight add / options / reconfigure paths. This file covers what those do
not reach, all through the REAL ``MarstekVenusConfigFlow`` / ``OptionsFlowHandler``:

1. Huawei in two steps (cascade -> slave picker) in the config flow, the options
   flow and reconfigure, including the back-jump on a serial mismatch.
2. Indevolt with and without the ``indevolt_mode_notice`` step; reconfigure in two
   steps with the registry migration deferred to the second one.
3. DHCP (``async_step_dhcp``) for the five IP-addressed brands: what is re-probed
   and what is stored.
4. Restoring an old backup (historic keys, a battery without ``brand``).
5. An error form per brand keeps the submitted values (what the
   ``_preserve_values_in_error_forms`` decorator guarantees), in both flows.
6. The drifts of plan section 2.3, frozen as they are (see the notes in each test).

Anything that looks like a defect is characterised, not fixed; the plan's section 8
lists them. Run with the HA plugin (the plain ``pytest`` skips these)::

    .venv-test/Scripts/python -m pytest -q -o addopts="" tests/test_config_flow_special_paths.py

Regenerate snapshots (then review the diff by eye)::

    OMNIBATTERY_UPDATE_SNAPSHOTS=1 .venv-test/Scripts/python -m pytest -q -o addopts="" tests/test_config_flow_special_paths.py
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_DHCP, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omnibattery.config_backup import BACKUP_KEY
from tests.flow_harness import (  # noqa: F401  (fixtures are used by name)
    CASES,
    DOMAIN,
    ENTRY_VERSION,
    SYSTEM_INPUT,
    assert_snapshot,
    drive,
    flow_env,
    probes,
)

SNAP = "special"  # snapshot prefix of this file

# Two inverters carrying a battery on the same bus: a Huawei cascade.
CASCADE = [(1, "SUN2000-5KTL-L1", True), (2, "SUN2000-8KTL-M1", True)]
CASCADE_PROBE = (True, "SUN2000-8KTL-M1", 8000, 7000, "HV0000000002", 10000)


def seq(*inputs):
    """A ``drive`` input that answers successive visits of one step with ``inputs``."""
    queue = list(inputs)
    return lambda _result: queue.pop(0)


# --------------------------------------------------------------------------- helpers


class FakeDriver:
    """Records the close/connect a probe puts the live coordinator through."""

    def __init__(self, probes) -> None:
        self._probes = probes

    async def close(self) -> None:
        self._probes._record("coordinator.close")

    async def connect(self) -> bool:
        self._probes._record("coordinator.connect")
        return True


class FakeCoordinator:
    """Just what the flows read from a live coordinator in ``hass.data``."""

    def __init__(self, probes, *, host, port=502, slave_id=1, brand="marstek",
                 battery_version="v3", is_available=True, data=None) -> None:
        self.driver = FakeDriver(probes)
        self.lock = asyncio.Lock()
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.brand = brand
        self.battery_version = battery_version
        self.is_available = is_available
        self.data = data or {}


def install_coordinator(hass, entry, coordinator) -> None:
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinators": [coordinator]}


def seed_registry(hass, entry, device_key: str) -> None:
    """One entity and one device named after ``device_key``, as a loaded entry leaves them."""
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{device_key}_soc", config_entry=entry,
        suggested_object_id="battery_soc",
    )
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, device_key)}, name="Battery",
    )


def registry_state(hass, entry) -> dict:
    ents = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    devs = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return {
        "entity_unique_ids": sorted(e.unique_id for e in ents),
        "device_identifiers": sorted(
            sorted(i[1] for i in d.identifiers if i[0] == DOMAIN) for d in devs
        ),
    }


def device_key(battery: dict) -> str:
    slave = battery.get("slave_id", 1)
    base = f"{battery['host']}_{battery['port']}"
    return base if slave == 1 else f"{base}_{slave}"


async def open_config_flow(env, case, *, stop_at):
    """Config flow add path, stopped on the form of ``stop_at`` (not submitted)."""
    manager = env.hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": SOURCE_USER})
    result, _ = await drive(env.hass, manager, result, {
        "user": dict(SYSTEM_INPUT),
        "batteries": {"num_batteries": 1},
        "battery_brand": {"brand": case.brand},
    }, stop_at=stop_at)
    assert result["step_id"] == stop_at, result
    return manager, result


async def open_options_flow(env, case, *, stop_at):
    """Options flow on an entry holding ``case.existing``, stopped on ``stop_at``."""
    entry = env.make_entry([dict(case.existing)])
    manager = env.hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result, _ = await drive(env.hass, manager, result, {
        "init": "batteries",
        "battery_count": {"num_batteries": 1},
        "battery_brand": {"brand": case.brand},
    }, stop_at=stop_at)
    assert result["step_id"] == stop_at, result
    return manager, result


def shown_values(result) -> dict:
    """The value each field of a form would show: suggested value first, else its default."""
    shown = {}
    for marker in result["data_schema"].schema:
        description = marker.description if isinstance(marker.description, dict) else {}
        if "suggested_value" in description:
            shown[str(marker.schema)] = description["suggested_value"]
        elif marker.default is not vol.UNDEFINED:
            shown[str(marker.schema)] = marker.default()
    return shown


# --------------------------------------------------------------------------- 1. Huawei


def _cascade(probes) -> None:
    probes.huawei_scan = list(CASCADE)
    probes.huawei_probes = [CASCADE_PROBE]


HUAWEI_CASCADE_CONNECTION_ADD = {"name": "Huawei 1", "host": "192.0.2.70", "port": 502,
                                 "huawei_direct_write": True}  # no slave id: scan
HUAWEI_CASCADE_CONNECTION_OPT = {"name": "Huawei 1 edited", "host": "192.0.2.71", "port": 502,
                                 "huawei_direct_write": True}
HUAWEI_PICK_SLAVE_2 = {"battery_connection_huawei_slave": {"slave_id": "2"}}


async def test_huawei_cascade_config_flow(flow_env, probes):
    """CF add: no slave id -> scan finds two -> slave picker -> limits."""
    _cascade(probes)
    run = await flow_env.add_via_config_flow(
        CASES["huawei"],
        connection_input=HUAWEI_CASCADE_CONNECTION_ADD,
        extra_inputs=HUAWEI_PICK_SLAVE_2,
    )

    assert run.steps == [
        "user", "batteries", "battery_brand", "battery_connection_huawei",
        "battery_connection_huawei_slave", "battery_limits",
        "time_slots", "excluded_devices", "predictive_charging",
    ]
    # No initial probe: the scan found two, so the slave is probed once it is picked.
    assert probes.calls == [
        ("huawei.scan_slave_ids", "192.0.2.70", 502),
        ("huawei.probe", "192.0.2.70", 502, 2),
        ("huawei.find_emma_slave_id", "192.0.2.70", 502),
    ]
    assert_snapshot(f"{SNAP}_huawei_cascade_add", run.batteries[0])


async def test_huawei_cascade_options_flow(flow_env, probes):
    """OF: same two steps, the form is prefilled from the stored battery."""
    _cascade(probes)
    case = CASES["huawei"]
    run = await flow_env.edit_via_options_flow(
        case,
        connection_input=HUAWEI_CASCADE_CONNECTION_OPT,
        extra_inputs=HUAWEI_PICK_SLAVE_2,
    )

    assert run.steps == [
        "init", "battery_count", "battery_brand", "battery_connection_huawei",
        "battery_connection_huawei_slave", "battery_limits",
    ]
    assert probes.calls == [
        ("huawei.scan_slave_ids", "192.0.2.71", 502),
        ("huawei.probe", "192.0.2.71", 502, 2),
        ("huawei.find_emma_slave_id", "192.0.2.71", 502),
    ]
    assert_snapshot(f"{SNAP}_huawei_cascade_options", run.batteries[0])


async def test_huawei_cascade_reconfigure(flow_env, probes):
    """CF reconfigure: scan finds two -> slave picker -> stored, registry migrated."""
    _cascade(probes)
    case = CASES["huawei"]
    entry = flow_env.make_entry([dict(case.existing)])
    seed_registry(flow_env.hass, entry, device_key(case.existing))

    run = await flow_env.reconfigure(
        case, entry,
        connection_input={"name": "Huawei 1 moved", "host": "192.0.2.72", "port": 502,
                          "huawei_direct_write": True},
        extra_inputs={"reconfigure_battery_huawei_slave": {"slave_id": "2"}},
    )

    assert run.steps == ["reconfigure_battery_huawei", "reconfigure_battery_huawei_slave"]
    assert_snapshot(f"{SNAP}_huawei_cascade_reconfigure", {
        "battery": run.batteries[0],
        "registry": registry_state(flow_env.hass, entry),
    })


async def test_huawei_reconfigure_serial_mismatch_jumps_back(flow_env, probes):
    """Slave picked on the cascade form does not match the chosen device -> back to the first form."""
    _cascade(probes)
    hass = flow_env.hass
    case = CASES["huawei"]
    entry = flow_env.make_entry([dict(case.existing)])
    huawei_solar = MockConfigEntry(domain="huawei_solar", title="Huawei Solar")
    huawei_solar.add_to_hass(hass)
    # A battery device that hangs off an inverter whose serial is NOT HV0000000002.
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=huawei_solar.entry_id,
        identifiers={("huawei_solar", "HV9999999999")},
        name="Batteries",
    )

    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    steps = [result["step_id"]]
    assert steps == ["reconfigure_battery_huawei"]

    # Service path (no direct write) with a device and no slave id -> cascade.
    result = await manager.async_configure(result["flow_id"], {
        "name": "Huawei 1 moved", "host": "192.0.2.72", "port": 502,
        "huawei_direct_write": False, "huawei_battery_device": device.id,
    })
    steps.append(result["step_id"])
    assert steps[-1] == "reconfigure_battery_huawei_slave"

    # Picking inverter 2 (serial HV0000000002) contradicts the device -> back.
    result = await manager.async_configure(result["flow_id"], {"slave_id": "2"})
    steps.append(result["step_id"])
    assert result["type"] is FlowResultType.FORM
    assert steps[-1] == "reconfigure_battery_huawei"
    assert result["errors"] == {"huawei_battery_device": "huawei_device_mismatch"}
    # The stored battery is untouched until a valid pairing is saved.
    assert entry.data["batteries"][0] == case.existing

    # The user gives up the device and uses direct writes with an explicit slave.
    result = await manager.async_configure(result["flow_id"], {
        "name": "Huawei 1 moved", "host": "192.0.2.72", "port": 502,
        "slave_id": 3, "huawei_direct_write": True,
    })
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    assert steps == [
        "reconfigure_battery_huawei", "reconfigure_battery_huawei_slave",
        "reconfigure_battery_huawei",
    ]
    assert_snapshot(f"{SNAP}_huawei_reconfigure_mismatch_back", entry.data["batteries"][0])


# --------------------------------------------------------------------------- 2. Indevolt


def _notice_case():
    return CASES["indevolt"]


async def test_indevolt_mode_notice_config_flow(flow_env, probes):
    """Not in Realtime mode: an extra notice step sits between connection and speed."""
    probes.indevolt_info["realtime_mode"] = False
    case = _notice_case()
    run = await flow_env.add_via_config_flow(case)

    assert run.steps == [
        "user", "batteries", "battery_brand", "battery_connection_indevolt",
        "indevolt_mode_notice", "indevolt_request_speed", "battery_limits",
        "time_slots", "excluded_devices", "predictive_charging",
    ]
    assert_snapshot(f"{SNAP}_indevolt_notice_add", run.batteries[0])


async def test_indevolt_mode_notice_options_flow(flow_env, probes):
    probes.indevolt_info["realtime_mode"] = False
    run = await flow_env.edit_via_options_flow(_notice_case())

    assert run.steps == [
        "init", "battery_count", "battery_brand", "battery_connection_indevolt",
        "indevolt_mode_notice", "indevolt_request_speed", "battery_limits",
    ]
    assert_snapshot(f"{SNAP}_indevolt_notice_options", run.batteries[0])


async def test_indevolt_realtime_mode_skips_the_notice(flow_env, probes):
    """In Realtime mode (the default of the probes) the notice step never shows."""
    cf = await flow_env.add_via_config_flow(_notice_case())
    of = await flow_env.edit_via_options_flow(_notice_case())
    assert "indevolt_mode_notice" not in cf.steps
    assert "indevolt_mode_notice" not in of.steps


async def test_indevolt_reconfigure_defers_the_migration_to_the_second_step(flow_env, probes):
    """Host change: nothing is renamed or stored on step one; both happen on step two."""
    hass = flow_env.hass
    case = CASES["indevolt"]
    entry = flow_env.make_entry([dict(case.existing)])
    seed_registry(hass, entry, device_key(case.existing))
    manager = hass.config_entries.flow

    result = await manager.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["step_id"] == "reconfigure_battery_indevolt"
    result = await manager.async_configure(result["flow_id"], case.reconfigure_input)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_battery_indevolt_request_speed"

    after_step_one = {
        "battery": dict(entry.data["batteries"][0]),
        "registry": registry_state(hass, entry),
    }
    assert after_step_one["battery"] == case.existing
    assert after_step_one["registry"]["entity_unique_ids"] == ["192.0.2.60_8080_soc"]

    result = await manager.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    assert_snapshot(f"{SNAP}_indevolt_reconfigure_deferred", {
        "after_step_one": after_step_one,
        "after_step_two": {
            "battery": entry.data["batteries"][0],
            "registry": registry_state(hass, entry),
        },
    })


async def test_indevolt_reconfigure_abandoned_on_step_two_changes_nothing(flow_env, probes):
    hass = flow_env.hass
    case = CASES["indevolt"]
    entry = flow_env.make_entry([dict(case.existing)])
    seed_registry(hass, entry, device_key(case.existing))
    manager = hass.config_entries.flow

    result = await manager.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await manager.async_configure(result["flow_id"], case.reconfigure_input)
    assert result["step_id"] == "reconfigure_battery_indevolt_request_speed"
    manager.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    assert entry.data["batteries"][0] == case.existing
    assert registry_state(hass, entry)["entity_unique_ids"] == ["192.0.2.60_8080_soc"]


# --------------------------------------------------------------------------- 3. DHCP

IP_BASED_BRANDS = frozenset({"marstek", "zendure", "anker", "sessy", "indevolt"})
MAC = "aa:bb:cc:dd:ee:ff"
NEW_IP = "192.0.2.99"
DHCP_CASE_IDS = [
    case_id for case_id, case in CASES.items() if case.brand in IP_BASED_BRANDS
]


def test_dhcp_covers_every_ip_based_brand():
    assert IP_BASED_BRANDS == frozenset({"marstek", "zendure", "anker", "sessy", "indevolt"})
    assert {CASES[c].brand for c in DHCP_CASE_IDS} == set(IP_BASED_BRANDS)


def _tracked(case) -> dict:
    return {**case.existing, "track_mac": True, "mac": MAC}


async def _dhcp(env, battery: dict):
    entry = env.make_entry([battery])
    info = SimpleNamespace(ip=NEW_IP, hostname="battery", macaddress=MAC.replace(":", ""))
    result = await env.hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=info
    )
    await env.hass.async_block_till_done()
    return entry, result


@pytest.mark.parametrize("case_id", DHCP_CASE_IDS)
async def test_dhcp_moves_the_battery_after_reprobing_its_brand(flow_env, probes, case_id):
    """A tracked battery that went quiet is probed with its OWN driver at the new address."""
    case = CASES[case_id]
    hass = flow_env.hass
    battery = _tracked(case)
    entry = flow_env.make_entry([battery])
    seed_registry(hass, entry, device_key(battery))
    info = SimpleNamespace(ip=NEW_IP, hostname="battery", macaddress=MAC.replace(":", ""))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=info
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "ip_updated"
    stored = entry.data["batteries"][0]
    assert stored["host"] == NEW_IP
    assert {k: v for k, v in stored.items() if k != "host"} == {
        k: v for k, v in battery.items() if k != "host"
    }
    assert_snapshot(f"{SNAP}_dhcp_{case_id}", {
        "reason": result["reason"],
        "probe_calls": probes.calls,
        "battery": stored,
        "registry": registry_state(hass, entry),
    })


@pytest.mark.parametrize("case_id", DHCP_CASE_IDS)
async def test_dhcp_candidate_that_stays_silent_changes_nothing(flow_env, probes, case_id):
    case = CASES[case_id]
    brand = case.brand
    if brand == "indevolt":
        probes.indevolt_info = None
    else:
        setattr(probes, f"ok_{brand}", False)
    battery = _tracked(case)
    entry, result = await _dhcp(flow_env, battery)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "candidate_silent"
    assert entry.data["batteries"][0] == battery
    assert len(probes.calls) == 1  # one probe, of the stored brand only


@pytest.mark.parametrize("case_id", ["esphome", "huawei", "hoymiles"])
async def test_dhcp_ignores_brands_that_are_not_ip_addressed(flow_env, probes, case_id):
    battery = _tracked(CASES[case_id])
    entry, result = await _dhcp(flow_env, battery)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_tracked_battery"
    assert entry.data["batteries"][0] == battery
    assert probes.calls == []


async def test_dhcp_ignores_a_marstek_on_a_serial_port(flow_env, probes):
    battery = {**_tracked(CASES["marstek"]), "serial_port": "/dev/ttyUSB0"}
    entry, result = await _dhcp(flow_env, battery)

    assert result["reason"] == "no_tracked_battery"
    assert entry.data["batteries"][0] == battery
    assert probes.calls == []


# --------------------------------------------------------------------------- 4. backup

OLD_BACKUP_BATTERIES = [
    # Historic Marstek entry: no `brand`, no `slave_id`, no `serial_port`, an
    # option nothing reads any more, and the hysteresis flag turned off.
    {
        "name": "Old Venus",
        "host": "192.0.2.5",
        "port": 502,
        "battery_version": "v2",
        "max_charge_power": 2500,
        "max_discharge_power": 2500,
        "max_soc": 100,
        "min_soc": 12,
        "enable_charge_hysteresis": False,
        "charge_hysteresis_percent": 3,
        "retired_option": "kept",
    },
    # Same record, a brand that was added later: its keys survive untouched too.
    {
        "name": "Old Zendure",
        "brand": "zendure",
        "host": "192.0.2.6",
        "port": 80,
        "zendure_model": "2400ac_pro",
        "max_charge_power": 1200,
        "max_discharge_power": 1200,
        "max_soc": 100,
        "min_soc": 10,
    },
]


async def test_restore_backup_keeps_historic_keys_and_does_not_add_a_brand(flow_env):
    hass = flow_env.hass
    record = {
        "domain": DOMAIN,
        "entry_id": "old-entry-id",
        "version": ENTRY_VERSION,
        "minor_version": 1,
        "title": "Omnibattery",
        "data": {**SYSTEM_INPUT, "num_batteries": 2,
                 "batteries": [dict(b) for b in OLD_BACKUP_BATTERIES]},
        "options": {"pd_controller_kp": 0.42},
        "unique_id": None,
        "source": "user",
    }
    await Store(hass, 1, BACKUP_KEY).async_save({"entries": [record]})
    assert hass.config_entries.async_entries(DOMAIN) == []

    manager = hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "restore_backup"
    result = await manager.async_configure(result["flow_id"], {"restore": True})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "restore_successful"
    (restored,) = hass.config_entries.async_entries(DOMAIN)
    batteries = restored.data["batteries"]
    assert batteries == OLD_BACKUP_BATTERIES
    assert "brand" not in batteries[0]
    assert dict(restored.options) == {"pd_controller_kp": 0.42}
    assert_snapshot(f"{SNAP}_restore_backup_old", batteries)


# --------------------------------------------------------------------------- 5. error forms


def _break_probe(probes, brand: str) -> None:
    """Make the connection probe of ``brand`` fail the way a dead device does."""
    if brand == "esphome":
        probes.esphome_missing = ["sensor.lilygo_soc"]
    elif brand == "indevolt":
        probes.indevolt_info = None
    elif brand == "huawei":
        probes.huawei_probes = [(False, None, None, None, None, None)]
        probes.huawei_scan = []
    else:
        setattr(probes, f"ok_{brand}", False)


def _assert_form_kept_the_values(result, case, submitted: dict) -> None:
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == case.connection_step
    assert result["errors"], result
    shown = shown_values(result)
    for key, value in submitted.items():
        assert key in shown, f"{key} missing from the error form of {case.id}"
        assert shown[key] == value, (key, shown[key], value)


@pytest.mark.parametrize("brand", ["marstek", "zendure", "esphome", "anker", "sessy",
                                   "indevolt", "huawei", "hoymiles"])
async def test_error_form_keeps_the_submitted_values_config_flow(flow_env, probes, brand):
    case = CASES[brand]
    _break_probe(probes, brand)
    manager, result = await open_config_flow(flow_env, case, stop_at=case.connection_step)

    result = await manager.async_configure(result["flow_id"], case.add_input)

    _assert_form_kept_the_values(result, case, case.add_input)


@pytest.mark.parametrize("brand", ["marstek", "zendure", "esphome", "anker", "sessy",
                                   "indevolt", "huawei", "hoymiles"])
async def test_error_form_keeps_the_submitted_values_options_flow(flow_env, probes, brand):
    case = CASES[brand]
    _break_probe(probes, brand)
    manager, result = await open_options_flow(flow_env, case, stop_at=case.connection_step)
    # The form opens on the STORED values: the submitted ones differ from them.
    assert shown_values(result)["name"] == case.existing["name"] != case.options_input["name"]

    result = await manager.async_configure(result["flow_id"], case.options_input)

    _assert_form_kept_the_values(result, case, case.options_input)


# --------------------------------------------------------------------------- 6a. brand change in OF


BRAND_CHANGES = [
    ("marstek_vd", "zendure"),
    ("huawei", "marstek"),
    ("zendure", "marstek"),
    ("marstek", "huawei"),
]


@pytest.mark.parametrize("old_id,new_id", BRAND_CHANGES)
async def test_options_brand_change_keeps_the_previous_brands_keys(flow_env, old_id, new_id):
    """OF starts from the stored battery and lays the new data over it.

    Nothing of the old brand is removed, so its keys survive the change.
    """
    old, new = CASES[old_id], CASES[new_id]
    entry = flow_env.make_entry([dict(old.existing)])
    run = await flow_env.edit_via_options_flow(new, entry, brand=new.brand)

    stored = run.batteries[0]
    assert stored["brand"] == new.brand
    survivors = {
        key: value for key, value in old.existing.items()
        if key.startswith(("huawei_", "zendure_", "hoymiles_"))
        or key in ("battery_version", "slave_id", "serial_port", "ems_version",
                   "rs485_gateway", "esphome_device_id", "user_max_charge_power")
    }
    survivors = {k: v for k, v in survivors.items() if k not in new.options_input}
    for key, value in survivors.items():
        if key in ("huawei_model", "slave_id", "device_max_charge_power"):
            continue  # overwritten by the new brand's own connection step
        assert stored.get(key) == value, key
    assert_snapshot(f"{SNAP}_drift_options_{old_id}_to_{new_id}", stored)


async def test_options_brand_change_from_marstek_vd_keeps_dc_pv_on_a_non_marstek_battery(flow_env):
    """Fixed: plan section 8 #11 (T1.1): a stale ``battery_version`` no longer counts.

    The Zendure form has no DC-PV field; the stored answer (False) is kept instead of
    the old default True that the stale vD version used to write.
    """
    old = CASES["marstek_vd"]
    assert old.existing["dc_pv_connected"] is True
    entry = flow_env.make_entry([{**old.existing, "dc_pv_connected": False}])
    run = await flow_env.edit_via_options_flow(CASES["zendure"], entry, brand="zendure")

    stored = run.batteries[0]
    assert stored["brand"] == "zendure"
    assert stored["battery_version"] == "vD"
    assert stored["dc_pv_connected"] is False


# --------------------------------------------------------------------------- 6b. registry migration


RECONFIGURE_MIGRATIONS = {
    # name: (case, connection input override or None)
    "anker_slave_only": ("anker", {"name": "Anker 1", "host": "192.0.2.40", "port": 502,
                                   "slave_id": 2}),
    "marstek_slave_only": ("marstek", {"name": "Venus 1", "host": "192.0.2.10", "port": 502,
                                       "slave_id": 2, "battery_version": "v3"}),
    "huawei_slave_only": ("huawei", {"name": "Huawei 1", "host": "192.0.2.70", "port": 502,
                                     "slave_id": 3, "huawei_direct_write": True}),
    "anker_host_and_slave": ("anker", None),
    "marstek_host_and_slave": ("marstek", None),
}


@pytest.mark.parametrize("name", list(RECONFIGURE_MIGRATIONS))
async def test_reconfigure_registry_migration_by_brand(flow_env, name):
    """Anker never migrates a slave-only change; Marstek and Huawei do.

    Anker also builds the new key with the DEFAULT slave on a host change, so a
    battery stored with slave 2 gets an id without the ``_2`` suffix (section 8).
    """
    case_id, connection = RECONFIGURE_MIGRATIONS[name]
    case = CASES[case_id]
    entry = flow_env.make_entry([dict(case.existing)])
    seed_registry(flow_env.hass, entry, device_key(case.existing))

    run = await flow_env.reconfigure(case, entry, connection_input=connection)

    stored = run.batteries[0]
    registry = registry_state(flow_env.hass, entry)
    assert_snapshot(f"{SNAP}_drift_reconfigure_registry_{name}", {
        "stored": {k: stored[k] for k in ("host", "port", "slave_id")},
        "registry": registry,
        "device_key_of_stored_battery": device_key(stored),
    })
    if name == "anker_slave_only":
        assert registry["entity_unique_ids"] == ["192.0.2.40_502_soc"]
    if name in ("marstek_slave_only", "huawei_slave_only"):
        assert registry["entity_unique_ids"] != [f"{device_key(case.existing)}_soc"]


# --------------------------------------------------------------------------- 6c. Marstek probe


async def test_options_flow_closes_the_live_coordinator_around_the_marstek_probe(flow_env, probes):
    case = CASES["marstek"]
    entry = flow_env.make_entry([dict(case.existing)])
    install_coordinator(
        flow_env.hass, entry,
        FakeCoordinator(probes, host=case.options_input["host"], slave_id=1),
    )

    await flow_env.edit_via_options_flow(case, entry)

    assert [c[0] for c in probes.calls] == [
        "coordinator.close", "marstek.probe", "coordinator.connect",
    ]


async def test_options_flow_reopens_the_coordinator_when_the_marstek_probe_fails(flow_env, probes):
    case = CASES["marstek"]
    probes.ok_marstek = False
    entry = flow_env.make_entry([dict(case.existing)])
    install_coordinator(
        flow_env.hass, entry,
        FakeCoordinator(probes, host=case.options_input["host"], slave_id=1),
    )
    manager = flow_env.hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result, _ = await drive(flow_env.hass, manager, result, {
        "init": "batteries", "battery_count": {"num_batteries": 1},
        "battery_brand": {"brand": "marstek"},
    }, stop_at="battery_connection")
    result = await manager.async_configure(result["flow_id"], case.options_input)

    assert result["errors"] == {"base": "cannot_connect"}
    assert [c[0] for c in probes.calls] == [
        "coordinator.close", "marstek.probe", "coordinator.connect",
    ]


async def test_options_flow_probes_without_a_matching_coordinator_as_is(flow_env, probes):
    """A coordinator on another host is left alone: the probe just runs."""
    case = CASES["marstek"]
    entry = flow_env.make_entry([dict(case.existing)])
    install_coordinator(flow_env.hass, entry, FakeCoordinator(probes, host="192.0.2.250"))

    await flow_env.edit_via_options_flow(case, entry)

    assert [c[0] for c in probes.calls] == ["marstek.probe"]


async def test_config_flow_reconfigure_never_touches_the_live_coordinator(flow_env, probes):
    case = CASES["marstek"]
    entry = flow_env.make_entry([dict(case.existing)])
    install_coordinator(
        flow_env.hass, entry,
        FakeCoordinator(probes, host=case.reconfigure_input["host"], slave_id=1),
    )

    await flow_env.reconfigure(case, entry)

    assert [c[0] for c in probes.calls] == ["marstek.probe"]


async def test_config_flow_add_never_touches_a_live_coordinator(flow_env, probes):
    case = CASES["marstek"]
    stray = FakeCoordinator(probes, host=case.add_input["host"], slave_id=1)
    flow_env.hass.data.setdefault(DOMAIN, {})["other-entry"] = {"coordinators": [stray]}

    await flow_env.add_via_config_flow(case)

    assert [c[0] for c in probes.calls] == ["marstek.probe"]


# --------------------------------------------------------------------------- Anker live coordinator


def _live_anker(probes, case, host):
    return FakeCoordinator(
        probes, host=host, port=502, slave_id=1, brand="anker", is_available=True,
        data={"max_charge_power": 2500, "max_discharge_power": 2600},
    )


async def test_options_flow_anker_reuses_the_live_coordinator_and_skips_the_probe(flow_env, probes):
    case = CASES["anker"]
    entry = flow_env.make_entry([dict(case.existing)])
    install_coordinator(
        flow_env.hass, entry, _live_anker(probes, case, case.options_input["host"])
    )

    run = await flow_env.edit_via_options_flow(case, entry)

    assert probes.calls_named("anker.probe") == []
    stored = run.batteries[0]
    # The capabilities come from the coordinator's telemetry, not from a probe.
    assert (stored["device_max_charge_power"], stored["device_max_discharge_power"]) == (2500, 2600)
    assert_snapshot(f"{SNAP}_drift_options_anker_live_coordinator", stored)


async def test_reconfigure_anker_reuses_the_live_coordinator_and_skips_the_probe(flow_env, probes):
    case = CASES["anker"]
    entry = flow_env.make_entry([dict(case.existing)])
    install_coordinator(
        flow_env.hass, entry, FakeCoordinator(
            probes, host=case.reconfigure_input["host"], port=502,
            slave_id=case.reconfigure_input["slave_id"], brand="anker",
        )
    )

    await flow_env.reconfigure(case, entry)

    assert probes.calls_named("anker.probe") == []


async def test_config_flow_add_anker_always_probes(flow_env, probes):
    """The add flow calls the driver probe directly: no coordinator to reuse."""
    case = CASES["anker"]
    await flow_env.add_via_config_flow(case)

    assert [c[0] for c in probes.calls] == ["anker.probe"]


# --------------------------------------------------------------------------- 6d. OF never migrates


@pytest.mark.parametrize("case_id", ["marstek", "zendure", "anker", "sessy"])
async def test_options_flow_host_change_never_migrates_the_registry(flow_env, case_id):
    """Same host change as the reconfigure path, but the ids keep the old address."""
    case = CASES[case_id]
    old_key = device_key(case.existing)
    entry = flow_env.make_entry([dict(case.existing)])
    seed_registry(flow_env.hass, entry, old_key)

    run = await flow_env.edit_via_options_flow(case, entry)

    stored = run.batteries[0]
    assert stored["host"] == case.options_input["host"] != case.existing["host"]
    registry = registry_state(flow_env.hass, entry)
    assert registry == {
        "entity_unique_ids": [f"{old_key}_soc"],
        "device_identifiers": [[old_key]],
    }
    assert_snapshot(f"{SNAP}_drift_options_host_change_{case_id}", {
        "stored_host": stored["host"],
        "registry": registry,
        "device_key_of_stored_battery": device_key(stored),
    })


# --------------------------------------------------------------------------- observed battery: mode entity


async def test_generic_ha_mode_entity_through_the_three_flows(flow_env):
    """Add with a mode entity, edit it keeping the preference, then clear it."""
    case = CASES["generic_ha"]
    flow_env.hass.states.async_set(
        "select.byd_mode", "auto", {"options": ["auto", "hold", "backup"]}
    )
    mode_step = {
        "battery_mode_generic_ha": {
            "ha_mode_nom_option": "auto", "ha_mode_manual_option": "hold",
        }
    }
    added = await flow_env.add_via_config_flow(
        case,
        connection_input={**case.add_input, "ha_mode_entity": "select.byd_mode"},
        extra_inputs=mode_step,
    )
    battery = added.batteries[0]
    assert "battery_mode_generic_ha" in added.steps
    assert battery["ha_mode_entity"] == "select.byd_mode"
    assert (battery["ha_mode_nom_option"], battery["ha_mode_manual_option"]) == ("auto", "hold")
    assert battery["autonomous_requested_mode"] == "manual"

    # The user has since picked NOM with the battery's select.
    battery = {**battery, "autonomous_requested_mode": "nom"}
    edited = await flow_env.edit_via_options_flow(
        case,
        flow_env.make_entry([battery]),
        connection_input={**case.options_input, "ha_mode_entity": "select.byd_mode"},
        extra_inputs=mode_step,
    )
    assert edited.batteries[0]["autonomous_requested_mode"] == "nom"
    assert edited.batteries[0]["ha_mode_nom_option"] == "auto"

    moved = await flow_env.reconfigure(
        case,
        flow_env.make_entry([battery]),
        extra_inputs={
            case.reconfigure_step: {**case.reconfigure_input, "ha_mode_entity": "select.byd_mode"},
            **mode_step,
        },
    )
    assert "battery_mode_generic_ha" in moved.steps
    assert moved.batteries[0]["autonomous_requested_mode"] == "nom"

    cleared = await flow_env.edit_via_options_flow(case, flow_env.make_entry([battery]))
    assert "battery_mode_generic_ha" not in cleared.steps
    assert cleared.batteries[0]["ha_mode_entity"] is None
