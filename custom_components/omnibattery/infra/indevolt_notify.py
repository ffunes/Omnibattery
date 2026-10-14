"""Persistent notifications for the Indevolt driver.

The driver reports why control is paused, and a few one-off setup problems,
through this adapter. Each topic has one notification per battery: raising it
again replaces the text, and it is dismissed once the condition clears.

Texts follow Home Assistant's language for English and Dutch (many Indevolt
owners are Dutch); every other language falls back to English.
"""

from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from ..const import DOMAIN, NOTIFICATION_ID_PREFIX

_LOGGER = logging.getLogger(__name__)

# The official Home Assistant integration polls the same device. When it is
# loaded, it is the most likely reason the battery answers slowly.
OFFICIAL_INTEGRATION_DOMAIN = "indevolt"

PAUSE_TOPICS = ("realtime_mode_required", "handover_failed", "unknown_model")

_TEXTS = {
    "en": {
        "paused_title": "Indevolt control paused: {name}",
        "realtime_mode_required": (
            "Omnibattery paused control of **{name}** because the battery is not in "
            "**Realtime** mode.\n\n"
            "Open the Indevolt app and set the working mode to Realtime. Omnibattery "
            "then resumes control automatically."
        ),
        "handover_failed": (
            "Omnibattery could not take control of **{name}**: the battery did not "
            "confirm standby after a stop command. It keeps monitoring and retries "
            "automatically."
        ),
        "unknown_model": (
            "**{name}** reports device type `{model}`, which Omnibattery does not know "
            "yet. It is monitored, but not controlled. Please report the device type "
            "so it can be added."
        ),
        "firmware_title": "Indevolt firmware update recommended: {name}",
        "firmware": (
            "**{name}** runs firmware `{firmware}`. Indevolt recommends at least "
            "`{minimum}` for the local API. Monitoring works; update the battery in "
            "the Indevolt app if control misbehaves."
        ),
        "floor_title": "Indevolt reserve above Omnibattery minimum: {name}",
        "floor": (
            "**{name}** stops discharging at its own reserve of {native:.0f}%, but "
            "Omnibattery's minimum SOC is {core:.0f}%. Between those values "
            "Omnibattery asks for power the battery will not deliver. Lower the reserve "
            "in the Indevolt app, or raise Omnibattery's minimum SOC to {native:.0f}%."
        ),
        "throttled_title": "Indevolt battery slow: {name}",
        "throttled": (
            "**{name}** answers slowly or not at all, so Omnibattery now waits "
            "{interval:.0f} s between requests."
        ),
        "official": (
            "\n\nThe official Indevolt integration is also polling this battery. "
            "Omnibattery is meant to replace it: disable the Indevolt integration for "
            "this device to free the battery's request budget."
        ),
    },
    "nl": {
        "paused_title": "Indevolt-aansturing gepauzeerd: {name}",
        "realtime_mode_required": (
            "Omnibattery heeft de aansturing van **{name}** gepauzeerd, omdat de "
            "batterij niet in de modus **Realtime** staat.\n\n"
            "Open de Indevolt-app en zet de werkmodus op Realtime. Omnibattery neemt "
            "de aansturing dan vanzelf weer over."
        ),
        "handover_failed": (
            "Omnibattery kon de aansturing van **{name}** niet overnemen: de batterij "
            "bevestigde geen stand-by na een stopopdracht. Omnibattery blijft de "
            "batterij volgen en probeert het vanzelf opnieuw."
        ),
        "unknown_model": (
            "**{name}** meldt apparaattype `{model}`, dat Omnibattery nog niet kent. "
            "De batterij wordt gevolgd, maar niet aangestuurd. Meld het apparaattype "
            "graag, zodat het kan worden toegevoegd."
        ),
        "firmware_title": "Indevolt-firmware-update aanbevolen: {name}",
        "firmware": (
            "**{name}** draait firmware `{firmware}`. Indevolt raadt voor de lokale API "
            "minimaal `{minimum}` aan. Uitlezen werkt; werk de batterij bij in de "
            "Indevolt-app als de aansturing niet goed werkt."
        ),
        "floor_title": "Indevolt-reserve hoger dan Omnibattery-minimum: {name}",
        "floor": (
            "**{name}** stopt met ontladen bij zijn eigen reserve van {native:.0f}%, "
            "maar het minimale SOC van Omnibattery is {core:.0f}%. Daartussen vraagt "
            "Omnibattery vermogen dat de batterij niet levert. Verlaag de reserve in de "
            "Indevolt-app, of verhoog het minimale SOC van Omnibattery naar "
            "{native:.0f}%."
        ),
        "throttled_title": "Indevolt-batterij traag: {name}",
        "throttled": (
            "**{name}** reageert traag of niet, dus Omnibattery wacht nu {interval:.0f} s "
            "tussen verzoeken."
        ),
        "official": (
            "\n\nDe officiële Indevolt-integratie vraagt deze batterij ook uit. "
            "Omnibattery is bedoeld als vervanging: schakel de Indevolt-integratie voor "
            "dit apparaat uit, zodat de batterij haar verzoeken niet hoeft te delen."
        ),
    },
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
        language = str(getattr(getattr(self._hass, "config", None), "language", None) or "en")
        texts = _TEXTS.get(language.split("-")[0].lower(), _TEXTS["en"])
        return texts[key].format(name=self.display_name, **values)

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
        persistent_notification.async_create(
            self._hass, message, title=title, notification_id=self._prefix + topic
        )

    def _dismiss(self, topic: str) -> None:
        if topic in self._active:
            self._active.discard(topic)
            persistent_notification.async_dismiss(self._hass, self._prefix + topic)

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

    def floor_mismatch(self, core_min_soc: float, native_floor: float) -> None:
        self._create(
            "floor",
            self._text("floor_title"),
            self._text("floor", core=core_min_soc, native=native_floor),
        )

    def floor_ok(self) -> None:
        self._dismiss("floor")

    def throttled(self, interval_s: float) -> None:
        message = self._text("throttled", interval=interval_s)
        if self.official_integration_loaded():
            message += self._text("official")
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
