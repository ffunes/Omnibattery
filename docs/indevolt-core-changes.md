# Indevolt: changes outside the Indevolt files

Omnibattery's core serves many manufacturers, so Indevolt behaviour lives in
its own files:

- `custom_components/omnibattery/infra/indevolt_client.py`: transport, flash
  guard, pacing and the telemetry scheduler
- `custom_components/omnibattery/infra/indevolt_notify.py`: persistent
  notifications
- `custom_components/omnibattery/drivers/indevolt.py`: the driver and its
  control state machine
- `custom_components/omnibattery/drivers/indevolt_settings.py`: the two-way sync
  of the three user settings (min SOC, max charge and max discharge power)

A change elsewhere is allowed only when Indevolt cannot work without it. Every
such change must be generic, keep other brands' behaviour unchanged by default,
say why it exists in a docstring, be covered by a test, and be listed here.

## Registration touchpoints

These are the same touchpoints every brand has (compare Sessy and Huawei):

| File | Change |
|---|---|
| `drivers/__init__.py` | Export `IndevoltDriver`. |
| `infra/coordinator.py` | Factory branch for `brand == "indevolt"` (it hands the driver `persist_battery_config` and a reader of this battery's config, so the settings sync can store what it adopted or wrote), manufacturer and model labels, and `indevolt` in the existing voltage-taper exclusion tuple. |
| `config_flow.py` | Brand option, connection step (read-only probe), Realtime-mode notice step, reconfigure step, power ceilings in the existing limits step, SOC selector bounds (5–50%, the same range as the min-SOC number, so saving the form unchanged never clamps and rewrites the battery's reserve), and `indevolt` in the existing taper tuples. |
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

### `number.py`: a register-backed SOC number replaces its software twin

- **File:** `number.py`, `async_setup_entry` (about 6 lines).
- **Why:** batteries without hardware SOC cut-offs (`hardware_soc_cutoff=False`)
  get two software sliders, `charging_cutoff_capacity` and
  `discharging_cutoff_capacity`. Indevolt has no max-SOC setting (software, as
  before), but its reserve *is* the min SOC, which the driver now exposes as a
  register-style number so it is written to and adopted from the battery. Both
  entities would share one unique ID. The software slider is now skipped when
  the driver's `number_definitions` already provide that key.
- **Proof:** `tests/test_indevolt_registration.py::test_the_device_backed_min_soc_replaces_its_software_twin`.
- **Other brands:** no other driver defines either key while reporting
  `hardware_soc_cutoff=False`, so they keep both sliders;
  `test_other_software_soc_brands_keep_both_sliders` pins that for Marstek v3.

### `number.py`: a register number shows its written value at once

- **File:** `number.py`, `MarstekVenusNumber.async_set_native_value` (about 5
  lines).
- **Why:** the entity reads `coordinator.data`, which only catches up when the
  key's read group is next polled (up to 5s for "medium"). Home Assistant's own
  device page then snapped the slider back to the old value and jumped to the
  new one seconds later, which looks like a failed write. The Omnibattery panel
  hid this with its own pending state. Found when the Indevolt settings became
  writable; Marstek and Zendure register sliders had the same jump.
- **Change:** after a write the driver accepted, the entity stores the written
  value in `coordinator.data` and reports its state; a refused write keeps the
  old value. The next poll corrects either way.
- **Proof:** `tests/test_marstek_number.py::test_a_refused_register_write_keeps_showing_the_old_value`
  and the extended `test_zendure_inverse_max_power_updates_effective_discharge_limit`.
- **Other brands:** they gain the same immediate update; nothing else changes.

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
- **User settings, both ways.** Min SOC, max charge and max discharge power
  are flash settings on the battery (1142 → 6105, 1138 → 11009, 1147 → 11011).
  The driver exposes them as register-style numbers
  (`discharging_cutoff_capacity`, `max_charge_power`, `max_discharge_power`):
  the core already writes those through `write_control` and adopts the polled
  values back into `min_soc` and the configured power ceilings, as for Marstek
  and Zendure. The driver writes only on that user action (and on setup after
  an options-flow change), debounced, compared first and within a daily
  budget; nothing periodic can reach the config channel. Max SOC has no device
  setting: `hardware_soc_cutoff=False`, so the core enforces it in software and
  weekly full charge needs no write (`set_charge_cutoff` returns False).
- **Hardware power ceilings.** The config-flow probe reports 114/115, which the
  coordinator keeps as the device envelope; the number entities use them as
  their maximum.
- **Why control is paused.** The driver's own `control_status` sensor and a
  persistent notification. Showing it in the panel's warning chip would be a
  separate, optional follow-up.
