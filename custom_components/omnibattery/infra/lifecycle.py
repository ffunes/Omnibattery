"""Small config-entry lifecycle guards shared by flows and runtime listeners."""
from __future__ import annotations

from typing import Any

from ..const import DOMAIN

_RELOAD_PENDING_KEY = "_omnibattery_reload_pending"


def _pending_entries(hass: Any) -> set[str]:
    """Return the domain-owned set used while an options reload is in flight."""
    data = getattr(hass, "data", None)
    if not isinstance(data, dict):
        try:
            pending = getattr(hass, _RELOAD_PENDING_KEY)
            if isinstance(pending, set):
                return pending
        except AttributeError:
            pending = set()
            try:
                setattr(hass, _RELOAD_PENDING_KEY, pending)
            except AttributeError:
                pass
            return pending
        return set()
    domain_data = data.setdefault(DOMAIN, {})
    if not isinstance(domain_data, dict):
        domain_data = {}
        data[DOMAIN] = domain_data
    pending = domain_data.get(_RELOAD_PENDING_KEY)
    if not isinstance(pending, set):
        pending = set()
        domain_data[_RELOAD_PENDING_KEY] = pending
    return pending


def _fallback_pending_entries(hass: Any) -> set[str]:
    """Return the fallback marker for minimal Home Assistant test doubles."""
    try:
        pending = getattr(hass, _RELOAD_PENDING_KEY)
    except AttributeError:
        pending = set()
        try:
            setattr(hass, _RELOAD_PENDING_KEY, pending)
        except AttributeError:
            return set()
    return pending if isinstance(pending, set) else set()


def mark_reload_pending(hass: Any, entry_id: str) -> None:
    """Mark an entry before mutating its data and requesting a reload."""
    _pending_entries(hass).add(str(entry_id))


def is_reload_pending(hass: Any, entry_id: str) -> bool:
    """Return whether the old runtime must ignore its update callback."""
    data = getattr(hass, "data", None)
    if not isinstance(data, dict):
        return str(entry_id) in _fallback_pending_entries(hass)
    domain_data = data.get(DOMAIN, {}) if isinstance(data, dict) else {}
    pending = domain_data.get(_RELOAD_PENDING_KEY)
    return isinstance(pending, set) and str(entry_id) in pending


def clear_reload_pending(hass: Any, entry_id: str) -> None:
    """Clear the marker even when the reload raises or setup is retried."""
    data = getattr(hass, "data", None)
    if not isinstance(data, dict):
        _fallback_pending_entries(hass).discard(str(entry_id))
        return
    domain_data = data.get(DOMAIN, {}) if isinstance(data, dict) else {}
    pending = domain_data.get(_RELOAD_PENDING_KEY)
    if not isinstance(pending, set):
        return
    pending.discard(str(entry_id))
    if not pending:
        domain_data.pop(_RELOAD_PENDING_KEY, None)


__all__ = [
    "clear_reload_pending",
    "is_reload_pending",
    "mark_reload_pending",
]
