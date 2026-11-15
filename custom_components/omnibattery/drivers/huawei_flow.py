"""Huawei config flow: connection, conditional cascade (slave) step, two-step reconfigure.

Imports only ``drivers.setup``, ``const`` and Home Assistant. ``huawei.py`` imports
``HUAWEI_SETUP`` from here to set ``HuaweiSolarDriver.SETUP``, so this module cannot
import ``huawei.py`` at the top (cycle): everything that needs it imports it inside
the function. It is the same class object, so patching ``HuaweiSolarDriver.probe``,
``scan_slave_ids`` or ``find_emma_slave_id`` works as before.

The state the steps share (``pending``, ``candidates``, ``reconfigure_current``) lives
in ``ctx.work``. ``candidates`` is read with a default of ``[]``; no ``_huawei_*`` attribute
exists on the flow instance.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..const import (
    CONF_DC_PV_CONNECTED,
    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    CONF_SLAVE_ID,
    DEFAULT_SLAVE_ID,
)
from .setup import (
    DriverSetup,
    FlowKind,
    ReplacesBattery,
    SetupContext,
    saved_dc_pv_connected,
)

_LOGGER = logging.getLogger(__name__)

STEP_CONNECTION = "battery_connection_huawei"
STEP_SLAVE = "battery_connection_huawei_slave"
STEP_RECONFIGURE = "reconfigure_battery_huawei"
STEP_RECONFIGURE_SLAVE = "reconfigure_battery_huawei_slave"

# The integration that provides the Huawei control services, and the domain it
# identifies its devices under.
HUAWEI_SOLAR_DOMAIN = "huawei_solar"

# A Huawei inverter accepts one Modbus connection, so a second client needs a
# proxy in front. Supplied as a placeholder rather than written into the strings:
# hassfest rejects a literal URL in a translation.
_MODBUS_PROXY_URL = "https://github.com/Akulatraxas/ha-modbusproxy"

_HUAWEI_MAX_POWER_W = 15000


def _huawei_power_ceilings(battery_data: dict) -> tuple[int, int]:
    """Upper bound the limits form allows for a Huawei battery.

    Registers 37046/37048 report what the battery permits *right now*, and that
    moves with the pack count — a third pack raises it. Treating a momentary
    reading as a hard ceiling locks the user out of a figure their installation
    can genuinely reach, so it serves as the starting value instead.

    What the installation cannot exceed is the inverter's own maximum active
    power (30075): everything, charge and discharge alike, passes through it.
    Where that is unknown the probe's own figure stands in, and the constant is
    only a sanity bound against a malformed reading.
    """
    inverter_max = int(battery_data.get("device_inverter_max_power") or 0)
    charge = int(battery_data.get("device_max_charge_power") or 5000)
    discharge = int(battery_data.get("device_max_discharge_power") or 5000)
    return (
        max(100, min(_HUAWEI_MAX_POWER_W, inverter_max or charge, _HUAWEI_MAX_POWER_W)),
        max(100, min(_HUAWEI_MAX_POWER_W, inverter_max or discharge, _HUAWEI_MAX_POWER_W)),
    )


def _huawei_inverter_serial(hass, device_id: str) -> str | None:
    """Serial of the inverter a huawei_solar battery device belongs to.

    The battery device hangs off its inverter via ``via_device``, and
    huawei_solar identifies that inverter by its serial — the same string the
    inverter reports over Modbus.
    """
    registry = dr.async_get(hass)
    device = registry.async_get(device_id)
    if device is None:
        return None
    parent = registry.async_get(device.via_device_id) if device.via_device_id else None
    for candidate in (parent, device):
        if candidate is None:
            continue
        if candidate.serial_number:
            return candidate.serial_number
        for domain, identifier in candidate.identifiers:
            if domain == HUAWEI_SOLAR_DOMAIN:
                return identifier
    return None


def _huawei_device_matches_inverter(
    hass: Any,
    device_id: str | None,
    serial: str | None,
    slave_id: int | None,
) -> bool:
    """Whether the selected huawei_solar device belongs to the probed inverter.

    Missing serials are treated as unknown rather than contradictory: older
    huawei_solar versions and some firmware do not expose one. A known
    disagreement, however, must stop both setup and reconfiguration before the
    telemetry/command paths can be paired with different inverters.
    """
    if not device_id or not serial:
        return True
    device_serial = _huawei_inverter_serial(hass, device_id)
    if not device_serial or serial.upper() in device_serial.upper():
        return True
    _LOGGER.warning(
        "Huawei device %s belongs to inverter %s, but slave %s is %s",
        device_id, device_serial, slave_id, serial,
    )
    return False


def _huawei_slave_schema(candidates: list) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_SLAVE_ID, default=str(candidates[0][0]) if candidates else "1"):
            SelectSelector(SelectSelectorConfig(
                options=[
                    {"value": str(sid), "label": f"{model} — Slave {sid}"}
                    for sid, model, _battery in candidates
                ],
                mode=SelectSelectorMode.LIST,
            )),
    })


def _huawei_schema(current_battery: dict, battery_num: int) -> vol.Schema:
    """Form for a Huawei battery.

    The battery device is optional because it is only needed for the service
    control path; with direct writes the driver addresses the inverter over
    Modbus and needs nothing from huawei_solar. Requiring it there would make
    the form impossible to submit on an installation that does not run that
    integration at all. Which of the two is missing is checked on submit.
    """
    return vol.Schema({
        vol.Required(
            CONF_NAME,
            default=current_battery.get(CONF_NAME, f"Huawei LUNA2000 {battery_num}"),
        ): str,
        vol.Required(CONF_HOST, default=current_battery.get(CONF_HOST, "")): str,
        vol.Optional(CONF_PORT, default=current_battery.get(CONF_PORT, 502)): int,
        # No default: an empty field means "go and find it". The id is not
        # derivable, so prefilling a guess would only invite the user to
        # accept a wrong one.
        # A selector, not a bare vol.Any: the frontend is handed a
        # serialised schema, and vol.Any has no serialised form — a form
        # containing one cannot be drawn at all.
        vol.Optional(
            CONF_SLAVE_ID,
            description={"suggested_value": current_battery.get(CONF_SLAVE_ID)},
        ): NumberSelector(
            NumberSelectorConfig(min=0, max=247, step=1, mode=NumberSelectorMode.BOX)
        ),
        vol.Optional(
            "huawei_direct_write",
            default=current_battery.get("huawei_direct_write", False),
        ): bool,
        vol.Optional(
            "huawei_battery_device",
            description={
                "suggested_value": current_battery.get("huawei_battery_device_id")
            },
        ): DeviceSelector(
            DeviceSelectorConfig(integration=HUAWEI_SOLAR_DOMAIN, model="Batteries")
        ),
    })


async def _connection(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Configure a Huawei SUN2000 + LUNA2000.

    Control takes one of two paths. By default set-points go through the
    Huawei Solar integration's services, which address the battery by
    device. With direct Modbus writes the driver addresses the inverter
    itself and needs nothing from that integration — which is why the device
    field is optional and only checked when it is actually used.
    """
    from .huawei import HuaweiSolarDriver  # local: huawei.py imports this module

    errors = {}
    battery_num = flow.battery_index + 1
    current_battery = (ctx.battery or {}) if ctx.kind is FlowKind.OPTIONS else {}

    if user_input is not None:
        host = (user_input[CONF_HOST] or "").strip()
        port = int(user_input.get(CONF_PORT, 502))
        raw_slave = user_input.get(CONF_SLAVE_ID)
        slave_id = None if raw_slave in (None, "") else int(raw_slave)
        device_id = user_input.get("huawei_battery_device") or ""
        direct_write = bool(user_input.get("huawei_direct_write", False))
        ctx.work["pending"] = {
            CONF_NAME: user_input[CONF_NAME],
            CONF_HOST: host,
            CONF_PORT: port,
            "huawei_battery_device_id": device_id,
            "huawei_direct_write": direct_write,
        }
        if not direct_write and not device_id:
            # Without direct writes every set-point is a service call, and
            # those address the battery by device. A missing integration is
            # a different problem than an unanswered question, so say which.
            errors["huawei_battery_device"] = (
                "huawei_device_required"
                if flow.hass.config_entries.async_entries(HUAWEI_SOLAR_DOMAIN)
                else "huawei_solar_missing"
            )
        elif slave_id is not None:
            _LOGGER.info(
                "Probing Huawei inverter at %s:%s (slave %s)", host, port, slave_id
            )
            ok, model, max_charge, max_discharge, serial, inverter_max = await HuaweiSolarDriver.probe(
                flow.hass, host, port, slave_id
            )
            if ok:
                result = await _store(
                    flow, ctx, slave_id, model, max_charge, max_discharge, serial,
                    inverter_max, errors,
                )
                if result is not None:
                    return result
            else:
                # An id that does not answer is a guess worth replacing, not
                # a reason to send the user away.
                result = await _search(flow, ctx, host, port, errors)
                if result is not None:
                    return result
        else:
            result = await _search(flow, ctx, host, port, errors)
            if result is not None:
                return result

    return flow.async_show_form(
        step_id=STEP_CONNECTION,
        data_schema=_huawei_schema(current_battery, battery_num),
        errors=errors,
        description_placeholders={
            "battery_num": str(battery_num),
            "proxy_url": _MODBUS_PROXY_URL,
        },
    )


