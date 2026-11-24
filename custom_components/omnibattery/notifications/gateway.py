"""Single gateway for persistent notifications, gated by category."""
from __future__ import annotations

from collections.abc import Mapping

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..const import (
    CONF_NOTIFY_EXECUTION_ENABLED,
    CONF_NOTIFY_MODES_ENABLED,
    CONF_NOTIFY_PLAN_INFO_ENABLED,
    CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED,
    DOMAIN,
)

PLAN_CHARGE = "plan_charge"
PLAN_INFO = "plan_info"
EXECUTION = "execution"
MODES = "modes"
HEALTH = "health"

# HEALTH is absent on purpose: alarms and failures cannot be muted.
CATEGORY_KEYS = {
    PLAN_CHARGE: CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED,
    PLAN_INFO: CONF_NOTIFY_PLAN_INFO_ENABLED,
    EXECUTION: CONF_NOTIFY_EXECUTION_ENABLED,
    MODES: CONF_NOTIFY_MODES_ENABLED,
}


def _store(hass: HomeAssistant) -> dict | None:
    """hass.data[DOMAIN], or None for hass objects without data (tests)."""
    data = getattr(hass, "data", None)
    if not isinstance(data, dict):
        return None
    return data.setdefault(DOMAIN, {})


def _resolve(data: Mapping) -> dict:
    plan_charge = bool(data.get(CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED, True))
    return {
        PLAN_CHARGE: plan_charge,
        PLAN_INFO: bool(data.get(CONF_NOTIFY_PLAN_INFO_ENABLED, plan_charge)),
        EXECUTION: bool(data.get(CONF_NOTIFY_EXECUTION_ENABLED, plan_charge)),
        MODES: bool(data.get(CONF_NOTIFY_MODES_ENABLED, True)),
    }


def load_categories(hass: HomeAssistant, data: Mapping) -> None:
    """Read the category switches from the entry data into hass.data.

    A missing plan-info or execution key follows the plan-charge key, so
    installs that muted notifications before keep them muted; modes default on.
    """
    store = _store(hass)
    if store is None:
        return
    store["notification_categories"] = _resolve(data)


def category_enabled(hass: HomeAssistant, category: str) -> bool:
    """Whether a category may notify (HEALTH always may)."""
    if category == HEALTH:
        return True
    store = _store(hass)
    if store is None or "notification_categories" not in store:
        return True
    return store["notification_categories"].get(category, True)



def _remember(hass: HomeAssistant, category: str, notification_id: str) -> None:
    # ponytail: in memory only; HA's persistent notifications do not survive a
    # restart either, so there is nothing to dismiss after one.
    store = _store(hass)
    if store is not None:
        store.setdefault("notification_shown", {})[notification_id] = category


def _forget(hass: HomeAssistant, notification_id: str) -> None:
    store = _store(hass)
    if store is not None:
        store.setdefault("notification_shown", {}).pop(notification_id, None)


async def async_notify(
    hass: HomeAssistant, category: str, notification_id: str, title: str, message: str
) -> bool:
    """Create a notification; False when the category is off."""
    if not category_enabled(hass, category):
        return False
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {"title": title, "message": message, "notification_id": notification_id},
    )
    _remember(hass, category, notification_id)
    return True


async def async_dismiss(hass: HomeAssistant, notification_id: str) -> None:
    """Dismiss a notification."""
    await hass.services.async_call(
        "persistent_notification", "dismiss", {"notification_id": notification_id}
    )
    _forget(hass, notification_id)


async def async_set_category_enabled(
    hass: HomeAssistant, entry: ConfigEntry, category: str, enabled: bool
) -> None:
    """Switch a category on or off, persist all four keys (ends the inheritance), clear its notices."""
    if category not in CATEGORY_KEYS:
        raise ValueError(f"Category {category!r} cannot be switched")
    store = _store(hass)
    if store is not None and "notification_categories" not in store:
        load_categories(hass, entry.data)
    if store is not None:
        store["notification_categories"][category] = enabled
    # All four keys: the load-time inheritance must only seed missing keys.
    if store is not None:
        current = dict(store["notification_categories"])
    else:
        current = _resolve(entry.data)
        current[category] = enabled
    resolved = {CATEGORY_KEYS[c]: v for c, v in current.items()}
    hass.config_entries.async_update_entry(entry, data={**entry.data, **resolved})
    if not enabled and store is not None:
        shown = store.get("notification_shown", {})
        for notification_id in [i for i, c in shown.items() if c == category]:
            await async_dismiss(hass, notification_id)


@callback
def notify(
    hass: HomeAssistant, category: str, notification_id: str, title: str, message: str
) -> bool:
    """Synchronous twin of async_notify for @callback emitters."""
    if not category_enabled(hass, category):
        return False
    persistent_notification.async_create(
        hass, message, title=title, notification_id=notification_id
    )
    _remember(hass, category, notification_id)
    return True


@callback
def dismiss(hass: HomeAssistant, notification_id: str) -> None:
    """Synchronous twin of async_dismiss."""
    persistent_notification.async_dismiss(hass, notification_id)
    _forget(hass, notification_id)
