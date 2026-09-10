"""Button platform for the Omnibattery integration."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_ENABLE_PREDICTIVE_CHARGING,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_TIME_SLOT,
)
from .infra.coordinator import MarstekVenusDataUpdateCoordinator
from .infra.entity_naming import english_entity_id, system_entity_id, SYSTEM_UNIQUE_ID_PREFIX
from .pricing.engine import DynamicPricingEvaluationHorizon

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button platform."""
    coordinators: list[MarstekVenusDataUpdateCoordinator] = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    controller = hass.data[DOMAIN][entry.entry_id].get("controller")
    entities = []

    # Add regular battery buttons
    for coordinator in coordinators:
        for definition in coordinator.button_definitions:
            entities.append(MarstekVenusButton(coordinator, definition))

    # System-level button: re-run the predictive charge evaluation on demand.
    # Real-time price is deliberately excluded: it re-decides every cycle while
    # the price is below the threshold, so the button would do nothing.
    if (
        controller
        and CONF_ENABLE_PREDICTIVE_CHARGING in entry.data
        and controller.predictive_charging_mode in (
            PREDICTIVE_MODE_DYNAMIC_PRICING,
            PREDICTIVE_MODE_TIME_SLOT,
        )
    ):
        entities.append(ReevaluateDynamicPricingButton(controller))

    async_add_entities(entities)


class MarstekVenusButton(ButtonEntity):
    """Representation of a Marstek Venus button."""

    def __init__(
        self, coordinator: MarstekVenusDataUpdateCoordinator, definition: dict
    ) -> None:
        """Initialize the button."""
        self.coordinator = coordinator

        self._attr_has_entity_name = True
        self._attr_translation_key = definition["key"]
        self._attr_unique_id = f"{coordinator.device_key}_{definition['key']}"
        self.entity_id = english_entity_id("button", coordinator.name, definition["key"])
        self._attr_icon = definition.get("icon")
        self._attr_device_class = definition.get("device_class")
        self._attr_entity_registry_enabled_default = definition.get("enabled_by_default", True)
        self._attr_should_poll = False
        self._key = definition["key"]
        self._command = definition["command"]

    async def async_press(self) -> None:
        """Press the button."""
        await self.coordinator.write_control(self._key, self._command, do_refresh=True)

    @property
    def device_info(self):
        """Return device information."""
        return self.coordinator.battery_device_info


class ReevaluateDynamicPricingButton(ButtonEntity):
    """System button to re-run the dynamic-pricing predictive charge evaluation on demand."""

    def __init__(self, controller) -> None:
        """Initialize the re-evaluation button."""
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "reevaluate_dynamic_pricing"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}reevaluate_dynamic_pricing"
        self.entity_id = system_entity_id("button", "reevaluate_dynamic_pricing")
        self._attr_icon = "mdi:calendar-refresh"
        self._attr_should_poll = False

    async def async_press(self) -> None:
        """Rebuild whatever plan the configured predictive mode works from."""
        if self.controller.predictive_charging_mode != PREDICTIVE_MODE_DYNAMIC_PRICING:
            # Time slot has no calendar to rebuild. Clearing the SOC reference
            # makes the next cycle inside a window run a full initial
            # evaluation, notification included; outside one it does nothing,
            # which is correct — that mode cannot charge there anyway.
            self.controller.invalidate_predictive_plan("re-evaluate button")
            return
        # The button is commonly pressed after a price/forecast adjustment.  It
        # must not reuse the full-day 00:05 balance after energy has elapsed.
        await self.controller._pricing_mgr._evaluate_dynamic_pricing(
            horizon=DynamicPricingEvaluationHorizon.REMAINING,
            extended_horizon=True,
        )

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }
