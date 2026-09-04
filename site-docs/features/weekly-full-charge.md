# Weekly full charge

Charges batteries to **100% once a week** so the pack reaches the LFP top-balancing window and the integration can measure cell imbalance under repeatable conditions.

## Charge profiles

| Profile | Description | Switch | Default |
| --- | --- | --- | --- | 
| **100% charge voltage taper** | Slows charging near top voltage window to allow some minor cell balancing | `full_charge_voltage_taper` | On |

For Venus E models, the 100% charge voltage taper uses the same voltage profile
as a normal battery configured with `max_soc = 100`. The weekly feature raises
the target to 100%; it does not use a separate balancing algorithm.

On Venus A/D with coupled packs, the normal 3.60 V stop is also bypassed and a
reported 100% SOC from the first pack does not finish the cycle. The tapered
200 W command remains active until the shared BMS cutoff is confirmed.

For deliberate active cell balancing, use the optional [Marstek active-balance blueprint](../blueprints.md#active-cell-balancing-for-one-marstek-battery). It runs one battery at a time through the per-battery Battery Manual Mode switch and is independent of this weekly feature.

!!! warning "Cell balancing"
    Active cell balancing is **very slow**. Reducing the top-of-charge cell delta by roughly 5 mV typically takes around 24 hours of cumulative time at the top of the balance window.

## Where the energy comes from

Solar surplus fills the pack first. On the weekly day, [predictive charging](../configuration/predictive-charging/index.md) also adds the remaining gap to 100% to its energy balance, exactly as the guaranteed minimum SOC does, so it buys from the grid whatever the solar forecast will not cover — in the cheapest hours with dynamic pricing, or in the configured window on a flat tariff. If the forecast surplus already covers the gap, nothing is bought.

This needs no configuration beyond enabling predictive charging: the weekly day is the only trigger. Without predictive charging the cycle stays solar-only and may not complete on a cloudy day.

## Dashboard configuration

The weekly full charge is configured from the Omnibattery Dashboard. The only required choice is the day on which the cycle should run.

| Field | Description | Default |
|---|---|---|
| **Day of the week** | The day on which the battery charges to 100% for cell balancing. | — |
| **Wait for solar charge delay** | When enabled, solar charge delay has priority and the weekly charge waits for it to unlock. | Disabled |

![Weekly full charge configuration](../assets/screenshots/configuration/advanced-weekly-full-charge-config.png){ width="650" style="display: block; margin: 0 auto;"}

See [Cell balancing](cell-balance-monitor.md) for full details.

!!! note "Drifted SOC"
    During the weekly charge the 3.60 V pause is **not** applied — charging keeps going at the tapered 200 W until the BMS itself cuts off. If the BMS coulomb counter has drifted (cells genuinely full but reported SOC below 100%), completion is still detected: the BMS-cutoff signature (charge ≤10 W with the inverter in Standby for 5 consecutive cycles) is recognised whenever the pack is in the top taper zone (≥ 3.48 V), regardless of the reported SOC. This lets the weekly cycle finish even when the pack never reads 100%, and best-effort attempts to recalibrate the SOC — depending on BMS firmware. See [SOC recalibration on a stuck top voltage](cell-balance-monitor.md#soc-recalibration-on-a-stuck-top-voltage).

## When the cycle completes

The weekly charge is marked **Complete** only when every battery is genuinely full — not merely when a cell touches the 3.60 V top voltage. For Venus E models, a battery counts as full when either:

- its reported SOC reaches **100%**, or
- a **BMS cutoff** is confirmed: charge collapses to ≤10 W with the inverter in Standby for 5 consecutive cycles (~10 s). During the weekly charge this is recognised whenever the pack is in the top taper zone (≥ 3.48 V), so a pack with a drifted SOC still completes.

For Venus A/D with coupled packs, the BMS cutoff is required after the tapered
charge reaches the top-voltage path, even if the reported SOC has already
reached 100%.

The 60-second cell-delta measurement still runs as a diagnostic, but it no longer gates completion. If a battery's BMS cuts below 3.60 V while it is in the taper zone, the measurement starts after that confirmed cutoff so the charge is not interrupted prematurely. This also covers Venus A/D, whose final cutoff is required before measuring. On completion the configured max SOC (and the hardware cutoff register on v2) is restored, and charge hysteresis is re-enabled.

The **Weekly Full Charge** sensor exposes per-battery diagnostics under its `batteries` attribute: live SOC and BMS-cutoff cycle count while charging, and a completion snapshot (`soc_at_completion`, `max_cell_voltage_at_completion`, `completion_reason`, `bms_cutoff_cycles`).

## Cell balance monitor

The **cell balance monitor** records the voltage spread between the highest and lowest cell after each top-voltage measurement and keeps the sensor history, trend and alerts updated.

## Interaction with solar charge delay

If [solar charge delay](solar-charge-delay.md) is active, the weekly charge can be postponed while the forecast solar production is sufficient to reach 100%.

When the weekly full charge is active, the integration bypasses the delay by default so the battery reaches the top-voltage measurement point and the balance reading is not skipped.

The **Delay weekly full charge** switch (`weekly_full_charge_delay`, on the Weekly full charge card) reverses this: turn it on to let the weekly charge wait for the solar charge delay to unlock, charging from solar instead of starting immediately on the target day. It only appears when both weekly full charge and the charge delay are configured.

## Modbus register involved

This feature manipulates register **44000** (charging cutoff) to temporarily raise the limit.

!!! info
    This feature is available for all supported battery versions (v2, v3, vA, vD).

![Weekly full charge configuration](../assets/screenshots/features/weekly-full-charge-config.png){ width="650"  style="display: block; margin: 0 auto;"}
