# Load exclusion

See [Excluded devices](../configuration/excluded-devices.md) for configuration.

## How it works internally

When an excluded device is active, the controller subtracts its power from the grid consumption before computing the PD controller adjustment:

```
effective_consumption = grid_consumption - excluded_power
error = effective_consumption - target_grid_power
```

This causes the battery to "ignore" that load and not try to compensate it.

### If the device is NOT included in the main sensor

The integration **adds** the excluded device's power to the measured grid consumption (because the main sensor does not see it) and then subtracts it, resulting in the same net effective consumption.

## "Allow solar surplus" option

When active, if the system is operating on solar surplus (battery is charging from surplus), the exclusion does not apply to the charging side. In other words: the battery will not charge to compensate this device's consumption when solar surplus is already available.

This is the basis for **EV vs. battery charging priority**:

| Mode | Battery charges with solar? | Battery discharges for device? |
|---|---|---|
| Excluded, surplus OFF | Yes | No |
| Excluded, surplus ON | **No** — solar goes to device first | No |

### Solar Surplus switch (runtime control)

Each excluded device gets a dedicated **Solar Surplus** switch entity that toggles this behaviour at runtime without reconfiguring the integration. Use it in HA automations to change priority dynamically:

```yaml
# Example: prioritise EV when connected
automation:
  trigger:
    - platform: state
      entity_id: binary_sensor.ev_connected
      to: "on"
  action:
    - service: switch.turn_on
      target:
        entity_id: switch.solar_surplus_wallbox_power
```

![Excluded device power sensor in HA](../assets/screenshots/features/load-exclusion-entities.png){ width="700"  style="display: block; margin: 0 auto;"}

### Dynamic Power Control switch

For a wallbox or another flexible load with its own surplus controller, standard
Solar Surplus can still leave two controllers settled at an undesirable split:
the battery removes export before the wallbox has a chance to ramp up. **Dynamic
Power Control** adds a small state machine around the normal exclusion logic.

The device-active / EV-charging state sensor closes the cold-start gap:
while it requests power but the wallbox still reads 0 W, battery charging stays
blocked so the wallbox can see the export and start. It is required for new
Dynamic Power Control configurations; existing sensor-less entries keep their
measured-power fallback.

On first measured demand it blocks battery charging for 30 seconds. Charging may
then resume only on the export the device left behind. A solar rise triggers a
new 20-second yield, and a zero-power pause is held for 5 minutes so the battery
does not prevent the device from restarting. No maximum-demand sensor is needed.

## EV charger without power telemetry

For EV chargers that only expose a state sensor (no real-time power reading), a dedicated **EV charger without power telemetry** option is available. The same device-active / EV-charging field is used. Legacy entries that stored this state sensor in the old device-sensor field continue to work unchanged.

| Phase | Battery behaviour |
|---|---|
| EV state → Charging (first 5 min) | 0 W — both charge and discharge blocked, PD state frozen |
| EV charging (after 5 min) | Charging from solar surplus allowed; discharge always blocked |
| EV state → anything else | Normal operation |

See [EV charger without power telemetry](../configuration/excluded-devices.md#ev-charger-without-power-telemetry) in the configuration reference for setup details.
