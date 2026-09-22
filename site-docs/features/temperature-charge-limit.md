# Temperature charge limit

Reduces charge and discharge power when the battery gets hot. Above a configured temperature limit, power is throttled proportionally and restored as the battery cools down.

The derating is linear: charge power falls from the normal ceiling to a configured floor as the battery temperature crosses the limit and ramp band. The floor is clamped to each battery's minimum operating power (Marstek v2/v3/vA/vD and Zendure = 0 W). Discharge derating can also be enabled to stay below the BMS over-temperature cutoff.

## Dashboard configuration

| Field | Description | Default |
|---|---|---|
| **Temperature limit (°C)** | Charging runs at full power at or below this temperature; derating begins above it. | `40 °C` |
| **Ramp band (°C)** | Temperature range above the limit over which charge power ramps down to the minimum. | `10 °C` |
| **Minimum charge power (%)** | Charge power at the limit plus the band, as a percentage of the normal charge ceiling. `0 %` stops charging when very hot. | `40 %` |
| **Also throttle discharge** | Applies the same temperature derating to discharge power, keeping it below the BMS over-temperature cutoff. | Off |

![Temperature charge limit configuration](../assets/screenshots/configuration/advanced-temperature-charge-limit-config.png){ width="650" style="display: block; margin: 0 auto;"}

The controls are available in all six supported languages.
