"""Marstek config flow: connection step, reconfigure form, and the limits hooks.

Imports only ``drivers.setup``, ``const`` and Home Assistant. ``marstek.py`` imports
``MARSTEK_SETUP`` from here to set ``MarstekModbusDriver.SETUP``, so this module cannot
import ``marstek.py`` at the top (cycle): everything that needs it imports it inside
the function. It is the same class object, so patching ``MarstekModbusDriver.probe`` /
``probe_details`` works as before.

Step ids carry no brand suffix (``battery_connection`` / ``reconfigure_battery``).
``reconfigure_battery`` is also the shared flow's dispatcher method, so the shared
flow never generates a method for it: its dispatcher calls this handler directly.

The EMS firmware a vD probe reports lives in ``ctx.work["ems_version"]``, not on
the flow instance. The two probes differ on purpose: in the options
flow the live coordinator of the same host/slave is closed before probing and reopened
after (a v3 accepts a single Modbus TCP connection), in the config flow (add and
reconfigure) nothing is closed.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..const import (
    CONF_BATTERY_VERSION,
    CONF_DC_PV_CONNECTED,
    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    CONF_RS485_GATEWAY,
    CONF_SERIAL_PORT,
    CONF_SLAVE_ID,
    DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    DEFAULT_SLAVE_ID,
    DEFAULT_VERSION,
    DOMAIN,
    max_power_for_battery_version,
)
from .setup import DriverSetup, FlowKind, SetupContext, saved_dc_pv_connected

_LOGGER = logging.getLogger(__name__)

STEP_CONNECTION = "battery_connection"
STEP_RECONFIGURE = "reconfigure_battery"

_BRAND = "marstek"


def _model_selector() -> SelectSelector:
    return SelectSelector(SelectSelectorConfig(
        options=[
            {"value": "v2", "label": "Ev2"},
            {"value": "v3", "label": "Ev3"},
            {"value": "vA", "label": "A"},
            {"value": "vD", "label": "D"},
        ],
        mode=SelectSelectorMode.DROPDOWN,
    ))


def _slave_selector():
    return vol.All(
        NumberSelector(NumberSelectorConfig(min=1, max=247, step=1, mode=NumberSelectorMode.BOX)),
        vol.Coerce(int),
    )


def _bare_schema(battery_num: int) -> vol.Schema:
    """The add flow's form: fixed defaults, no prefilled host or serial port."""
    return vol.Schema({
        vol.Required(CONF_NAME, default=f"Marstek Venus {battery_num}"): str,
        vol.Optional(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=502): int,
        vol.Optional(CONF_SERIAL_PORT): str,
        vol.Optional(CONF_RS485_GATEWAY, default=False): bool,
        vol.Required(CONF_SLAVE_ID, default=DEFAULT_SLAVE_ID): _slave_selector(),
        vol.Required(CONF_BATTERY_VERSION, default=DEFAULT_VERSION): _model_selector(),
    })


def _stored_schema(current: dict, battery_num: int) -> vol.Schema:
    """The options / reconfigure form, prefilled from the stored battery."""
    serial_default = current.get(CONF_SERIAL_PORT, "")
    # A serial battery stores its path in CONF_HOST too; don't prefill the IP
    # field with the device path.
    host_default = "" if serial_default else current.get(CONF_HOST, "")
    return vol.Schema({
        vol.Required(CONF_NAME, default=current.get(CONF_NAME, f"Marstek Venus {battery_num}")): str,
        vol.Optional(CONF_HOST, default=host_default): str,
        vol.Optional(CONF_PORT, default=current.get(CONF_PORT, 502)): int,
        vol.Optional(CONF_SERIAL_PORT, default=serial_default): str,
        vol.Optional(CONF_RS485_GATEWAY, default=current.get(CONF_RS485_GATEWAY, False)): bool,
        vol.Required(CONF_SLAVE_ID, default=current.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)):
            _slave_selector(),
        vol.Required(
            CONF_BATTERY_VERSION, default=current.get(CONF_BATTERY_VERSION, DEFAULT_VERSION)
        ): _model_selector(),
    })


async def _probe_once(
    ctx: SetupContext, host, port, version, slave_id, serial_port, *, always_pass_serial: bool
) -> bool:
    """One probe; a vD also stores the EMS firmware it reports in ``ctx.work``."""
    from .marstek import MarstekModbusDriver  # local: marstek.py imports this module

    kwargs = {"serial_port": serial_port} if (serial_port or always_pass_serial) else {}
    if version == "vD":
        result, ctx.work["ems_version"] = await MarstekModbusDriver.probe_details(
            host, port, version, slave_id, **kwargs
        )
        return result
    return await MarstekModbusDriver.probe(host, port, version, slave_id, **kwargs)


