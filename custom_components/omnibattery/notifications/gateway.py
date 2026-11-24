"""Single gateway for persistent notifications, gated by category."""
from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback

PLAN_CHARGE = "plan_charge"
PLAN_INFO = "plan_info"
EXECUTION = "execution"
MODES = "modes"
HEALTH = "health"


def _category_enabled(hass: HomeAssistant, category: str) -> bool:
    """Whether a category may notify (all on for now)."""
    return True


async def async_notify(
    hass: HomeAssistant, category: str, notification_id: str, title: str, message: str
) -> bool:
    """Create a notification; False when the category is off."""
    if not _category_enabled(hass, category):
        return False
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {"title": title, "message": message, "notification_id": notification_id},
    )
    return True


async def async_dismiss(hass: HomeAssistant, notification_id: str) -> None:
    """Dismiss a notification."""
    await hass.services.async_call(
        "persistent_notification", "dismiss", {"notification_id": notification_id}
    )


@callback
def notify(
    hass: HomeAssistant, category: str, notification_id: str, title: str, message: str
) -> bool:
    """Synchronous twin of async_notify for @callback emitters."""
    if not _category_enabled(hass, category):
        return False
    persistent_notification.async_create(
        hass, message, title=title, notification_id=notification_id
    )
    return True


@callback
def dismiss(hass: HomeAssistant, notification_id: str) -> None:
    """Synchronous twin of async_dismiss."""
    persistent_notification.async_dismiss(hass, notification_id)
