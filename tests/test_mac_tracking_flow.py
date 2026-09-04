"""Flow-level tests for the DHCP step that applies a MAC-tracked address change.

The decision guards are unit-tested in ``test_mac_tracking``; what is checked
here is the wiring: that a refused verdict leaves the config entry strictly
alone, that an accepted one rewrites the host *and* goes through the existing
registry migration, and that the migration keeps the entity ids — which is what
preserves history and long-term statistics.

Written against small stubs rather than the ``hass`` fixture: ``pytest.ini``
disables the Home Assistant plugin and CI runs a bare ``pytest``, so a
fixture-based test would be skipped everywhere.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from custom_components.omnibattery import config_flow as cf
from custom_components.omnibattery.infra.mac_tracking import CONF_MAC, CONF_TRACK_MAC

MAC_A = "dc:04:5a:14:6b:33"
MAC_B = "dc:04:5a:7d:de:c6"


# --- stubs ------------------------------------------------------------------


class FakeEntry:
    def __init__(self, batteries, entry_id="entry1", title="Omnibattery"):
        self.entry_id = entry_id
        self.title = title
        self.data = {"batteries": batteries, "consumption_sensor": "sensor.grid"}


class FakeConfigEntries:
    def __init__(self, entries):
        self._entries = entries
        self.updated = []
        self.reloaded = []

    def async_entries(self, domain):
        return list(self._entries)

    def async_update_entry(self, entry, data=None, **kwargs):
        entry.data = data
        self.updated.append((entry.entry_id, data))
        return True

    async def async_reload(self, entry_id):
        self.reloaded.append(entry_id)
        return True


class FakeHass:
    def __init__(self, entries, coordinators=None):
        self.config_entries = FakeConfigEntries(entries)
        self.data = {cf.DOMAIN: {"entry1": {"coordinators": coordinators or []}}}


def battery(host="192.168.1.181", mac=MAC_A, track=True, name="Marstek Venus 2"):
    return {
        "name": name,
        "host": host,
        "port": 502,
        "slave_id": 1,
        "brand": "marstek",
        "serial_port": "",
        CONF_TRACK_MAC: track,
        CONF_MAC: mac,
    }


def make_flow(hass, migrations, candidate_answers=True):
    """A config flow instance wired to ``hass``, recording registry migrations.

    The candidate probe is stubbed by default: these tests are about which
    verdict the flow acts on, and a real probe would reach for the network. The
    probe itself is exercised further down against fake drivers.
    """
    flow = cf.MarstekVenusConfigFlow()
    flow.hass = hass
    flow._migrate_battery_registry_ids = lambda *args: migrations.append(args)

    async def _probe(entry, index, battery, host):
        return candidate_answers

    flow._async_probe_candidate = _probe
    return flow



def make_flow_probing_for_real(hass, migrations=None):
    """Same, but keeping the real candidate probe, aimed at stubbed drivers."""
    flow = cf.MarstekVenusConfigFlow()
    flow.hass = hass
    flow._migrate_battery_registry_ids = lambda *args: (
        migrations.append(args) if migrations is not None else None
    )
    return flow


def lease(mac, ip):
    return types.SimpleNamespace(macaddress=mac, ip=ip)


# --- the opt-in is off: nothing at all happens ------------------------------


async def test_disabled_tracking_leaves_the_entry_untouched():
    hass = FakeHass([FakeEntry([battery(track=False)])])
    migrations: list = []
    flow = make_flow(hass, migrations)

    reason = await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.180")

    assert reason == "no_tracked_battery"
    assert hass.config_entries.updated == []
    assert hass.config_entries.reloaded == []
    assert migrations == []


async def test_an_unknown_mac_leaves_the_entry_untouched():
    hass = FakeHass([FakeEntry([battery(mac=MAC_A)])])
    migrations: list = []
    flow = make_flow(hass, migrations)

    assert await flow._async_apply_dhcp_lease("aa:bb:cc:dd:ee:ff", "192.168.1.180") == "no_tracked_battery"
    assert hass.config_entries.updated == []


# --- the nominal move -------------------------------------------------------


async def test_a_tracked_battery_is_moved_and_the_entry_reloaded():
    entry = FakeEntry([battery(host="192.168.1.181")])
    hass = FakeHass([entry])
    migrations: list = []
    flow = make_flow(hass, migrations)

    reason = await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.180")

    assert reason == "ip_updated"
    assert entry.data["batteries"][0]["host"] == "192.168.1.180"
    assert hass.config_entries.reloaded == [entry.entry_id]
    # Other keys of the battery survive untouched.
    assert entry.data["batteries"][0][CONF_MAC] == MAC_A
    assert entry.data["consumption_sensor"] == "sensor.grid"


async def test_the_registry_migration_is_called_with_the_old_and_new_endpoint():
    """This call is what keeps entity ids, history and statistics."""
    entry = FakeEntry([battery(host="192.168.1.181")])
    hass = FakeHass([entry])
    migrations: list = []
    flow = make_flow(hass, migrations)

    await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.180")

    assert len(migrations) == 1
    called_entry, old_host, old_port, new_host, new_port, old_slave, new_slave = migrations[0]
    assert called_entry is entry
    assert (old_host, old_port) == ("192.168.1.181", 502)
    assert (new_host, new_port) == ("192.168.1.180", 502)
    assert old_slave == new_slave == 1


async def test_only_the_matching_battery_moves():
    entry = FakeEntry([battery(host="192.168.1.64", mac=MAC_B), battery(host="192.168.1.181", mac=MAC_A)])
    hass = FakeHass([entry])
    flow = make_flow(hass, [])

    await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.180")

    assert entry.data["batteries"][0]["host"] == "192.168.1.64"
    assert entry.data["batteries"][1]["host"] == "192.168.1.180"


# --- guards seen from the flow ---------------------------------------------


async def test_a_battery_still_answering_is_left_where_it_is():
    """A device holding two addresses must not be pulled off a working one."""
    entry = FakeEntry([battery(host="192.168.1.181")])
    live = types.SimpleNamespace(is_available=True)
    hass = FakeHass([entry], coordinators=[live])
    flow = make_flow(hass, [])

    assert await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.180") == "no_tracked_battery"
    assert entry.data["batteries"][0]["host"] == "192.168.1.181"
    assert hass.config_entries.updated == []


async def test_a_quiet_battery_is_moved():
    entry = FakeEntry([battery(host="192.168.1.181")])
    quiet = types.SimpleNamespace(is_available=False)
    hass = FakeHass([entry], coordinators=[quiet])
    flow = make_flow(hass, [])

    assert await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.180") == "ip_updated"


async def test_a_renewed_lease_for_the_current_address_does_not_reload():
    entry = FakeEntry([battery(host="192.168.1.181")])
    hass = FakeHass([entry])
    flow = make_flow(hass, [])

    assert await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.181") == "no_tracked_battery"
    assert hass.config_entries.reloaded == []


async def test_a_shared_gateway_mac_moves_nothing():
    entry = FakeEntry(
        [
            battery(host="192.168.1.50", mac=MAC_A) | {"slave_id": 1},
            battery(host="192.168.1.50", mac=MAC_A) | {"slave_id": 2},
        ]
    )
    hass = FakeHass([entry])
    flow = make_flow(hass, [])

    assert await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.51") == "no_tracked_battery"
    assert hass.config_entries.updated == []


# --- the candidate address has to answer ------------------------------------


class FakeDriver:
    """Records the close/connect sequence a probe puts the coordinator through."""

    def __init__(self):
        self.calls: list[str] = []

    async def close(self):
        self.calls.append("close")

    async def connect(self):
        self.calls.append("connect")
        return True


class FakeCoordinator:
    def __init__(self, available=False):
        self.is_available = available
        self.lock = asyncio.Lock()
        self.driver = FakeDriver()


@pytest.fixture
def no_settle_delay(monkeypatch):
    """Skip the settle margins; they exist for the device, not for the test."""

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(cf.asyncio, "sleep", _instant)


def patch_probe(monkeypatch, driver_name, result, calls):
    """Replace one driver's probe, recording what it was asked."""

    async def _probe(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(getattr(cf, driver_name), "probe", _probe)


async def test_a_candidate_that_answers_is_adopted(monkeypatch, no_settle_delay):
    """The nominal case: something is there, and it speaks the battery's protocol."""
    entry = FakeEntry([battery(host="192.168.1.181")])
    hass = FakeHass([entry])
    migrations: list = []
    calls: list = []
    patch_probe(monkeypatch, "MarstekModbusDriver", True, calls)

    flow = make_flow_probing_for_real(hass, migrations)

    assert await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.180") == "ip_updated"
    assert entry.data["batteries"][0]["host"] == "192.168.1.180"
    # Probed at the candidate address, not the one already configured.
    assert calls[0][0][0] == "192.168.1.180"


async def test_a_silent_candidate_changes_nothing(monkeypatch, no_settle_delay):
    """A MAC can cover a bridge and the battery behind it; only one answers."""
    entry = FakeEntry([battery(host="192.168.1.181")])
    hass = FakeHass([entry])
    migrations: list = []
    patch_probe(monkeypatch, "MarstekModbusDriver", False, [])

    flow = make_flow_probing_for_real(hass, migrations)

    assert await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.180") == "candidate_silent"
    assert entry.data["batteries"][0]["host"] == "192.168.1.181"
    assert migrations == []
    assert hass.config_entries.updated == []
    assert hass.config_entries.reloaded == []


async def test_the_connection_is_restored_when_the_candidate_stays_silent(
    monkeypatch, no_settle_delay
):
    """The slot is freed to probe; a failed probe must give it back."""
    entry = FakeEntry([battery(host="192.168.1.181")])
    coordinator = FakeCoordinator(available=False)
    hass = FakeHass([entry], coordinators=[coordinator])
    patch_probe(monkeypatch, "MarstekModbusDriver", False, [])
    flow = make_flow_probing_for_real(hass)

    assert await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.180") == "candidate_silent"
    assert coordinator.driver.calls == ["close", "connect"]
    assert not coordinator.lock.locked()


async def test_a_successful_probe_leaves_the_reload_to_rebuild_the_connection(
    monkeypatch, no_settle_delay
):
    """Reconnecting to the old address would be pointless: the reload follows."""
    entry = FakeEntry([battery(host="192.168.1.181")])
    coordinator = FakeCoordinator(available=False)
    hass = FakeHass([entry], coordinators=[coordinator])
    patch_probe(monkeypatch, "MarstekModbusDriver", True, [])
    flow = make_flow_probing_for_real(hass)

    assert await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.180") == "ip_updated"
    assert coordinator.driver.calls == ["close"]
    assert hass.config_entries.reloaded == [entry.entry_id]


async def test_sessy_is_probed_with_its_stored_credentials(monkeypatch, no_settle_delay):
    """Sessy refuses an unauthenticated probe, which reads exactly like an absent device."""
    sessy = battery(host="192.168.1.181") | {
        "brand": "sessy",
        "port": 80,
        "username": "sessy-user",
        "password": "sessy-pass",
    }
    entry = FakeEntry([sessy])
    hass = FakeHass([entry])
    calls: list = []
    patch_probe(monkeypatch, "SessyLocalDriver", True, calls)
    flow = make_flow_probing_for_real(hass)

    assert await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.180") == "ip_updated"
    assert calls[0][0] == ("192.168.1.180", 80, "sessy-user", "sessy-pass")


@pytest.mark.parametrize(
    "brand,driver_name,tuple_result",
    [
        ("marstek", "MarstekModbusDriver", False),
        ("zendure", "ZendureLocalDriver", True),
        ("anker", "AnkerModbusDriver", True),
        ("sessy", "SessyLocalDriver", False),
    ],
)
async def test_every_ip_based_brand_is_probed_through_its_own_driver(
    monkeypatch, no_settle_delay, brand, driver_name, tuple_result
):
    """The four brands that can drift are the four brands that must be probed."""
    entry = FakeEntry([battery(host="192.168.1.181") | {"brand": brand}])
    hass = FakeHass([entry])
    calls: list = []

    async def _probe(*args, **kwargs):
        calls.append((args, kwargs))
        return (True, None) if tuple_result else True

    monkeypatch.setattr(getattr(cf, driver_name), "probe", _probe)
    flow = make_flow_probing_for_real(hass)

    assert await flow._async_apply_dhcp_lease(MAC_A, "192.168.1.180") == "ip_updated"
    assert len(calls) == 1
    assert calls[0][0][0] == "192.168.1.180"


# --- the migration helper actually preserves entity ids ---------------------


class FakeRegistryEntry:
    def __init__(self, entity_id, unique_id, config_entry_id):
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.config_entry_id = config_entry_id


class FakeEntityRegistry:
    def __init__(self, entries):
        self.entities = {e.entity_id: e for e in entries}
        self.calls: list = []

    def async_update_entity(self, entity_id, **kwargs):
        self.calls.append((entity_id, kwargs))
        if "new_unique_id" in kwargs:
            self.entities[entity_id].unique_id = kwargs["new_unique_id"]


class FakeDeviceRegistry:
    def __init__(self, device):
        self.device = device
        self.updates: list = []

    def async_get_device(self, identifiers=None, **kwargs):
        return self.device if identifiers == self.device.identifiers else None

    def async_update_device(self, device_id, new_identifiers=None, **kwargs):
        self.updates.append((device_id, new_identifiers))


def test_migration_rewrites_unique_ids_but_never_entity_ids(monkeypatch):
    """Long-term statistics follow the entity_id, so it must survive a move."""
    entry = FakeEntry([battery()])
    entities = [
        FakeRegistryEntry("sensor.marstek_venus_2_battery_soc", "192.168.1.181_502_battery_soc", entry.entry_id),
        FakeRegistryEntry("sensor.marstek_venus_2_battery_power", "192.168.1.181_502_battery_power", entry.entry_id),
        FakeRegistryEntry("sensor.other_integration", "somethingelse_soc", "other_entry"),
    ]
    ent_reg = FakeEntityRegistry(entities)
    device = types.SimpleNamespace(id="dev1", identifiers={(cf.DOMAIN, "192.168.1.181_502")})
    dev_reg = FakeDeviceRegistry(device)

    monkeypatch.setattr(cf.er, "async_get", lambda hass: ent_reg)
    monkeypatch.setattr(cf.dr, "async_get", lambda hass: dev_reg)

    flow = cf.MarstekVenusConfigFlow()
    flow.hass = FakeHass([entry])
    flow._migrate_battery_registry_ids(entry, "192.168.1.181", 502, "192.168.1.180", 502, 1, 1)

    # unique_ids re-prefixed onto the new endpoint...
    assert entities[0].unique_id == "192.168.1.180_502_battery_soc"
    assert entities[1].unique_id == "192.168.1.180_502_battery_power"
    # ...an unrelated integration untouched...
    assert entities[2].unique_id == "somethingelse_soc"
    # ...and no call ever asked to change an entity_id.
    assert all("new_entity_id" not in kwargs for _eid, kwargs in ent_reg.calls)
    assert {e.entity_id for e in entities} == {
        "sensor.marstek_venus_2_battery_soc",
        "sensor.marstek_venus_2_battery_power",
        "sensor.other_integration",
    }
    # The device identifier follows the same rename.
    assert dev_reg.updates == [("dev1", {(cf.DOMAIN, "192.168.1.180_502")})]
