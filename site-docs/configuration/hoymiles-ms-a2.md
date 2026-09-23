# Hoymiles MS-A2

This guide takes an installed MS-A2 from S-Miles Home commissioning to local control through Home Assistant and Omnibattery. The battery communicates through the MQTT broker already configured in Home Assistant.

## Do I need it?

**Use this guide if** you have a Hoymiles MS-A2 whose firmware shows **MQTT Service**. For HiBattery models, use the shared [Hoymiles MQTT battery guide](batteries/hoymiles.md) and the product's electrical manual.

One MS-A2 has a nominal capacity of `2.24 kWh` and a `1,000 W` charge/discharge ceiling. A supported two-unit system can provide `4.48 kWh` and up to `2,000 W`, subject to the MQTT envelope published by the device.

## Before you start

You need:

- an MS-A2 commissioned in **S-Miles Home**;
- firmware that exposes **MQTT Service**;
- Home Assistant with a connected **MQTT** integration and local broker;
- Omnibattery installed with a grid power sensor;
- the complete MS-A2 MQTT device ID.

!!! warning "Electrical installation"
    Follow the current [official MS-A2 installation guide](https://www.hoymiles.com/statics/5/hoymiles/picture/User-Manual_MS_A2_Global_EN_REV1.4.pdf), the product labels, and local electrical rules. Isolate the battery and solar equipment before changing AC connections.

## How to add it

1. **Commission the battery.** Inspect the unit, cable, and connectors; install it upright with the clearances and weather protection required by Hoymiles. With the equipment isolated, connect the microinverter system and AC cable as shown in the official guide, connect the approved Schuko outlet, then turn on the MS-A2. Join the supported Wi-Fi network in **S-Miles Home**, confirm that state of charge and power update, and install offered firmware updates.
2. **Prepare Home Assistant MQTT.** Open **Settings → Devices & services**, confirm **MQTT** is connected, and create a dedicated broker user for the battery when your broker supports users.
3. **Connect the battery to the broker.** In **S-Miles Home → MQTT Service**, enter the broker's local address, port, username, and password. Save the settings and record the complete device ID, including its `MSA-` prefix.
4. **Add the battery.** Open **Settings → Devices & services → Add integration → Omnibattery**, choose **Hoymiles MQTT**, enter the device ID, and leave **Battery model** on **Auto-detect**.
5. **Review and verify.** Keep the detected capacity and safe power envelope, complete the common limits, and confirm that state of charge, battery power, voltage, temperature, and daily energy update.

## What you will see

Charging is positive battery power in Omnibattery, discharging is negative, and idle is zero. Automatic control and the software manual controls use the same convention; turn on **Battery Manual Control** before testing a manual target.

After setup, verify a low-power charge, discharge, and idle command. State of charge and battery power should follow S-Miles Home, and MQTT telemetry should continue updating through the local broker.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The wizard reports **Cannot connect** | Home Assistant MQTT is disconnected, MQTT Service is off, or the ID is incomplete | Confirm the integration, service, and full device ID |
| The app works but Omnibattery receives no data | The broker details in S-Miles Home are wrong or unreachable from the battery | Check broker address, port, credentials, and Wi-Fi routing |
| The battery returns to autonomous control | MQTT disconnected long enough for external control to expire | Check broker restarts and connection stability |
| A paired system remains limited to 1,000 W | Omnibattery has not read the paired retained envelope | Pair both units, then reconfigure the battery |
| Detailed entities update more slowly than power | Hoymiles publishes those topics less often | Confirm that their timestamps still advance |
| Control is rejected | Firmware is old or the ID belongs to the wrong unit | Update firmware and use the master or standalone device ID |

??? "Advanced details"
    The MS-A2 publishes fast state under `homeassistant/sensor/<device_id>/quick/state`; for example, `homeassistant/sensor/MSA-280024341346/quick/state`. Omnibattery subscribes through Home Assistant, so you do not need to create MQTT sensors or automations. Voltage, temperature, and daily-energy topics normally update less often than the fast state topic.

    Keep the broker on the trusted local network and create a dedicated battery user where possible.

    The driver selects `mqtt_ctrl` and refreshes the exact target every `30 s`. A failed refresh retries after `5 s`. External control expires if command refreshes stop, so broker stability matters. When Omnibattery unloads, it sends `0 W` and restores `general` mode.

    Firmware `01.06.03` can advertise an asymmetric `−1,000…+2,000 W` envelope for one unit even though its hardware limit is symmetric at `1,000 W`. Omnibattery uses the charge-side magnitude to keep a single unit symmetric. A paired device publishing `−2,000…+2,000 W` retains that range.

    The MQTT protocol does not expose writable SOC cutoffs or individual cell voltages. Omnibattery enforces state-of-charge limits in software; cell-balance and voltage-taper features are unavailable. The setup defaults are `100%` maximum SOC, `10%` minimum SOC, and `2%` charge hysteresis.

    Begin verification at low power and stop if the measured direction does not match the requested charge or discharge direction.

    Keep the broker on the local network or behind a secured VPN. Do not expose an unencrypted MQTT listener directly to the Internet.

    Official references:

    - [Hoymiles MS-A2 product page](https://www.hoymiles.com/products/micro-storage.html)
    - [MS-A2 installation and user guide](https://www.hoymiles.com/statics/5/hoymiles/picture/User-Manual_MS_A2_Global_EN_REV1.4.pdf)
    - [Hoymiles MQTT protocol guide](https://www.hoymiles.com/uploadfile/1/202511/9350aa1077.txt)
