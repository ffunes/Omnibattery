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

from homeassistant.util import slugify


def english_entity_id(domain: str, device_name: str, key: str) -> str:
    """Return an English ``entity_id`` slug, independent of the UI language.

    ``device_name`` is the device's name (used as the entity_id prefix, matching
    HA's default ``has_entity_name`` behavior) and ``key`` is the English
    translation key.
    """
    return f"{domain}.{slugify(f'{device_name} {key}')}"
