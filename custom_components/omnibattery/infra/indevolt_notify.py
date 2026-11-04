"""Persistent notifications for the Indevolt driver.

The driver reports why control is paused, and a few one-off setup problems,
through this adapter. Each topic has one notification per battery: raising it
again replaces the text, and it is dismissed once the condition clears.

Texts follow Home Assistant's language for English and Dutch (many Indevolt
owners are Dutch); every other language falls back to English.

One topic is a Repairs issue instead: setting writes that stopped at the yearly
allowance (see drivers/indevolt_settings.py). Only the user can turn them back
on, through its fix flow (repairs.py), and its texts live in the translations.
"""

from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from ..const import DOMAIN, NOTIFICATION_ID_PREFIX
from ..drivers.indevolt_settings import FLASH_WRITES_ISSUE

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
            "**{name}** reports model code `{model}`, which Omnibattery does not "
            "support yet. It is monitored, but not controlled. Please report the model "
            "code so it can be added."
        ),
        "firmware_title": "Indevolt firmware update recommended: {name}",
        "firmware": (
            "**{name}** runs firmware `{firmware}`. Indevolt recommends at least "
            "`{minimum}` for the local API. Monitoring works; update the battery in "
            "the Indevolt app if control misbehaves."
        ),
        "setting_title": "Indevolt setting not applied: {name}",
        "setting_failed": (
            "Omnibattery could not set the {setting} of **{name}** to {value}. The "
            "battery keeps its current value, and Omnibattery shows and uses that "
            "value again. Try again later, or change it in the Indevolt app."
        ),
        "min_soc": "minimum SOC",
        "max_charge_power": "maximum charge power",
        "max_discharge_power": "maximum discharge power",
        "throttled_title": "Indevolt battery slow: {name}",
        "throttled": (
            "**{name}** answers slowly or not at all, so Omnibattery now waits "
            "{interval:.0f}s between requests. Something else may be polling the battery "
            "too, such as another Home Assistant integration, or its network connection "
            "may be weak."
        ),
        "official": (
            "\n\nThe official Indevolt integration is also polling this battery. "
            "Omnibattery is meant to replace it: disable the Indevolt integration for "
            "this device to free the battery's request budget. Its controls (energy "
            "mode, discharge limit, feed-in limit) also write to the battery's flash "
            "memory whenever they are used."
        ),
        "slower": (
            "\n\nIf this keeps happening, lower the request speed of **{name}** one step, "
            "to **{speed}**: in Settings → Devices & services, choose Reconfigure on "
            "Omnibattery."
        ),
        "speed_fast": "Fast (2s)",
        "speed_normal": "Normal (3s)",
        "speed_conservative": "Conservative (5s)",
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
            "**{name}** meldt modelcode `{model}`, die Omnibattery nog niet ondersteunt. "
            "De batterij wordt gevolgd, maar niet aangestuurd. Meld de modelcode graag, "
            "zodat die kan worden toegevoegd."
        ),
        "firmware_title": "Indevolt-firmware-update aanbevolen: {name}",
        "firmware": (
            "**{name}** draait firmware `{firmware}`. Indevolt raadt voor de lokale API "
            "minimaal `{minimum}` aan. Uitlezen werkt; werk de batterij bij in de "
            "Indevolt-app als de aansturing niet goed werkt."
        ),
        "setting_title": "Indevolt-instelling niet toegepast: {name}",
        "setting_failed": (
            "Omnibattery kon het {setting} van **{name}** niet op {value} zetten. De "
            "batterij houdt haar huidige waarde, en Omnibattery toont en gebruikt die "
            "waarde weer. Probeer het later opnieuw, of wijzig het in de Indevolt-app."
        ),
        "min_soc": "minimale SOC",
        "max_charge_power": "maximale laadvermogen",
        "max_discharge_power": "maximale ontlaadvermogen",
        "throttled_title": "Indevolt-batterij traag: {name}",
        "throttled": (
            "**{name}** reageert traag of niet, dus Omnibattery wacht nu {interval:.0f}s "
            "tussen verzoeken. Mogelijk vraagt iets anders de batterij ook uit, zoals een "
            "andere Home Assistant-integratie, of is de netwerkverbinding zwak."
        ),
        "official": (
            "\n\nDe officiële Indevolt-integratie vraagt deze batterij ook uit. "
            "Omnibattery is bedoeld als vervanging: schakel de Indevolt-integratie voor "
            "dit apparaat uit, zodat de batterij haar verzoeken niet hoeft te delen. De "
            "bediening van die integratie (werkmodus, ontlaadlimiet, terugleverlimiet) "
            "schrijft bovendien naar het flashgeheugen van de batterij zodra je die gebruikt."
        ),
        "slower": (
            "\n\nGebeurt dit vaker, zet de verzoeksnelheid van **{name}** dan een stap "
            "lager, op **{speed}**: kies in Instellingen → Apparaten & diensten bij "
            "Omnibattery voor Opnieuw configureren."
        ),
        "speed_fast": "Snel (2s)",
        "speed_normal": "Normaal (3s)",
        "speed_conservative": "Conservatief (5s)",
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