async def _test_connection(
    flow, ctx: SetupContext, host, port, version, slave_id, serial_port
) -> bool:
    """Probe the battery the way the running flow always has.

    Options flow: free the single Modbus slot first (close the live coordinator of
    this host/slave, probe, reopen it even when the probe failed). Config flow (add
    and reconfigure): probe without touching any coordinator.
    """
    ctx.work["ems_version"] = None
    if ctx.kind is FlowKind.OPTIONS:
        entry_data = (getattr(flow.hass, "data", None) or {}).get(DOMAIN, {}).get(
            ctx.entry.entry_id, {}
        )
        existing_coordinator = None
        for coordinator in entry_data.get("coordinators", []):
            if coordinator.host == host and coordinator.slave_id == slave_id:
                existing_coordinator = coordinator
                break

        if existing_coordinator is not None:
            _LOGGER.info(
                "Reusing coordinator for %s (version=%s) - closing connection for test",
                host, existing_coordinator.battery_version
            )
            async with existing_coordinator.lock:
                await existing_coordinator.driver.close()
                await asyncio.sleep(0.5)
                result = await _probe_once(
                    ctx, host, port, version, slave_id, serial_port, always_pass_serial=True
                )
                await asyncio.sleep(0.3)
                await existing_coordinator.driver.connect()
                if result:
                    _LOGGER.info("Test connection to %s successful, coordinator reconnected", host)
                else:
                    _LOGGER.warning("Test connection to %s failed after closing coordinator", host)
                return result
        _LOGGER.info("No existing coordinator for %s - opening new connection", host)
        return await _probe_once(
            ctx, host, port, version, slave_id, serial_port, always_pass_serial=True
        )

    if serial_port:
        _LOGGER.info("Probing Marstek %s over serial %s slave %s", version, serial_port, slave_id)
    else:
        _LOGGER.info("Probing Marstek %s at %s:%s slave %s", version, host, port, slave_id)
    result = await _probe_once(
        ctx, host, port, version, slave_id, serial_port, always_pass_serial=False
    )
    if not result:
        _LOGGER.error("Failed to connect to %s:%s (brand=%s)", host, port, _BRAND)
    return result


async def _connection(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Connection details and battery model (add flow and options flow)."""
    if ctx.kind is not FlowKind.OPTIONS:
        return await _connect(flow, user_input, ctx)
    # The options flow has always turned an unexpected error here into an abort,
    # and went on to the limits form from inside this step.
    try:
        return await _connect(flow, user_input, ctx)
    except Exception as e:
        _LOGGER.error("Error in options flow battery_connection step: %s", e, exc_info=True)
        return flow.async_abort(reason="unknown_error")


async def _connect(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    options = ctx.kind is FlowKind.OPTIONS
    errors = {}
    battery_num = flow.battery_index + 1

    if user_input is not None:
        battery_version = user_input.get(CONF_BATTERY_VERSION, DEFAULT_VERSION)
        slave_id = user_input.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)
        serial_port = (user_input.get(CONF_SERIAL_PORT) or "").strip()
        rs485_gateway = bool(user_input.get(CONF_RS485_GATEWAY, False))
        host = (user_input.get(CONF_HOST) or "").strip()
        is_serial = bool(serial_port)

        if not is_serial and not host:
            # No IP and no serial port: nothing to connect to.
            errors["base"] = "host_or_serial_required"
        else:
            if is_serial:
                # Serial has no IP:port; the path doubles as the battery's
                # identity (device_key, naming). port stays a placeholder.
                host = serial_port
                port = user_input.get(CONF_PORT, 502)
            elif options:
                port = user_input.get(CONF_PORT, 502)
            else:
                port = user_input[CONF_PORT]

            if not await _test_connection(
                flow, ctx, host, port, battery_version, slave_id, serial_port or None
            ):
                errors["base"] = "cannot_connect"
            else:
                data = {
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_SERIAL_PORT: serial_port,
                    CONF_RS485_GATEWAY: rs485_gateway,
                    CONF_SLAVE_ID: slave_id,
                    CONF_BATTERY_VERSION: battery_version,
                    "brand": _BRAND,
                    "ems_version": ctx.work.get("ems_version"),
                }
                if options:
                    # Inside this step's error guard, as it always was.
                    flow._current_battery_data.update(data)
                    return await flow.async_step_battery_limits()
                return data

    return flow.async_show_form(
        step_id=STEP_CONNECTION,
        data_schema=_stored_schema(ctx.battery or {}, battery_num) if options else _bare_schema(battery_num),
        errors=errors,
        description_placeholders={"battery_num": str(battery_num)},
    )


async def _reconfigure(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Update a Marstek battery's connection settings (reconfigure source).

    The probe never touches the live coordinator here (plan section 8).
    """
    entry = ctx.entry
    current = ctx.battery or {}
    errors = {}
    battery_num = flow.battery_index + 1

    if user_input is not None:
        battery_version = user_input.get(CONF_BATTERY_VERSION, DEFAULT_VERSION)
        slave_id = user_input.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)
        serial_port = (user_input.get(CONF_SERIAL_PORT) or "").strip()
        rs485_gateway = bool(user_input.get(CONF_RS485_GATEWAY, False))
        new_host = (user_input.get(CONF_HOST) or "").strip()
        is_serial = bool(serial_port)

        if is_serial:
            new_host = serial_port  # path doubles as identity (see add flow)
            new_port = user_input.get(CONF_PORT, 502)
        else:
            new_port = user_input.get(CONF_PORT, 502)

        if not is_serial and not new_host:
            errors["base"] = "host_or_serial_required"
        elif not await _test_connection(
            flow, ctx, new_host, new_port, battery_version, slave_id, serial_port or None
        ):
            errors["base"] = "cannot_connect"
        else:
            old_host = current.get(CONF_HOST)
            old_port = current.get(CONF_PORT)
            old_slave = current.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)

            if (
                old_host
                and old_port
                and (old_host != new_host or old_port != new_port or old_slave != slave_id)
            ):
                flow._migrate_battery_registry_ids(
                    entry, old_host, old_port, new_host, new_port, old_slave, slave_id
                )

            # The shared flow lays this over the stored battery.
            return {
                CONF_NAME: user_input[CONF_NAME],
                CONF_HOST: new_host,
                CONF_PORT: new_port,
                CONF_SERIAL_PORT: serial_port,
                CONF_RS485_GATEWAY: rs485_gateway,
                CONF_SLAVE_ID: slave_id,
                CONF_BATTERY_VERSION: battery_version,
                "ems_version": ctx.work.get("ems_version"),
            }

    return flow.async_show_form(
        step_id=STEP_RECONFIGURE,
        data_schema=_stored_schema(current, battery_num),
        errors=errors,
        description_placeholders={"battery_num": str(battery_num)},
    )


