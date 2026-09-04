# Capacity protection (aka peak shaving)

Reserves a portion of battery capacity to cover demand spikes that exceed a configurable power threshold. Instead of the battery covering all household consumption, it holds back energy and only discharges to compensate the portion of demand above the peak limit — keeping capacity in reserve for when it is actually needed.

## Behaviour without capacity protection active

The PD controller covers all household consumption → the battery can fully discharge if consumption is high and sustained.

## Behaviour with capacity protection active

When SOC is below the threshold:
- The battery does **not** cover all consumption.
- It only discharges to compensate the portion of consumption that exceeds the configured **peak power limit**.

```
battery_power = max(0, grid_consumption - peak_limit)
```

## Dashboard configuration

| Field | Description | Default | Range |
|---|---|---|---|
| **SOC threshold (%)** | Capacity protection activates below this SOC. | `30 %` | 20–100 % |
| **Peak limit (W)** | Grid power threshold. When capacity protection is active, the battery discharges only the excess above this limit. | `2500 W` | 500–10000 W |

![Capacity protection configuration](../assets/screenshots/configuration/advanced-capacity-protection-config.png){ width="650" style="display: block; margin: 0 auto;"}

## Example

```
Peak limit: 3,000 W
Current consumption: 4,500 W

Battery power = 4,500 - 3,000 = 1,500 W
Grid covers 3,000 W and the battery only 1,500 W
```

If consumption were 2,000 W (< limit), the battery would not discharge at all.

## Interaction with predictive grid charging

During an active predictive slot, household demand always receives priority
over grid charging. The controller first reduces the battery's charge command;
if the import ceiling is reached, it commands idle and waits for inverter and
meter telemetry to settle before considering any discharge.

After settling:

- With Capacity Protection enabled, Peak Shaving discharges only the excess
  above `min(capacity_protection_limit, max_contracted_power)`.
- Independently of the Capacity Protection switch, physical import above
  `max_contracted_power` activates contracted-power emergency protection. Loads
  excluded from ordinary battery coverage cannot be excluded from this check,
  because the grid connection still sees them.
- Price-based discharge restrictions and protected negative-price windows may
  not suppress a legitimate Peak Shaving/emergency command. Physical and user
  safety restrictions — minimum SOC, battery availability, manual/slot
  ownership, backup, phase and power limits — still apply.

Stopping predictive charging, discharging for Peak Shaving, normal PD discharge
and contracted-power emergency discharge are therefore separate actions. Peak
protection never turns a cheap charging slot into normal economic discharge
towards the regular PD grid target. When stable headroom returns, discharge is
stopped, telemetry settles again and predictive charging resumes with
hysteresis. Its SOC target and pending kWh are preserved.

See [Household demand during a predictive charging slot](../configuration/predictive-charging/index.md#household-demand-during-a-charging-slot)
for the complete sequence and an example.

## Peak shaving for excluded devices

The optional **Peak Shaving for Excluded Devices** switch extends the peak
limit to loads that are normally excluded from battery coverage. It is disabled
by default.

When enabled and battery SOC is above the conservation threshold, normal home
coverage continues unchanged. If the excluded share would leave grid import
above the configured peak limit, the battery covers only that excess.

For example, with 1,000 W of normal home demand, 4,000 W of excluded demand and
a 3,000 W peak limit, the battery supplies 2,000 W: 1,000 W for the home and
1,000 W to shave the excluded-device peak. Grid import remains at 3,000 W.

When SOC is below the conservation threshold, the existing capacity-protection
behaviour already applies the peak limit to total demand. Battery minimum SOC,
available discharge power and other safety restrictions always remain in force.

## When to use it

Useful when:
- The grid has a fixed cost per maximum contracted power and you want to limit peaks.
- You want to ensure battery reserve for the night.

![Peak shaving configuration](../assets/screenshots/features/peak-shaving-config.png){ width="650"  style="display: block; margin: 0 auto;"}
