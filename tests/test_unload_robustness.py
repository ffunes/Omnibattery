"""One driver failing to close must not abort the rest of the unload.

``asyncio.gather`` without ``return_exceptions`` re-raised the first
disconnect error, so the hourly balance, consumption tracker and daily
timeline were never saved and the entry's ``hass.data`` was left behind.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import custom_components.omnibattery as integration
from custom_components.omnibattery.const import DOMAIN


async def _noop(*_args, **_kwargs):
    return None


def _coordinator(name, *, fails=False):
    async def disconnect():
        if fails:
            raise ConnectionError("socket already gone")
        closed.append(name)

    closed = []
    coordinator = SimpleNamespace(
        name=name,
        _is_connected=False,  # skip the shutdown register writes
        disconnect=disconnect,
        set_shutting_down=lambda _flag: None,
        closed=closed,
    )
    return coordinator


def test_unload_saves_state_when_a_driver_fails_to_close(monkeypatch):
    monkeypatch.setattr(integration.asyncio, "sleep", _noop)
    saved = []

    def _saver(label, *, fails=False):
        async def save():
            if fails:
                raise OSError("disk full")
            saved.append(label)

        return save

    controller = SimpleNamespace(
        _unloading=False,
        async_stop_background_tasks=_noop,
        _pricing_mgr=SimpleNamespace(
            clear_curtailment_runtime=lambda _r: None,
            clear_negative_price_runtime=lambda _r: None,
        ),
        _high_price_discharge_mgr=SimpleNamespace(clear_runtime=lambda _r: None),
        # One persistence step failing must not skip the next ones either.
        _hourly_balance_mgr=SimpleNamespace(async_unload=_saver("hourly", fails=True)),
        _consumption_tracker=SimpleNamespace(
            async_stop_background_work=_noop,
            async_save_all=_saver("tracker"),
        ),
        _daily_operation_timeline=SimpleNamespace(async_shutdown=_saver("timeline")),
    )
    broken = _coordinator("broken", fails=True)
    healthy = _coordinator("healthy")
    entry = SimpleNamespace(entry_id="entry1")
    other_entry = SimpleNamespace(entry_id="entry2")

    async def unload_platforms(_entry, _platforms):
        return True

    hass = SimpleNamespace(
        data={
            DOMAIN: {
                "entry1": {"coordinators": [broken, healthy], "controller": controller}
            }
        },
        config_entries=SimpleNamespace(
            async_unload_platforms=unload_platforms,
            async_entries=lambda _domain: [entry, other_entry],
        ),
    )

    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True

    assert healthy.closed == ["healthy"]
    assert saved == ["tracker", "timeline"]
    assert "entry1" not in hass.data[DOMAIN]
