# Troubleshooting

## Dynamic Pricing reports a deadline shortfall

Check `deadline_shortfall_kwh`, `earliest_projected_depletion`, `slot_deadlines` and `chronological_plan_reason` on `binary_sensor.omnibattery_predictive_charging_active`. A shortfall means the best physically feasible plan cannot deliver all required energy before the projected minimum-SOC crossing. Common causes are an explicit maximum-price threshold, no eligible slot before the deadline, insufficient charging power, battery headroom or manual/time-slot ownership. A later cheap slot is deliberately not shown as covering an earlier need. The integration continues normal control and never bypasses explicit safety limits.

## Marstek app compatibility

You do **not** need to make any changes in the Marstek app for the integration to work — including disabling the energy meter setting or changing any configuration. The integration works alongside the app without requiring any app-side adjustments.

However, **do not change any operating mode or setting from the Marstek app while the Home Assistant integration is running**. Doing so will break compatibility, and you will need to disable and re-enable the integration to restore normal operation.

---

## Battery does not respond to commands

1. Verify that the Modbus TCP converter (Elfin-EW11 or similar) is reachable by IP from Home Assistant.
2. Check that the configured port is correct (default `502`).
3. Make sure the **RS485 Control Mode** switch is enabled.
4. Ensure the configured battery version matches the actual hardware.

!!! note "Delay for v3/vA/vD"
    v3, vA and vD batteries require at least 150 ms between consecutive Modbus messages. The integration applies this automatically based on the configured version.

---

## PD controller oscillates

The system continuously switches between charging and discharging.

**Possible causes and solutions:**

| Cause | Solution |
|---|---|
| Deadband too small | The default ±40 W is appropriate for most installations |
| Grid sensor with high latency | Use a sensor with frequent updates (1–2 s) |
| Loads with sudden start-up | Configure the load as an [excluded device](configuration/excluded-devices.md) |

---

## Battery alarm or fault notification received

The integration monitors the battery's `Alarm Status` and `Fault Status` registers (v2 only) every 5 seconds. When a new bit is set a persistent notification appears in Home Assistant with the exact condition name (e.g. *BAT Overvoltage*, *Fan Abnormal Warning*). The notification is automatically dismissed once all conditions clear.

**Notification severity levels:**

| Title prefix | Meaning |
|---|---|
| 🚨 Battery Fault | At least one fault bit is active — requires immediate attention |
| ⚠️ Battery Warning | At least one alarm bit is active — monitor the situation |

**What to do when you receive a notification:**

1. Check the **`System Alarm Status`** sensor on the *Omnibattery System* device — its attributes list which battery is affected and what conditions are active.
2. Check the individual **Alarm Status** and **Fault Status** sensors on the affected battery device for the full current state.
3. Consult the Marstek Venus documentation or the Marstek app for the specific fault code.
4. If the condition does not clear automatically, consider restarting the battery or contacting Marstek support.

!!! note "v2 batteries only"
    Alarm and fault register monitoring is only available for v2 hardware. v3, vA and vD batteries do not expose these registers via Modbus.

---

## Predictive charging does not activate

1. Verify that the solar forecast sensor is available and has a value.
2. Check the `price_data_status` attribute of the `predictive_charging_active` sensor (Dynamic Pricing mode).
3. Review HA notifications: the 00:05 evaluation reports its result.
4. Make sure the energy balance actually requires charging (there may already be enough energy).

### The consumption source says `legacy_daily`

This is expected while the 28-day profile is learning or when the requested
intervals do not meet its coverage contract. Check
`sensor.omnibattery_expected_home_consumption_profile` and the integration
diagnostics endpoint. Changing a source or an excluded-load adjustment keeps
every learned day, and a timezone change re-bins them by the offset between the
two zones; Recorder backfill then rebuilds whatever is still missing in the
background. A gap longer than five minutes is not interpolated.

### The solar profile remains immature or falls back

This is safe and expected during the first days. Learning requires direct PV
power from the configured external sensor or readable MPPT channels, at least
seven closed quality days, recent coverage and enough evidence in the requested
future range. Invalid, negative and long-gap samples are excluded. Curtailment
signals can exclude intervals, and a source or capacity change starts a new
generation. Check the `solar_profile` diagnostics section and
`solar_timeline_fallback_reason`; the profile does not repair a bad weather
forecast or model unobservable curtailment.

---

## Metering device unavailable or losing connectivity

If the grid sensor (e.g. a power meter with a poor Wi-Fi connection) goes offline, the controller behaves differently depending on how the sensor fails.

### Sensor reports `unavailable` or `unknown`

The control loop exits immediately without sending any new command. The batteries **hold their last commanded power level** until the sensor comes back online.

### Sensor freezes (value stops updating)

The integration detects that the sensor's timestamp has not changed:

- For up to **15 cycles (~30 seconds)** it keeps the last command unchanged.
- After that grace period it performs a safety recalculation using the frozen value, with the derivative term suppressed to avoid power spikes.

### Summary

| Sensor state | Behaviour |
|---|---|
| `unavailable` / `unknown` | Control loop skips — batteries hold last power level |
| Frozen value (no new readings) | ~30 s grace period, then recalculates with stale value |

!!! warning "No automatic fallback to 0 W"
    If the meter goes unavailable while the battery was, for example, discharging at 2000 W, it will **continue discharging at 2000 W** until the meter recovers. There is no built-in timeout that ramps the battery to idle. Consider improving the Wi-Fi reliability of your metering device, or using a wired/Zigbee alternative if dropouts are frequent.

---

## Reporting an issue — Download diagnostics

When opening a bug report or asking for help, attach the JSON generated by Home Assistant's **Download diagnostics** action for the Omnibattery config entry. The dump contains the persisted configuration together with battery connection health, driver capabilities, non-responsive tracking, and dynamic-pricing runtime details. Sensitive connection fields and known identifiers are redacted by the integration.

**How to download it:**

1. Go to **Settings → Devices & Services**.
2. Open the **Omnibattery** integration and its config entry.
3. Click **Download diagnostics**.
4. Attach the resulting JSON file to the support request.

Review the file before sharing it and remove anything specific to your installation that you do not want to disclose.

---

## Debug logging

Enable `debug` for the integration by clicking in "Enable debug logging" button in the integration settings. Once you have run it for the appropriate time, disable it to avoid filling the logs, and a log file will be created with the debug information.