async def _search(flow, ctx: SetupContext, host: str, port: int, errors: dict) -> FlowResult | dict | None:
    """Look for inverters on the bus; returns None when nothing usable was found.

    One match is taken straight away. Several mean a cascade, which only the
    user can resolve — a battery belongs to one of them.
    """
    from .huawei import HuaweiSolarDriver

    _LOGGER.info("Scanning %s:%s for Huawei inverters", host, port)
    found = await HuaweiSolarDriver.scan_slave_ids(flow.hass, host, port)
    with_battery = [candidate for candidate in found if candidate[2]]
    if len(with_battery) == 1:
        sid = with_battery[0][0]
        ok, model, max_charge, max_discharge, serial, inverter_max = await HuaweiSolarDriver.probe(
            flow.hass, host, port, sid
        )
        if ok:
            _LOGGER.info("Found a Huawei battery on slave %s", sid)
            # A mismatch leaves its own error behind and must not be
            # papered over by the scan verdict below.
            return await _store(
                flow, ctx, sid, model, max_charge, max_discharge, serial, inverter_max, errors
            )
    if len(with_battery) > 1:
        ctx.work["candidates"] = with_battery
        return await flow.async_step_battery_connection_huawei_slave()
    # A reachable inverter with no SOC means no battery is attached, which is
    # a different mistake than an unreachable address.
    errors["base"] = "no_battery" if found else "cannot_connect"
    return None


