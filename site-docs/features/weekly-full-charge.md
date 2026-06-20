# Weekly full charge

Charges batteries to **100% once a week** so the pack reaches the LFP top-balancing window and the integration can measure cell imbalance under repeatable conditions.

## Charge profiles

| Profile | Description | Switch | Default |
| --- | --- | --- | --- | 
| **100% charge voltage taper** | Slows charging near top voltage window to allow some minor cell balancing | `full_charge_voltage_taper` | On |
| **Active cell balancing** | Full cell balancing - repeated slow charge/discharge near top voltage window until `cell_delta_V` drops below 0.03 V or switched off | `active_balance_mode` | Off |

The 100% charge voltage taper uses the same voltage profile as a normal battery configured with `max_soc = 100`. The weekly feature only raises the target to 100%; it does not use a separate balancing algorithm.

Active cell balancing repeatedly cycles slow charge or discharge near the top voltage window until the measured top-voltage delta is at or below 0.03 V, or until the user turns the switch off.

!!! warning "Cell balancing"
    Active cell balancing is **very slow**. Reducing the top-of-charge cell delta by roughly 5 mV typically takes around 24 hours of cumulative time at the top of the balance window.

See [Cell balancing](cell-balance-monitor.md) for full details.

!!! note "Drifted SOC"
    During the weekly charge the 3.58 V pause is **not** applied — charging keeps going at the tapered 95 W until the BMS itself cuts off. If the BMS coulomb counter has drifted (cells genuinely full but reported SOC below 100%), completion is still detected: the BMS-cutoff signature (charge ≤10 W with the inverter in Standby for 5 consecutive cycles) is recognised whenever the pack is in the top taper zone (≥ 3.48 V), regardless of the reported SOC. This lets the weekly cycle finish even when the pack never reads 100%, and best-effort attempts to recalibrate the SOC — depending on BMS firmware. See [SOC recalibration on a stuck top voltage](cell-balance-monitor.md#soc-recalibration-on-a-stuck-top-voltage).

## When the cycle completes

The weekly charge is marked **Complete** only when every battery is genuinely full — not merely when a cell touches the 3.58 V top voltage. A battery counts as full when either:

- its reported SOC reaches **100%**, or
- a **BMS cutoff** is confirmed: charge collapses to ≤10 W with the inverter in Standby for 5 consecutive cycles (~10 s). During the weekly charge this is recognised whenever the pack is in the top taper zone (≥ 3.48 V), so a pack with a drifted SOC still completes.

The 60-second cell-delta measurement still runs as a diagnostic, but it no longer gates completion. On completion the configured max SOC (and the hardware cutoff register on v2) is restored, and charge hysteresis is re-enabled.

The **Weekly Full Charge** sensor exposes per-battery diagnostics under its `batteries` attribute: live SOC and BMS-cutoff cycle count while charging, and a completion snapshot (`soc_at_completion`, `max_cell_voltage_at_completion`, `completion_reason`, `bms_cutoff_cycles`).

## Cell balance monitor

The **cell balance monitor** is only active when checked in the Weekly Full Charge Configuration. It records the voltage spread between the highest and lowest cell after each top-voltage measurement and keeps the sensor history, trend and alerts updated.

See [Advanced options](/configuration/advanced.md) for full details.

## Interaction with solar charge delay

If [solar charge delay](solar-charge-delay.md) is active, the weekly charge can be postponed while the forecast solar production is sufficient to reach 100%.

When the weekly full charge is active, the integration can bypass the delay so the battery reaches the top-voltage measurement point and the balance reading is not skipped.

## Modbus register involved

This feature manipulates register **44000** (charging cutoff) to temporarily raise the limit.

!!! info
    This feature is available for all supported battery versions (v2, v3, vA, vD).

![Weekly full charge configuration](../assets/screenshots/features/weekly-full-charge-config.png){ width="650"  style="display: block; margin: 0 auto;"}
