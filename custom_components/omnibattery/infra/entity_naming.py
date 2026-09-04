"""Helpers for stable, language-independent entity naming.

Home Assistant derives an entity's ``entity_id`` from its *translated* display
name, so a non-English install produces localized object ids (e.g.
``sensor.marstek_venus_1_potencia_ac``). That makes cross-language support
painful. Building the id from the English ``key`` keeps entity_ids consistent
regardless of the UI language, while the friendly (display) name stays
localized via ``translation_key``.

This only affects *newly* registered entities: the entity registry matches on
``unique_id`` and preserves the stored entity_id for entities it already knows,
so existing installs keep their current (possibly localized) ids untouched.
"""
from __future__ import annotations

from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify


def english_entity_id(domain: str, device_name: str, key: str) -> str:
    """Return an English ``entity_id`` slug, independent of the UI language.

    ``device_name`` is the device's name (used as the entity_id prefix, matching
    HA's default ``has_entity_name`` behavior) and ``key`` is the English
    translation key.
    """
    return f"{domain}.{slugify(f'{device_name} {key}')}"


# System (aggregate) entities keep a stable, brand-legacy ``unique_id`` prefix so
# the registry identity — and the long-term statistics/history tied to it —
# survive the Omnibattery rebrand untouched. Never change this; the v9 heal
# migration and existing installs depend on it.
SYSTEM_UNIQUE_ID_PREFIX = "marstek_venus_system_"

# The *suggested* object id, however, uses the Omnibattery prefix. Existing
# installs keep their stored ``sensor.marstek_venus_system_*`` id until the user
# opts in via HA's built-in "Recreate entity IDs" (which regenerates from the
# suggested object id); fresh installs are born ``omnibattery_*``.
#
# The prefix is just ``omnibattery_`` (not ``omnibattery_system_``): several keys
# already start with ``system_`` (e.g. ``system_soc``), so an ``..._system_``
# prefix would double it into ``omnibattery_system_system_soc``. Keys carry their
# own grouping; this yields ``omnibattery_system_soc`` and ``omnibattery_home_consumption``.
SYSTEM_OBJECT_ID_PREFIX = "omnibattery_"


def system_entity_id(domain: str, key: str) -> str:
    """Return the suggested ``entity_id`` for a system-level entity.

    ``key`` is the English object-id suffix (which may differ from the unique_id
    suffix, e.g. ``net_balance`` vs. unique ``balance_neto``). The unique_id keeps
    :data:`SYSTEM_UNIQUE_ID_PREFIX`; only the suggested entity_id is rebranded.
    """
    return f"{domain}.{SYSTEM_OBJECT_ID_PREFIX}{key}"


def is_omnibattery_solar_entity(hass, entity_id: str | None) -> bool:
    """Return whether an entity is one of OmniBattery's solar outputs.

    Check the stable registry identity as well as current and legacy suggested
    IDs so renamed aggregate and per-battery entities are both covered.
    """
    if not entity_id:
        return False
    if entity_id in {
        system_entity_id("sensor", "solar_power"),
        "sensor.marstek_venus_system_solar_power",
    }:
        return True
    try:
        entry = er.async_get(hass).async_get(entity_id)
    except (AttributeError, KeyError, TypeError):
        return False
    return bool(
        entry
        and entry.platform in {"omnibattery", "marstek_venus"}
        and (
            entry.unique_id == f"{SYSTEM_UNIQUE_ID_PREFIX}solar_power"
            or entry.unique_id.endswith("_solar_power")
        )
    )


def excluded_device_name(hass, device: dict) -> str:
    """Return an excluded device's Home Assistant name, with an ID fallback.

    Excluded-device controls are registered on the Omnibattery system device, so
    their translated names need an explicit per-device prefix.  Prefer the name
    the user gave the configured source entity rather than exposing its object
    ID.  The fallback keeps setup resilient when the source entity is currently
    unavailable or has been removed.
    """
    entity_id = device.get("power_sensor") or device.get("activity_sensor") or "device"
    state = hass.states.get(entity_id)
    if state:
        friendly_name = state.attributes.get("friendly_name")
        if friendly_name:
            return friendly_name
    return entity_id.split(".", 1)[-1].replace("_", " ").title()