async def _slave(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Pick which inverter on the bus this battery belongs to."""
    from .huawei import HuaweiSolarDriver

    candidates = ctx.work.get("candidates", [])
    if user_input is not None:
        slave_id = int(user_input[CONF_SLAVE_ID])
        pending = ctx.work["pending"]
        ok, model, max_charge, max_discharge, serial, inverter_max = await HuaweiSolarDriver.probe(
            flow.hass, pending[CONF_HOST], pending[CONF_PORT], slave_id
        )
        errors: dict[str, str] = {}
        if ok:
            result = await _store(
                flow, ctx, slave_id, model, max_charge, max_discharge, serial, inverter_max,
                errors, "base",
            )
            if result is not None:
                return result
        return flow.async_show_form(
            step_id=STEP_SLAVE,
            data_schema=_huawei_slave_schema(candidates),
            errors=errors or {"base": "cannot_connect"},
            description_placeholders={"count": str(len(candidates))},
        )

    return flow.async_show_form(
        step_id=STEP_SLAVE,
        data_schema=_huawei_slave_schema(candidates),
        description_placeholders={"count": str(len(candidates))},
    )


async def _store(
    flow, ctx: SetupContext, slave_id, model, max_charge, max_discharge, serial, inverter_max,
    errors, error_key: str = "huawei_battery_device",
) -> dict | None:
    """Commit a validated Huawei battery, or refuse a mismatched pairing.

    On the service path the battery is named twice over: once as a Modbus
    address and once as a device in the registry. Nothing forces those to be
    the same inverter, and on a cascade they easily are not — telemetry would
    then come from one unit while the commands went to another. The inverter
    serial shows up on both sides, so the pairing can be checked. Returns
    None with ``errors`` filled when it does not hold.
    """
    from .huawei import HuaweiSolarDriver

    pending = ctx.work["pending"]
    device_id = pending.get("huawei_battery_device_id")
    if not _huawei_device_matches_inverter(
        flow.hass, device_id, serial, slave_id
    ):
        errors[error_key] = "huawei_device_mismatch"
        return None
    # The shared flow lays this over the battery in progress and goes to the limits.
    data = dict(flow._current_battery_data)
    data.update({
        **pending,
        CONF_SLAVE_ID: slave_id,
        "brand": "huawei",
        "huawei_model": model,
    })
    if max_charge:
        data["device_max_charge_power"] = int(max_charge)
    if max_discharge:
        data["device_max_discharge_power"] = int(max_discharge)
    if inverter_max:
        data["device_inverter_max_power"] = int(inverter_max)
    # An EMMA on the same bus carries the installation's grid meter. Finding
    # it here means a user with one gets a grid reading fast enough to
    # control against without configuring anything.
    emma = await HuaweiSolarDriver.find_emma_slave_id(
        flow.hass, pending[CONF_HOST], pending[CONF_PORT]
    )
    if emma is not None:
        data["huawei_emma_slave_id"] = emma
    # What the battery reports today is the sensible starting value; the
    # form's ceiling is wider, because adding a pack raises it.
    for key, probed in (
        ("max_charge_power", max_charge),
        ("max_discharge_power", max_discharge),
    ):
        if probed and not data.get(key):
            data[key] = int(probed)
    return data


async def _reconfigure(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Update a Huawei battery's connection without asking Marstek questions.

    Without this branch the reconfigure flow offered the Marstek form and
    probe: a battery version, a slave id meaning something else, and no way
    to reach the fields this brand actually needs.
    """
    from .huawei import HuaweiSolarDriver

    entry = ctx.entry
    current = ctx.battery or {}
    errors: dict[str, str] = {}

    if user_input is not None:
        host = (user_input[CONF_HOST] or "").strip()
        port = int(user_input.get(CONF_PORT, 502))
        raw_slave = user_input.get(CONF_SLAVE_ID)
        slave_id = None if raw_slave in (None, "") else int(raw_slave)
        device_id = user_input.get("huawei_battery_device") or ""
        direct_write = bool(user_input.get("huawei_direct_write", False))

        if not direct_write and not device_id:
            errors["huawei_battery_device"] = (
                "huawei_device_required"
                if flow.hass.config_entries.async_entries(HUAWEI_SOLAR_DOMAIN)
                else "huawei_solar_missing"
            )
        else:
            found = slave_id
            cascade = []
            if found is None:
                candidates = await HuaweiSolarDriver.scan_slave_ids(flow.hass, host, port)
                with_battery = [candidate for candidate in candidates if candidate[2]]
                if len(with_battery) > 1:
                    # A cascade is not a failure to connect; only the user
                    # can say which inverter this battery belongs to.
                    cascade = with_battery
                elif with_battery:
                    found = with_battery[0][0]
            ok, model, max_charge, max_discharge, serial, inverter_max = (
                await HuaweiSolarDriver.probe(flow.hass, host, port, found)
                if found is not None
                else (False, None, None, None, None, None)
            )
            if cascade:
                ctx.work["candidates"] = cascade
                ctx.work["pending"] = {
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_HOST: host,
                    CONF_PORT: port,
                    "huawei_battery_device_id": device_id,
                    "huawei_direct_write": direct_write,
                }
                ctx.work["reconfigure_current"] = current
                return await flow.async_step_reconfigure_battery_huawei_slave()
            if not ok:
                errors["base"] = "cannot_connect"
            elif not _huawei_device_matches_inverter(
                flow.hass, device_id, serial, found
            ):
                # Same check the setup flow makes: telemetry and commands
                # must reach the same inverter, which a cascade can break.
                errors["huawei_battery_device"] = "huawei_device_mismatch"
            else:
                return await _reconfigure_store(
                    flow, entry, current, user_input, host, port, found,
                    model, max_charge, max_discharge, inverter_max,
                )
    return flow.async_show_form(
        step_id=STEP_RECONFIGURE,
        data_schema=_huawei_schema(current, flow.battery_index + 1),
        errors=errors,
        description_placeholders={"battery_num": str(flow.battery_index + 1)},
    )


async def _reconfigure_slave(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Pick which inverter of a cascade this battery belongs to."""
    from .huawei import HuaweiSolarDriver

    candidates = ctx.work.get("candidates", [])
    if user_input is not None:
        entry = ctx.entry
        current = ctx.work["reconfigure_current"]
        pending = ctx.work["pending"]
        slave_id = int(user_input[CONF_SLAVE_ID])
        ok, model, max_charge, max_discharge, serial, inverter_max = (
            await HuaweiSolarDriver.probe(
                flow.hass, pending[CONF_HOST], pending[CONF_PORT], slave_id
            )
        )
        if ok:
            if not _huawei_device_matches_inverter(
                flow.hass,
                pending.get("huawei_battery_device_id"),
                serial,
                slave_id,
            ):
                # The selected device is not editable on the cascade form;
                # return to the Huawei form so the user can choose the
                # matching device or switch to direct writes.
                return flow.async_show_form(
                    step_id=STEP_RECONFIGURE,
                    data_schema=_huawei_schema(
                        current, flow.battery_index + 1
                    ),
                    errors={"huawei_battery_device": "huawei_device_mismatch"},
                    description_placeholders={
                        "battery_num": str(flow.battery_index + 1)
                    },
                )
            return await _reconfigure_store(
                flow, entry, current, pending, pending[CONF_HOST], pending[CONF_PORT],
                slave_id, model, max_charge, max_discharge, inverter_max,
            )
        return flow.async_show_form(
            step_id=STEP_RECONFIGURE_SLAVE,
            data_schema=_huawei_slave_schema(candidates),
            errors={"base": "cannot_connect"},
            description_placeholders={"count": str(len(candidates))},
        )
    return flow.async_show_form(
        step_id=STEP_RECONFIGURE_SLAVE,
        data_schema=_huawei_slave_schema(candidates),
        description_placeholders={"count": str(len(candidates))},
    )


async def _reconfigure_store(
    flow, entry, current, values, host, port, slave_id,
    model, max_charge, max_discharge, inverter_max,
) -> dict:
    """The updated battery, carrying its history with it (registry ids follow the address)."""
    from .huawei import HuaweiSolarDriver

    old_host = current.get(CONF_HOST)
    old_port = current.get(CONF_PORT, 502)
    old_slave = current.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)
    # The slave id is part of a battery's identity, so changing it renames
    # every entity and the device itself unless the registry follows.
    if old_host and (old_host != host or old_port != port or old_slave != slave_id):
        flow._migrate_battery_registry_ids(
            entry, old_host, old_port, host, port, old_slave, slave_id
        )

    device_id = (
        values.get("huawei_battery_device_id")
        or values.get("huawei_battery_device")
        or ""
    )
    updated = dict(current)
    updated.update({
        CONF_NAME: values[CONF_NAME],
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_SLAVE_ID: slave_id,
        "brand": "huawei",
        "huawei_battery_device_id": device_id,
        "huawei_direct_write": bool(values.get("huawei_direct_write", False)),
        "huawei_model": model,
    })
    if max_charge:
        updated["device_max_charge_power"] = int(max_charge)
    if max_discharge:
        updated["device_max_discharge_power"] = int(max_discharge)
    if inverter_max:
        updated["device_inverter_max_power"] = int(inverter_max)

    # Re-detect rather than carry over: pointed at a different endpoint, a
    # remembered unit id would have the driver reading a grid meter that is
    # not there, or worse, some other device answering on that id.
    emma = await HuaweiSolarDriver.find_emma_slave_id(flow.hass, host, port)
    if emma is not None:
        updated["huawei_emma_slave_id"] = emma
    else:
        updated.pop("huawei_emma_slave_id", None)

    # Replaces the stored battery (the shared flow would otherwise lay it back
    # over the old one and bring the popped key back).
    return ReplacesBattery(updated)


class _HuaweiSetup(DriverSetup):
    def power_ceilings(self, battery_data: dict) -> tuple[int, int]:
        return _huawei_power_ceilings(battery_data)

    def finalize_limits(
        self, merged: dict, user_input: dict, battery_data: dict, ctx: SetupContext
    ) -> None:
        from .huawei import HuaweiSolarDriver

        merged[CONF_DC_PV_CONNECTED] = saved_dc_pv_connected(HuaweiSolarDriver, merged, user_input)
        merged[CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED] = False


HUAWEI_SETUP = _HuaweiSetup(
    steps={
        STEP_CONNECTION: _connection,
        STEP_SLAVE: _slave,
        STEP_RECONFIGURE: _reconfigure,
        STEP_RECONFIGURE_SLAVE: _reconfigure_slave,
    },
    entry_step=STEP_CONNECTION,
    reconfigure_entry_step=STEP_RECONFIGURE,
)
