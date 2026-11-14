"""Websocket commands the panel uses to read the forecast history."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

WS_LIST = f"{DOMAIN}/forecast_history/list"
WS_DAY = f"{DOMAIN}/forecast_history/day"
WS_EXPORT = f"{DOMAIN}/forecast_history/export"
EXPORT_KINDS = ("days", "intervals", "revisions")
_REGISTERED_KEY = f"{DOMAIN}_forecast_history_ws_registered"


def _history(hass: HomeAssistant, entry_id: str | None) -> Any:
    """The requested entry's history, or the first entry that has one."""
    domain_data = hass.data.get(DOMAIN) or {}
    if entry_id:
        data = domain_data.get(entry_id)
        return data.get("forecast_history") if isinstance(data, dict) else None
    for data in domain_data.values():
        if isinstance(data, dict) and data.get("forecast_history") is not None:
            return data["forecast_history"]
    return None


def _not_found(connection: Any, msg: dict[str, Any]) -> None:
    connection.send_error(msg["id"], "not_found", "Forecast history is not available")


@websocket_api.websocket_command(
    {vol.Required("type"): WS_LIST, vol.Optional("entry_id"): str}
)
@callback
def ws_forecast_history_list(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    history = _history(hass, msg.get("entry_id"))
    if history is None:
        _not_found(connection, msg)
        return
    connection.send_result(msg["id"], history.list_days())


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_DAY,
        vol.Required("date"): str,
        vol.Optional("entry_id"): str,
    }
)
@callback
def ws_forecast_history_day(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    history = _history(hass, msg.get("entry_id"))
    if history is None:
        _not_found(connection, msg)
        return
    detail = history.day_detail(msg["date"])
    if detail is None:
        connection.send_error(msg["id"], "invalid_format", "Invalid date")
        return
    connection.send_result(msg["id"], detail)


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_EXPORT,
        vol.Optional("kind", default="days"): vol.In(EXPORT_KINDS),
        vol.Optional("entry_id"): str,
    }
)
@callback
def ws_forecast_history_export(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    history = _history(hass, msg.get("entry_id"))
    if history is None:
        _not_found(connection, msg)
        return
    kind = msg.get("kind", "days")
    connection.send_result(
        msg["id"],
        {"kind": kind, "filename": f"omnibattery_forecast_{kind}.csv", "csv": history.export_csv(kind)},
    )


@callback
def async_register_forecast_history_ws(hass: HomeAssistant) -> None:
    """Register the commands once per Home Assistant instance."""
    # Kept outside hass.data[DOMAIN], whose values are per-entry mappings.
    if hass.data.get(_REGISTERED_KEY):
        return
    websocket_api.async_register_command(hass, ws_forecast_history_list)
    websocket_api.async_register_command(hass, ws_forecast_history_day)
    websocket_api.async_register_command(hass, ws_forecast_history_export)
    hass.data[_REGISTERED_KEY] = True
