# Blueprints

Blueprints are optional Home Assistant automations that complement Omnibattery. They are not part of the integration configuration and do not change its code.

## Before you start

- Import a blueprint from **Settings → Automations & scenes → Blueprints** using its link below, then create an automation from it.
- For manual installation, copy the YAML file to `/config/blueprints/automation/omnibattery/` and reload blueprints. See [Installation](../installation.md#blueprint-installation) for the general steps.

## Active cell balancing for one Marstek battery

[Import blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/marstek_active_balance_blueprint.yaml)

- **Purpose:** runs an active cell-balancing profile that charges and discharges one battery in small steps to reduce the spread between its cells.
- **Requires:** one persistent `input_boolean` per battery, used as the run/cancel request; the blueprint discovers the rest of the telemetry and control entities from the selected Omnibattery device automatically.
- **Controls:** the per-battery **Battery Manual Mode** switch, force mode, and setpoint entities on the selected device only, for as long as the run is active.
- **Compatible with:** exactly one Marstek battery per automation instance. It uses Home Assistant entities only — it does not access Modbus directly.
- **Does not do:** run more than one battery per automation, or keep control after it exits — every exit attempts to write 0 W in both directions, restore the normal SOC maximum, and release Battery Manual Mode.

Create one automation per battery. **Battery Manual Mode** is the ownership boundary: while a run is active, Omnibattery's automatic controller and other manual automations cannot write competing setpoints to that battery. Turn the request `input_boolean` ON to start or resume after a restart, and OFF to cancel.

??? "Advanced details"
    The blueprint validates telemetry, force-mode options, voltage ordering, and number limits before taking control, including the `charging_cutoff_capacity` number used as the maximum-SOC limit. Optional advanced entity-ID overrides remain available for installations where an entity was renamed. The `force_mode` options are named **None**, **Charge**, and **Discharge**; old lowercase ESPHome entities remain supported during migration.

    Its defaults are 3.49 V → 3.60 V, 95 W top charge, 200 W discharge, 60 s rest, a 30 mV target, and a 3.40 V adaptive retry floor. If the BMS rejects charge before 3.60 V but still inside the upper window, the blueprint takes the same 60-second settled measurement before continuing with adaptive discharge; rejections below that window are not added to the formal history.

    If any safety confirmation fails, the request `input_boolean` is deliberately left ON so the battery can be inspected before another automation is allowed to control it.

    Its notification baseline comes from the integration's persisted `Cell Delta` value, which represents the last formal 100%/OCV reading rather than instantaneous cell telemetry. After each settled 60-second measurement the blueprint fires the public `omnibattery_balance_measurement_ready` event with the selected device and a measurement ID. Omnibattery resolves the device, reads the cell voltages from its own coordinator, and records the result in the existing `Cell Delta` history with `source: blueprint`. The event is read-only and does not grant the integration control of the battery.

## Central status webhook reporter

[Import blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/central_status_webhook_reporter_blueprint.yaml)

- **Purpose:** reports selected Omnibattery and Home Assistant sensors from each installation to a central HTTP endpoint, for a single dashboard covering several homes or batteries.
- **Requires:** a `rest_command` defined once in `configuration.yaml` (below) and its URL stored in `secrets.yaml`.
- **Controls:** nothing in Omnibattery; it only reads sensor states and sends them out.
- **Compatible with:** any installation; you choose which sensors to report (for example SOC, battery power, grid power, and integration state).
- **Does not do:** create the outgoing HTTP command itself — Home Assistant must define it, since a blueprint cannot.

Choose a unique site ID, the sensors to report, and a reporting interval. A report is also sent when Home Assistant starts. Each report contains the site ID, timestamp, and every selected entity's state, name, unit, and device class.

```yaml
rest_command:
  omnibattery_status_report:
    url: !secret omnibattery_status_webhook_url
    method: POST
    content_type: application/json
    payload: "{{ report }}"
```

Restart Home Assistant after adding the `rest_command`, and retain the default REST command service in the blueprint unless you chose a different name. Use HTTPS and treat the endpoint URL as a secret.

## Different grid target for charge and discharge

[Import blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/different_grid_target_blueprint.yaml)

- **Purpose:** sets **PD Target Grid Power** according to the active battery direction, to avoid oscillation around a zero-grid target or implement a deliberate import/export bias.
- **Requires:** the system charge and discharge power sensors, plus the **PD Target Grid Power** number.
- **Controls:** the **PD Target Grid Power** number only.
- **Compatible with:** any installation using PD (proportional–derivative) grid-following control.
- **Does not do:** change the idle target — an idle target is optional; if not set, the existing target is left unchanged while the system is idle.

By default it sets `-50 W` while charging (a small grid export) and `+50 W` while discharging (a small grid import). The active-power threshold ignores noise near zero. The automation runs when power crosses the threshold and when Home Assistant starts.

## Peak shaving limit sync

[Import blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/peak_shaving_limit_sync_blueprint.yaml)

- **Purpose:** synchronizes Omnibattery's **Capacity Protection Limit** with a monthly-peak sensor, for tariffs or demand-management setups where the allowed peak follows a measured monthly value.
- **Requires:** the monthly-peak sensor and the **Capacity Protection Limit** number.
- **Controls:** the **Capacity Protection Limit** number only.
- **Compatible with:** a monthly-peak sensor reporting in either `kW` or `W`; the blueprint converts `kW` to watts automatically.
- **Does not do:** write the number when its value already matches the measured peak.

Checks on changes and every 15 seconds.

## Peak shaving recharge to SOC

[Import blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/peak_shaving_recharge_blueprint.yaml)

- **Purpose:** optionally replenishes the battery from the grid while **Capacity Protection** (peak shaving) is active, so a low SOC does not leave you unprotected against the next peak.
- **Requires:** the system SOC and integration-status sensors, plus the **PD Target Grid Power** number.
- **Controls:** the **PD Target Grid Power** number, moving it to a positive import value to charge when system SOC falls below the configured floor.
- **Compatible with:** any installation using peak shaving (capacity protection) and PD grid-following control.
- **Does not do:** overwrite a later manual change or a different automation's target — it only restores the idle target when the recharge target it set is still in place.

Configure the SOC floor, a higher SOC recovery target, charge power, and idle target. It restores the idle target when SOC reaches the recovery target or capacity protection ends.

## Forward persistent notifications to Telegram

[Import blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/persistent_notification_to_telegram_blueprint.yaml)

- **Purpose:** forwards new or updated Home Assistant persistent notifications to a selected Telegram notify entity.
- **Requires:** a configured `telegram_bot` notify entity.
- **Controls:** nothing in Omnibattery; it only reads notifications and sends Telegram messages.
- **Compatible with:** any persistent notification, not only Omnibattery's; an optional ID-prefix filter narrows this. The default `marstek_venus_` preserves compatibility with notifications created by the earlier integration — clear the filter to forward every persistent notification, or replace it with another prefix.
- **Does not do:** resend existing notifications when Home Assistant restarts.

It sends the notification title, ID, and message with HTML-safe escaping.

## Dismiss predictive charging notifications

[Import blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/dismiss_predictive_charging_notifications_blueprint.yaml)

- **Purpose:** automatically dismisses persistent notifications about predictive grid charging, including evaluations, price-slot starts, and evening re-evaluations.
- **Requires:** no additional entities or helpers.
- **Controls:** nothing in Omnibattery; it only dismisses matching persistent notifications.
- **Compatible with:** any installation using predictive charging.
- **Does not do:** touch battery alarms, cell-balance messages, or manual-mode notifications — only predictive-charging notifications are dismissed.

The notification may be visible briefly before Home Assistant executes the automation. Disable the automation at any time to receive predictive-charging notifications again.

## Solar forecast reserve discharge

[Import blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/solar_forecast_reserve_discharge_blueprint.yaml)

- **Purpose:** maintains a night SOC reserve by blocking discharge below it unless the remaining solar forecast is sufficient to recharge from the configured minimum SOC back to the reserve during a specified daytime window.
- **Requires:** the Allow Discharge switches to control, the system SOC and total-energy sensors, a *remaining* solar-forecast sensor in kWh, and the minimum-SOC numbers for the controlled batteries.
- **Controls:** the selected **Allow Discharge** switches only.
- **Compatible with:** any installation with a remaining-solar-production forecast sensor in kWh.
- **Does not do:** write Modbus registers or force battery modes — it only toggles Allow Discharge.

Configure the reserve, release hysteresis, forecast margin, and daytime window.

??? "Advanced details"
    The calculation includes a fixed 78% charge-efficiency assumption and the safety margin.
