"""Per-brand config flow steps: ``DriverSetup`` and what its handlers receive.

A brand that wants its connection steps out of ``config_flow.py`` sets
``SETUP = DriverSetup(...)`` on its driver class. ``config_flow`` generates one
real ``async_step_<id>`` method per entry of ``DriverSetup.steps`` on both flow
classes (``_install_driver_steps``) and dispatches to ``entry_step`` from the
brand selector (add / options) or ``reconfigure_entry_step`` (reconfigure).

Import direction: this module imports neither ``config_flow`` nor
``drivers.registry`` (``config_flow -> registry -> driver -> <brand>_flow ->
config_flow`` would be a cycle). A ``drivers/<brand>_flow.py`` imports only from
here, ``const`` and Home Assistant; ``flow`` in a handler is the live flow object.

Handler contract::

    async def handler(flow, user_input, ctx) -> FlowResult | dict

* A ``FlowResult`` (a form, or ``await flow.async_step_<next>()``) is returned
  to Home Assistant untouched.
* A plain ``dict`` is the brand's validated battery data. The shared flow merges
  it into the battery in progress and continues: to ``battery_limits`` when
  adding or editing in options, and to the next battery or the save when
  reconfiguring (the data is laid over the stored battery). Registry-id
  migration on a changed host is the handler's business (``ctx.entry`` plus the
  flow's own helper), as it is in the brand branches today.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult, FlowResultType
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from ..const import CONF_DC_PV_CONNECTED, DOMAIN

# Ceiling used by the default ``power_ceilings``.
_DEFAULT_CEILING_W = 2500


class FlowKind(StrEnum):
    """Which flow a handler is running in."""

    CONFIG = "config"            # adding a battery (config flow)
    OPTIONS = "options"          # editing / replacing a battery (options flow)
    RECONFIGURE = "reconfigure"  # config flow, reconfigure source


@dataclass
class SetupContext:
    """What a step handler may read; ``work`` is the only thing it may mutate.

    ``battery``: the stored battery at this slot, ``None`` when adding a slot
    that does not exist yet. ``entry``: the config entry, ``None`` while adding
    the first one. ``coordinator``: the live coordinator of this slot, ``None``
    when nothing is loaded. ``work``: the brand's scratch dict; it lives on the
    flow, survives between the steps of one brand's setup and is emptied when
    the brand's entry step is (re)entered.
    """

    kind: FlowKind
    battery: dict | None
    entry: ConfigEntry | None
    coordinator: Any | None
    work: dict = field(default_factory=dict)


class ReplacesBattery(dict):
    """Battery data a reconfigure handler returns to *replace* the stored battery.

    A plain ``dict`` is laid over the stored battery, so it cannot drop a key;
    a handler that must (a stale value that has to go) returns this instead.
    """


StepHandler = Callable[[Any, "dict | None", SetupContext], Awaitable["FlowResult | dict"]]


class DriverSetup:
    """A brand's config flow: its steps plus the hooks the shared steps will call.

    ``steps`` maps step id -> handler; the handler decides the next step. The
    five methods below are what the shared ``battery_limits`` step (config and
    options flow) and the time-slot forms ask of a brand; their defaults describe
    a brand that adds nothing to them. ``ctx`` is the :class:`SetupContext` of the
    flow that is running (``ctx.kind`` tells config from options).
    """

    def __init__(
        self,
        steps: dict[str, StepHandler],
        entry_step: str,
        reconfigure_entry_step: str | None = None,
    ) -> None:
        self.steps = steps
        self.entry_step = entry_step
        self.reconfigure_entry_step = reconfigure_entry_step

    def power_ceilings(self, battery_data: dict) -> tuple[int, int]:
        """(max charge W, max discharge W) offered by the limits step."""
        return _DEFAULT_CEILING_W, _DEFAULT_CEILING_W

    def limits_schema(self, battery_data: dict, base_fields: dict, ctx: SetupContext) -> dict:
        """The limits form fields; may add and remove fields. Default: unchanged.

        ``base_fields`` are the shared fields (power, SOC, hysteresis, backup
        threshold); the brand's own fields go after them. The phase and MAC
        fields are added by the flow afterwards.
        """
        return base_fields

    def finalize_limits(
        self, merged: dict, user_input: dict, battery_data: dict, ctx: SetupContext
    ) -> None:
        """Adjust the battery dict right before it is stored. Default: nothing.

        ``merged`` already holds the shared values (the power limits from the form
        when it has those fields, SOC, hysteresis, phase);
        ``battery_data`` is the battery as the connection steps left it.
        """

    async def reprobe(self, hass: Any, battery_config: dict, host: str) -> bool:
        """Re-probe a stored battery at a new IP (DHCP). Default: not supported."""
        return False


def saved_dc_pv_connected(driver_cls: Any, merged: dict, user_input: dict) -> bool:
    """The ``dc_pv_connected`` value the limits step stores for a battery.

    The user's choice where the driver's traits for the stored config have an
    optional DC PV input, otherwise False. The base traits follow the stored
    version for any brand, which keeps the options-flow drift of plan section 8.
    """
    if not driver_cls.traits_for(merged).optional_dc_pv_input:
        return False
    return bool(user_input.get(CONF_DC_PV_CONNECTED, True))


def optional_capacity_field(
    fields: dict, battery_data: dict, ctx: SetupContext, default: float
) -> dict:
    """``fields`` plus an optional ``battery_capacity_kwh`` box (0 = let the device report it).

    Options flow: the default is the capacity already stored for this slot (the
    stored battery with this flow's connection data on top), else ``default``.
    """
    capacity_default = default
    if ctx.kind is FlowKind.OPTIONS and ctx.battery is not None:
        capacity_default = (ctx.battery | battery_data).get("battery_capacity_kwh", default)
    fields[vol.Optional("battery_capacity_kwh", default=capacity_default)] = NumberSelector(
        NumberSelectorConfig(
            min=0, max=100, step=0.01, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX
        )
    )
    return fields


def saved_optional_capacity(user_input: dict, default: float) -> float:
    """The ``battery_capacity_kwh`` the limits step stores for an optional-capacity brand."""
    return round(float(user_input.get("battery_capacity_kwh", default)), 2)


def is_flow_result(value: Any) -> bool:
    """True for a ``FlowResult`` (it carries a ``FlowResultType``), false for battery data."""
    return isinstance(value, dict) and isinstance(value.get("type"), FlowResultType)


def work_dict(flow: Any, brand_key: str, *, reset: bool = False) -> dict:
    """The brand's scratch dict, kept in the flow's own ``__dict__``."""
    store = flow.__dict__.setdefault("_driver_work", {})
    if reset or brand_key not in store:
        store[brand_key] = {}
    return store[brand_key]


def live_coordinator(flow: Any, entry: ConfigEntry | None, index: int) -> Any | None:
    """The loaded coordinator of battery ``index`` of ``entry``, or ``None``."""
    if entry is None:
        return None
    data = (getattr(flow.hass, "data", None) or {}).get(DOMAIN) or {}
    data = data.get(getattr(entry, "entry_id", None)) or {}
    coordinators = data.get("coordinators") or []
    return coordinators[index] if index < len(coordinators) else None