class _MarstekSetup(DriverSetup):
    async def reprobe(self, hass: Any, battery_config: dict, host: str) -> bool:
        port = battery_config.get(CONF_PORT)
        version = battery_config.get(CONF_BATTERY_VERSION, DEFAULT_VERSION)
        slave_id = battery_config.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)
        _LOGGER.info("Probing Marstek %s at %s:%s slave %s", version, host, port, slave_id)
        # Never over serial: a serial-port Marstek is not IP-addressed, so DHCP never
        # reaches it. The EMS firmware a vD probe reports is discarded here.
        return await _probe_once(
            SetupContext(FlowKind.CONFIG, None, None, None),
            host, port, version, slave_id, None, always_pass_serial=False,
        )

    def power_ceilings(self, battery_data: dict) -> tuple[int, int]:
        battery_version = battery_data.get(CONF_BATTERY_VERSION, DEFAULT_VERSION)
        ceiling = max_power_for_battery_version(battery_version, battery_data.get("ems_version"))
        return ceiling, ceiling

    def limits_schema(self, battery_data: dict, base_fields: dict, ctx: SetupContext) -> dict:
        # The two flows build the DC field differently: the config flow always opens
        # on fixed defaults, the options flow on the stored battery's values.
        options = ctx.kind is FlowKind.OPTIONS
        # Options flow: the stored battery with this flow's connection data on top.
        current = {**ctx.battery, **battery_data} if options and ctx.battery is not None else None
        fields = dict(base_fields)
        # Only a Marstek (the key is absent on a battery still being added by default)
        # gets the DC field; a brand that is not registered falls back to this setup
        # without it, as it always has.
        if (
            battery_data.get("brand", _BRAND) == _BRAND
            and battery_data.get(CONF_BATTERY_VERSION) in ("vA", "vD")
        ):
            if current is not None:
                dc_default = current.get(
                    CONF_DC_PV_CONNECTED,
                    current.get(CONF_BATTERY_VERSION) in ("vA", "vD"),
                )
            elif options:
                dc_default = battery_data.get(CONF_BATTERY_VERSION) in ("vA", "vD")
            else:
                dc_default = True
            fields[vol.Required(CONF_DC_PV_CONNECTED, default=dc_default)] = BooleanSelector()
        taper_default = (
            current.get(
                CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
                DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
            )
            if current is not None
            else DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED
        )
        fields[vol.Required(CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, default=taper_default)] = bool
        return fields

    def finalize_limits(
        self, merged: dict, user_input: dict, battery_data: dict, ctx: SetupContext
    ) -> None:
        from .marstek import MarstekModbusDriver

        merged[CONF_DC_PV_CONNECTED] = saved_dc_pv_connected(MarstekModbusDriver, merged, user_input)
        merged[CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED] = user_input.get(
            CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED
        )


MARSTEK_SETUP = _MarstekSetup(
    steps={
        STEP_CONNECTION: _connection,
        STEP_RECONFIGURE: _reconfigure,
    },
    entry_step=STEP_CONNECTION,
    reconfigure_entry_step=STEP_RECONFIGURE,
)
