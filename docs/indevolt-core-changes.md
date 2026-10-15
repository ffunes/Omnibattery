# Indevolt: changes outside the Indevolt files

Omnibattery's core serves many manufacturers, so Indevolt behaviour lives in
its own files:

- `custom_components/omnibattery/infra/indevolt_client.py`: transport, flash
  guard, pacing and the telemetry scheduler
- `custom_components/omnibattery/infra/indevolt_notify.py`: persistent
  notifications
- `custom_components/omnibattery/drivers/indevolt.py`: the driver and its
  control state machine

A change elsewhere is allowed only when Indevolt cannot work without it. Every
such change must be generic, keep other brands' behaviour unchanged by default,
say why it exists in a docstring, be covered by a test, and be listed here.

## Registration touchpoints

These are the same touchpoints every brand has (compare Sessy and Huawei):

| File | Change |
|---|---|
| `drivers/__init__.py` | Export `IndevoltDriver`. |
| `infra/coordinator.py` | Factory branch for `brand == "indevolt"`, manufacturer and model labels, and `indevolt` in the existing voltage-taper exclusion tuple. |
| `config_flow.py` | Brand option, connection step (read-only probe), Realtime-mode notice step, reconfigure step, power ceilings in the existing limits step, SOC selector bounds, and `indevolt` in the existing taper tuples. |
| `switch.py` | The per-battery "100% Charge Voltage Taper" switch now uses the config flow's taper brand list, so it is no longer shown for Indevolt (or for Sessy, Hoymiles and Huawei, where it also had no effect). Covered by `tests/test_taper_switch_brands.py`. |
| `strings.json`, `translations/*.json` | Texts for the new steps and the new sensor names. |

## Core changes beyond registration

### `BatteryDriver.dynamic_charge_limit_w` (charge-side counterpart of `dynamic_discharge_limit_w`)

- **Files:** `drivers/base.py` (new method, default `None`) and `__init__.py`
  (`_apply_driver_dynamic_limit` takes `charging=True`; `_battery_power_limit`
  applies it on the charge path). About 30 lines including docstrings.
- **Why:** while an Indevolt is not in Realtime mode it refuses every command.
  For discharge the existing `dynamic_discharge_limit_w` reports 0 and battery
  selection skips the battery. For charge there was no equivalent: the
  controller picked the paused battery (lowest SOC charges first), its share
  went undelivered, and only the non-responsive tracker excluded it, retrying
  every 5 minutes and raising a "not delivering" Repair after 30 minutes for
  what is a deliberate mode choice.
- **Proof:** `tests/test_indevolt_allocation.py::test_a_paused_indevolt_gets_no_charge`
  failed before the hook (the paused Indevolt was selected) and passes with it.
- **Other brands:** the default returns `None`, which keeps the static limit.
  `test_other_drivers_keep_their_limits` pins that, and the full suite is
  unchanged.

Everything else was met with existing mechanisms:

- **Pacing whatever the core polls.** The driver serves `read_telemetry` from
  its scheduler's cache; the coordinator's poll cadence never reaches the
  device.
- **Slow actuator.** The driver declares `actuator_latency_s` and
  `readback_latency_s`, so the core's zero-cross hold, settle window and
  poll-time delivery judgement apply, as for Zendure and Sessy.
- **A paused battery.** While control is paused, both dynamic limits return
  0, so selection skips the battery; `apply_setpoint` returns `ok=False` with
  the reason and `net_power_from_data` returns `None`, so a command that still
  arrives (for example from a manual slot) is refused visibly. A 0 W command
  is accepted without sending anything, because no command of Omnibattery's
  runs on the battery; the core's manual-control handoff relies on that.
- **SOC limits.** `hardware_soc_cutoff=False`: the core enforces min/max SOC in
  software (like Marstek v3). Nothing is written to the device.
- **Device power limits.** The driver publishes its device ceilings under the
  existing `max_charge_power` / `max_discharge_power` telemetry keys, which the
  coordinator already adopts as the device envelope for drivers without a
  writable limit.
- **Why control is paused.** The driver's own `control_status` sensor and a
  persistent notification. Showing it in the panel's warning chip would be a
  separate, optional follow-up.
