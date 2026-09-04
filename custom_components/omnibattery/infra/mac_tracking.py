"""Decide whether a DHCP lease may update a battery's configured IP address.

Batteries reached over TCP are identified in the config entry by their IP
address, and that address is handed out by the user's router. When the router
assigns a different one, the configured endpoint goes stale and the battery
looks unreachable while being perfectly healthy. The failure is quiet: entities
freeze on their last value instead of going unavailable, so a stale endpoint is
hard to tell apart from a battery sitting idle.

Home Assistant already has the plumbing for this — a ``registered_devices``
DHCP matcher delivers a discovery flow whenever a device whose MAC is in the
device registry gets a lease. What it cannot decide is whether acting on that
lease is *safe*. That decision lives here.

Every function in this module is pure: it takes the stored battery list and a
lease, and returns a verdict. Nothing here touches Home Assistant, so the guards
can be tested without an event loop — which is what the rest of this suite does.

The guards exist because a wrong update is worse than no update: pointing
battery A at battery B's endpoint would send A's power commands to B.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

# Brands whose batteries are addressed by IP and can therefore drift.
# Serial (a device path), ESPHome and MQTT (a device id) have no IP to track.
IP_BASED_BRANDS = frozenset({"marstek", "zendure", "anker", "sessy"})

CONF_TRACK_MAC = "track_mac"
CONF_MAC = "mac"

# Verdicts. Everything except OK means "do nothing", and each value is a
# distinct reason so the log line says which guard fired.
OK = "ok"
INVALID_MAC = "invalid_mac"
NO_MATCH = "no_match"
AMBIGUOUS_MAC = "ambiguous_mac"
NOT_IP_BASED = "not_ip_based"
UNCHANGED = "unchanged"
ENDPOINT_CONFLICT = "endpoint_conflict"
STILL_REACHABLE = "still_reachable"

# Decided by the flow rather than by ``evaluate_lease``: answering it means
# talking to the candidate address, and everything in this module is pure.
# It belongs here anyway, with the other reasons, so one place lists them all.
CANDIDATE_SILENT = "candidate_silent"


@dataclass(frozen=True)
class DhcpVerdict:
    """Outcome of evaluating one DHCP lease against the configured batteries."""

    reason: str
    index: int | None = None

    @property
    def should_update(self) -> bool:
        """True only when a single battery was identified and may be moved."""
        return self.reason == OK and self.index is not None


def normalise_mac(raw: Any) -> str | None:
    """Return ``aa:bb:cc:dd:ee:ff`` for anything that looks like a MAC, else None.

    Accepts the shapes users and routers actually produce: colon-separated,
    dash-separated, dot-separated (Cisco style) and bare hex. Home Assistant's
    ``DhcpServiceInfo`` delivers bare lowercase hex, config entries written by
    hand tend to carry colons, so both have to normalise to the same string or
    the lookup silently never matches.
    """
    if not isinstance(raw, str):
        return None
    hex_digits = raw.strip().lower().replace(":", "").replace("-", "").replace(".", "")
    if len(hex_digits) != 12:
        return None
    try:
        int(hex_digits, 16)
    except ValueError:
        return None
    return ":".join(hex_digits[i:i + 2] for i in range(0, 12, 2))


def is_ip_based(battery: dict) -> bool:
    """True when this battery is addressed by IP, so its address can drift.

    A Marstek entry configured over serial stores its device path in ``host``
    and is not IP-based despite the brand being in ``IP_BASED_BRANDS`` — the
    brand alone is not enough to decide.
    """
    if battery.get("brand", "marstek") not in IP_BASED_BRANDS:
        return False
    if (battery.get("serial_port") or "").strip():
        return False
    return bool((battery.get("host") or "").strip())


def tracking_enabled(battery: dict) -> bool:
    """True when the user opted this battery into MAC tracking and gave a MAC.

    Opt-in is per battery and defaults to off, so an install that never touches
    the checkbox behaves exactly as it did before.
    """
    if not battery.get(CONF_TRACK_MAC):
        return False
    return normalise_mac(battery.get(CONF_MAC)) is not None


def endpoint_of(battery: dict, host: str | None = None) -> tuple[str, Any, Any]:
    """Return the (host, port, slave) triple that identifies a battery's endpoint.

    Mirrors ``coordinator.device_key``: batteries behind one Modbus gateway
    share a host and port and are told apart by their slave id, so the slave id
    belongs in the comparison. ``host`` overrides the stored value to test a
    candidate address before committing to it.
    """
    return (
        (host if host is not None else battery.get("host")) or "",
        battery.get("port"),
        battery.get("slave_id"),
    )


def evaluate_lease(
    batteries: Sequence[dict],
    mac: Any,
    new_host: str,
    is_reachable: Callable[[int], bool] | None = None,
) -> DhcpVerdict:
    """Decide whether ``new_host`` may be written to one of ``batteries``.

    ``is_reachable`` is asked, for a battery index, whether that battery is
    talking right now at its currently configured address. It is optional so the
    guards stay testable in isolation; the config flow passes a callable backed
    by the live coordinator.

    Returns a verdict naming the single guard that refused, or OK plus the index
    of the battery to move.
    """
    normalised = normalise_mac(mac)
    if normalised is None:
        return DhcpVerdict(INVALID_MAC)

    host = (new_host or "").strip()
    if not host:
        return DhcpVerdict(INVALID_MAC)

    matches = [
        index
        for index, battery in enumerate(batteries)
        if tracking_enabled(battery) and normalise_mac(battery.get(CONF_MAC)) == normalised
    ]

    if not matches:
        return DhcpVerdict(NO_MATCH)

    # One MAC covering several batteries is the shared-gateway case: the MAC
    # belongs to the gateway, not to any one battery, so it cannot say which of
    # them moved. Abstaining is the only safe answer.
    if len(matches) > 1:
        return DhcpVerdict(AMBIGUOUS_MAC)

    index = matches[0]
    battery = batteries[index]

    if not is_ip_based(battery):
        return DhcpVerdict(NOT_IP_BASED)

    # A lease is renewed periodically for an address the battery already has.
    # Acting on it would reload the entry on every renewal for no gain.
    if (battery.get("host") or "").strip() == host:
        return DhcpVerdict(UNCHANGED)

    # A device can hold two addresses at once — we measured one Marstek unit
    # answering on two IPv4 addresses for ~20 h. While the configured address
    # still answers, the new lease is extra information, not a move: switching
    # would trade a working endpoint for an untested one and could flap between
    # the two. Only a battery that has actually gone quiet is worth moving.
    if is_reachable is not None and is_reachable(index):
        return DhcpVerdict(STILL_REACHABLE)

    # Moving onto an endpoint another battery already owns would point two
    # config entries at one device, and send one battery's commands to another.
    candidate = endpoint_of(battery, host)
    for other_index, other in enumerate(batteries):
        if other_index == index:
            continue
        if endpoint_of(other) == candidate:
            return DhcpVerdict(ENDPOINT_CONFLICT)

    return DhcpVerdict(OK, index)


def publishable_macs(batteries: Sequence[dict]) -> list[str | None]:
    """Return, per battery index, the MAC that may be published to the device registry.

    Home Assistant indexes devices by identifiers **and** by connections, and
    ``DeviceRegistryItems.get_entry()`` falls through to the connection index
    when no identifier matches. Two batteries publishing one MAC therefore
    resolve to the *same* device entry: the second one is absorbed into the
    first, before any DHCP lease is ever evaluated. The ``AMBIGUOUS_MAC`` guard
    cannot help — it runs in the discovery path, and this collision happens at
    registration.

    So a MAC shared by several configured batteries — the Modbus-gateway case,
    where the MAC belongs to the gateway rather than to any battery — is
    published for none of them. They stay separate devices, and DHCP tracking
    is simply inert for that gateway, which is the safe outcome.

    Returns None for a battery that is not tracked, has no valid MAC, or shares
    its MAC with another entry.
    """
    normalised = [
        normalise_mac(b.get(CONF_MAC)) if tracking_enabled(b) else None
        for b in batteries
    ]
    counts: dict[str, int] = {}
    for mac in normalised:
        if mac:
            counts[mac] = counts.get(mac, 0) + 1
    return [mac if mac and counts[mac] == 1 else None for mac in normalised]


def detect_mac(discovered: Iterable[Any], host: str) -> str | None:
    """Find the MAC of ``host`` among Home Assistant's known DHCP leases.

    Fed by ``homeassistant.components.dhcp.async_discovered_service_info``, whose
    entries expose ``ip`` and ``macaddress``. Returns None when Home Assistant
    has never seen the device — which is normal on installs that do not sit on
    the batteries' network — and the user is then asked for the MAC by hand.
    """
    target = (host or "").strip()
    if not target:
        return None
    for service_info in discovered:
        if getattr(service_info, "ip", None) == target:
            return normalise_mac(getattr(service_info, "macaddress", None))
    return None
