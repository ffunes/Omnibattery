"""Persistent notifications for the Indevolt driver.

The driver reports why control is paused, and a few one-off setup problems,
through this adapter. Each topic has one notification per battery: raising it
again replaces the text, and it is dismissed once the condition clears.

Texts live in the shared notification catalogue (notifications/texts, keys
"battery_control.*") and follow Home Assistant's language, with English as the
fallback. They do not name the brand: the user knows the battery by its name.

One topic is a Repairs issue instead: setting writes that stopped at the yearly
allowance (see drivers/indevolt_settings.py). Only the user can turn them back
on, through its fix flow (repairs.py), and its texts live in the translations.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from ..const import DOMAIN, NOTIFICATION_ID_PREFIX
from ..notifications import HEALTH, dismiss, notify
from ..notifications.texts import language, translate
from .indevolt_settings import FLASH_WRITES_ISSUE

_LOGGER = logging.getLogger(__name__)

# The official Home Assistant integration polls the same device. When it is
# loaded, it is the most likely reason the battery answers slowly.
OFFICIAL_INTEGRATION_DOMAIN = "indevolt"

PAUSE_TOPICS = ("realtime_mode_required", "handover_failed", "unknown_model")
# Setting key (see drivers/indevolt_settings.py) -> its name in the texts.
_SETTING_LABELS = {
    "discharging_cutoff_capacity": "min_soc",
    "max_charge_power": "max_charge_power",
    "max_discharge_power": "max_discharge_power",
}


class IndevoltNotifier:
    """Create and dismiss the Indevolt notifications for one battery."""

    def __init__(self, hass: HomeAssistant, name: str, device_key: str) -> None:
        self._hass = hass
        self._name = name
        self._device_key = device_key
        self._prefix = f"{NOTIFICATION_ID_PREFIX}indevolt_{device_key}_"
        self._active: set[str] = set()

    def _text(self, key: str, **values) -> str:
        return translate(language(self._hass), f"battery_control.{key}", name=self.display_name, **values)

    @property
    def display_name(self) -> str:
        """The battery as the user sees it: a renamed device wins over the config name."""
        try:
            registry = dr.async_get(self._hass)
            identifiers = {(DOMAIN, self._device_key)}
            if hasattr(registry, "async_get_devices"):
                # Recent Home Assistant deprecates async_get_device.
                devices = registry.async_get_devices(identifiers=identifiers)
                device = devices[0] if devices else None
            else:
                device = registry.async_get_device(identifiers=identifiers)
        except Exception:  # noqa: BLE001 - a name lookup must never stop a notification
            device = None
        if device is not None:
            return device.name_by_user or device.name or self._name
        return self._name

    def _create(self, topic: str, title: str, message: str) -> None:
        self._active.add(topic)
        notify(self._hass, HEALTH, self._prefix + topic, title, message)

    def _dismiss(self, topic: str) -> None:
        if topic in self._active:
            self._active.discard(topic)
            dismiss(self._hass, self._prefix + topic)

    def paused(self, reason: str, *, model: str | None = None) -> None:
        if reason not in PAUSE_TOPICS:
            return
        for other in PAUSE_TOPICS:
            if other != reason:
                self._dismiss(other)
        self._create(
            reason,
            self._text("paused_title"),
            self._text(reason, model=model or "unknown"),
        )

    def resumed(self) -> None:
        for reason in PAUSE_TOPICS:
            self._dismiss(reason)

    def firmware_outdated(self, firmware: str, minimum: str) -> None:
        self._create(
            "firmware",
            self._text("firmware_title"),
            self._text("firmware", firmware=firmware, minimum=minimum),
        )

    def _setting_name(self, key: str) -> str:
        return self._text(_SETTING_LABELS.get(key, "min_soc"))

    def setting_failed(self, key: str, value: int) -> None:
        unit = "%" if key == "discharging_cutoff_capacity" else " W"
        self._create(
            f"setting_{key}",
            self._text("setting_title"),
            self._text("setting_failed", setting=self._setting_name(key), value=f"{value}{unit}"),
        )

    def setting_ok(self, key: str) -> None:
        self._dismiss(f"setting_{key}")

    @property
    def flash_issue_id(self) -> str:
        return f"{FLASH_WRITES_ISSUE}_{self._device_key}"

    def flash_writes_paused(self, allowance: int) -> None:
        """Setting writes stopped at the yearly allowance: a fixable Repairs issue."""
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self.flash_issue_id,
            is_fixable=True,
            # Raised again from the persisted state after a restart.
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=FLASH_WRITES_ISSUE,
            translation_placeholders={"name": self.display_name, "allowance": str(allowance)},
            # repairs.py finds the battery by this key and lets its driver fix it.
            data={"device_key": self._device_key},
        )

    def flash_writes_resumed(self) -> None:
        ir.async_delete_issue(self._hass, DOMAIN, self.flash_issue_id)

    def throttled(self, interval_s: float, *, slower_speed: str | None = None) -> None:
        """``slower_speed`` is the request speed one step below the chosen one, if any."""
        message = self._text("throttled", interval=interval_s)
        if self.official_integration_loaded():
            message += self._text("official")
        if slower_speed is not None:
            message += self._text("slower", speed=self._text(f"speed_{slower_speed}"))
        self._create("throttled", self._text("throttled_title"), message)

    def throttle_cleared(self) -> None:
        self._dismiss("throttled")

    def official_integration_loaded(self) -> bool:
        try:
            return any(
                entry.state is ConfigEntryState.LOADED
                for entry in self._hass.config_entries.async_entries(
                    OFFICIAL_INTEGRATION_DOMAIN
                )
            )
        except Exception:  # noqa: BLE001 - advisory only
            _LOGGER.debug("Could not check for the official Indevolt integration", exc_info=True)
            return False
