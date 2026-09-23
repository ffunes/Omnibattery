# Shelly Pro 3EM MQTT scripts

A Shelly Pro 3EM does not provide a 1–2 second MQTT telemetry cadence natively, and Omnibattery's [main sensor](../configuration/main-sensor.md) works best with a fast-updating grid sensor. The scripts on this page run on the device itself and publish telemetry every second over MQTT, with Home Assistant MQTT Discovery so the sensors appear automatically.

## Which script do I need?

A Shelly Pro 3EM is configured with one of two metering profiles. Check yours in the Shelly web interface under **Settings → Device profile** before choosing a script — the two scripts are not interchangeable and only one should run on the device at a time.

| Your Shelly profile | Use this script |
|---|---|
| Three single-phase meters (`monophase` / three independent `EM1` channels) | [Three single-phase meters profile](#three-single-phase-meters-profile) |
| Triphase (one `EM` component across three phases) | [Three-phase profile](#three-phase-profile) |

## Before you start

- MQTT enabled and connected on the Shelly device.
- Home Assistant connected to the same MQTT broker.
- MQTT Discovery enabled in Home Assistant (the default prefix is `homeassistant`).

## Selecting the sensor in Omnibattery

Once the script is running, open Omnibattery's [main sensor](../configuration/main-sensor.md) configuration and select the discovered total-power sensor (**Total Active Power**) as the grid consumption sensor. Watch the sensor's value while importing and exporting: Omnibattery's standard convention is a **positive** value for import and **negative** for export. If your installation reports the opposite, enable **Inverted meter sign** rather than editing the script.

## Three single-phase meters profile

!!! warning "Scope"
    Use this script with a Shelly Pro 3EM configured in the three single-phase meters profile. It reads `EM1.GetStatus` for channels `0`, `1` and `2` and exposes each channel as a separate clamp.

### Install and verify

1. Open the Shelly web interface and go to **Scripts**.
2. Create a new script and paste the code below.
3. Save the script, then enable and run it.
4. In Home Assistant, confirm that **Total Active Power** and the three per-clamp sensors appear and update roughly once per second.
5. Select **Total Active Power** as described in [Selecting the sensor in Omnibattery](#selecting-the-sensor-in-omnibattery).

The script publishes state to `shellypro3em/<device-id>/state` and availability to `shellypro3em/<device-id>/availability`. Discovery messages are retained by the MQTT broker, while state is published once per second.

### Script

```javascript
// Shelly Pro 3EM in three single-phase meters profile
// MQTT telemetry + Home Assistant MQTT Discovery.
//
// Publishes every second:
// - Active power, voltage, current, and power factor per clamp
// - Total active power calculated from all three clamps
//
// Requirements:
// - MQTT enabled and connected on the Shelly device
// - Home Assistant connected to the same MQTT broker
// - MQTT Discovery enabled in Home Assistant (default prefix: homeassistant)

let DISCOVERY_PREFIX = "homeassistant";
let STATE_INTERVAL_MS = 1000;
let STATE_EXPIRE_AFTER_S = 5;

let dev = Shelly.getDeviceInfo();
let DEVICE_ID = dev.id || "shellypro3em";
let DEVICE_NAME = dev.name || "Shelly Pro 3EM";

let BASE_TOPIC = "shellypro3em/" + DEVICE_ID;
let STATE_TOPIC = BASE_TOPIC + "/state";
let AVAILABILITY_TOPIC = BASE_TOPIC + "/availability";

let lastAvailability = null;
let publishing = false;

function haConfigTopic(objectId) {
  return DISCOVERY_PREFIX + "/sensor/" + DEVICE_ID + "_" + objectId + "/config";
}

function setAvailability(online) {
  if (lastAvailability === online) return;

  lastAvailability = online;
  MQTT.publish(
    AVAILABILITY_TOPIC,
    online ? "online" : "offline",
    0,
    true
  );
}

function publishDiscoverySensor(
  objectId,
  name,
  unit,
  deviceClass,
  stateClass,
  valueTemplate
) {
  let payload = {
    name: name,
    unique_id: DEVICE_ID + "_" + objectId,
    object_id: DEVICE_ID + "_" + objectId,

    state_topic: STATE_TOPIC,
    value_template: valueTemplate,
    expire_after: STATE_EXPIRE_AFTER_S,

    availability_topic: AVAILABILITY_TOPIC,
    payload_available: "online",
    payload_not_available: "offline",

    unit_of_measurement: unit,
    device_class: deviceClass,
    state_class: stateClass,
    force_update: false,

    device: {
      identifiers: [DEVICE_ID],
      name: DEVICE_NAME,
      manufacturer: "Shelly",
      model: "Shelly Pro 3EM",
      sw_version: dev.fw_id || ""
    }
  };

  // Retained: Home Assistant can recreate the entities after a restart.
  MQTT.publish(haConfigTopic(objectId), JSON.stringify(payload), 0, true);
}

function publishDiscovery() {
  // Total active power
  publishDiscoverySensor(
    "total_active_power",
    "Total Active Power",
    "W",
    "power",
    "measurement",
    "{{ value_json.total_act_power | float(0) }}"
  );

  // Clamp 1
  publishDiscoverySensor(
    "clamp_1_active_power",
    "Clamp 1 Active Power",
    "W", "power", "measurement",
    "{{ value_json.clamp_1.act_power | float(0) }}"
  );
  publishDiscoverySensor(
    "clamp_1_voltage",
    "Clamp 1 Voltage",
    "V", "voltage", "measurement",
    "{{ value_json.clamp_1.voltage | float(0) }}"
  );
  publishDiscoverySensor(
    "clamp_1_current",
    "Clamp 1 Current",
    "A", "current", "measurement",
    "{{ value_json.clamp_1.current | float(0) }}"
  );
  publishDiscoverySensor(
    "clamp_1_power_factor",
    "Clamp 1 Power Factor",
    "", "power_factor", "measurement",
    "{{ value_json.clamp_1.pf | float(0) }}"
  );

  // Clamp 2
  publishDiscoverySensor(
    "clamp_2_active_power",
    "Clamp 2 Active Power",
    "W", "power", "measurement",
    "{{ value_json.clamp_2.act_power | float(0) }}"
  );
  publishDiscoverySensor(
    "clamp_2_voltage",
    "Clamp 2 Voltage",
    "V", "voltage", "measurement",
    "{{ value_json.clamp_2.voltage | float(0) }}"
  );
  publishDiscoverySensor(
    "clamp_2_current",
    "Clamp 2 Current",
    "A", "current", "measurement",
    "{{ value_json.clamp_2.current | float(0) }}"
  );
  publishDiscoverySensor(
    "clamp_2_power_factor",
    "Clamp 2 Power Factor",
    "", "power_factor", "measurement",
    "{{ value_json.clamp_2.pf | float(0) }}"
  );

  // Clamp 3
  publishDiscoverySensor(
    "clamp_3_active_power",
    "Clamp 3 Active Power",
    "W", "power", "measurement",
    "{{ value_json.clamp_3.act_power | float(0) }}"
  );
  publishDiscoverySensor(
    "clamp_3_voltage",
    "Clamp 3 Voltage",
    "V", "voltage", "measurement",
    "{{ value_json.clamp_3.voltage | float(0) }}"
  );
  publishDiscoverySensor(
    "clamp_3_current",
    "Clamp 3 Current",
    "A", "current", "measurement",
    "{{ value_json.clamp_3.current | float(0) }}"
  );
  publishDiscoverySensor(
    "clamp_3_power_factor",
    "Clamp 3 Power Factor",
    "", "power_factor", "measurement",
    "{{ value_json.clamp_3.pf | float(0) }}"
  );
}

function publishState() {
  // Prevent overlapping asynchronous requests.
  if (publishing) return;
  publishing = true;

  Shelly.call("EM1.GetStatus", { id: 0 }, function (c1, err1, msg1) {
    if (err1 !== 0) {
      print("EM1:0 error:", err1, msg1);
      setAvailability(false);
      publishing = false;
      return;
    }

    Shelly.call("EM1.GetStatus", { id: 1 }, function (c2, err2, msg2) {
      if (err2 !== 0) {
        print("EM1:1 error:", err2, msg2);
        setAvailability(false);
        publishing = false;
        return;
      }

      Shelly.call("EM1.GetStatus", { id: 2 }, function (c3, err3, msg3) {
        if (err3 !== 0) {
          print("EM1:2 error:", err3, msg3);
          setAvailability(false);
          publishing = false;
          return;
        }

        let p1 = c1.act_power || 0;
        let p2 = c2.act_power || 0;
        let p3 = c3.act_power || 0;

        let payload = {
          total_act_power: p1 + p2 + p3,

          clamp_1: {
            act_power: c1.act_power,
            aprt_power: c1.aprt_power,
            current: c1.current,
            voltage: c1.voltage,
            pf: c1.pf,
            freq: c1.freq
          },

          clamp_2: {
            act_power: c2.act_power,
            aprt_power: c2.aprt_power,
            current: c2.current,
            voltage: c2.voltage,
            pf: c2.pf,
            freq: c2.freq
          },

          clamp_3: {
            act_power: c3.act_power,
            aprt_power: c3.aprt_power,
            current: c3.current,
            voltage: c3.voltage,
            pf: c3.pf,
            freq: c3.freq
          }
        };

        setAvailability(true);
        MQTT.publish(STATE_TOPIC, JSON.stringify(payload), 0, false);
        publishing = false;
      });
    });
  });
}

// Publish discovery configuration once on each script start.
// Config messages are retained by the MQTT broker.
publishDiscovery();

// Publish immediately, then every second.
publishState();
Timer.set(STATE_INTERVAL_MS, true, publishState);
```

## Three-phase profile

!!! warning "Scope"
    Use this script with a Shelly Pro 3EM configured with the **triphase** profile. It reads **EM.GetStatus** with **id: 0** and publishes the three phases as **phase_a**, **phase_b** and **phase_c**. Run only one of the two profile scripts at a time.

### Install and verify

1. Open the Shelly web interface and go to **Scripts**.
2. Create a new script, or replace the script for the other metering profile.
3. Paste the code below, save it, then enable and run it.
4. In Home Assistant, confirm that **Total Active Power** and the per-phase sensors appear and update roughly once per second.
5. Select **Total Active Power** as described in [Selecting the sensor in Omnibattery](#selecting-the-sensor-in-omnibattery).

This script uses the same state and availability topics as the single-phase-profile script, so do not run both scripts simultaneously for the same device.

### Script

```javascript
// Shelly Pro 3EM — triphase profile
// MQTT telemetry + Home Assistant MQTT Discovery.
//
// Requirements:
// - Shelly profile: "triphase"
// - MQTT enabled and connected
// - MQTT Discovery enabled in Home Assistant

let DISCOVERY_PREFIX = "homeassistant";
let STATE_INTERVAL_MS = 1000;
let STATE_EXPIRE_AFTER_S = 5;

let dev = Shelly.getDeviceInfo();
let DEVICE_ID = dev.id || "shellypro3em";
let DEVICE_NAME = dev.name || "Shelly Pro 3EM";

let BASE_TOPIC = "shellypro3em/" + DEVICE_ID;
let STATE_TOPIC = BASE_TOPIC + "/state";
let AVAILABILITY_TOPIC = BASE_TOPIC + "/availability";

let lastAvailability = null;
let publishing = false;

function haConfigTopic(objectId) {
  return DISCOVERY_PREFIX + "/sensor/" + DEVICE_ID + "_" + objectId + "/config";
}

function setAvailability(online) {
  if (lastAvailability === online) return;

  lastAvailability = online;
  MQTT.publish(
    AVAILABILITY_TOPIC,
    online ? "online" : "offline",
    0,
    true
  );
}

function publishDiscoverySensor(
  objectId,
  name,
  unit,
  deviceClass,
  stateClass,
  valueTemplate
) {
  let payload = {
    name: name,
    unique_id: DEVICE_ID + "_" + objectId,
    object_id: DEVICE_ID + "_" + objectId,

    state_topic: STATE_TOPIC,
    value_template: valueTemplate,
    expire_after: STATE_EXPIRE_AFTER_S,

    availability_topic: AVAILABILITY_TOPIC,
    payload_available: "online",
    payload_not_available: "offline",

    unit_of_measurement: unit,
    device_class: deviceClass,
    state_class: stateClass,

    device: {
      identifiers: [DEVICE_ID],
      name: DEVICE_NAME,
      manufacturer: "Shelly",
      model: "Shelly Pro 3EM",
      sw_version: dev.fw_id || ""
    }
  };

  MQTT.publish(haConfigTopic(objectId), JSON.stringify(payload), 0, true);
}

function publishPhaseDiscovery(phase, label) {
  publishDiscoverySensor(
    "phase_" + phase + "_active_power",
    "Phase " + label + " Active Power",
    "W", "power", "measurement",
    "{{ value_json.phase_" + phase + ".act_power | float(0) }}"
  );

  publishDiscoverySensor(
    "phase_" + phase + "_voltage",
    "Phase " + label + " Voltage",
    "V", "voltage", "measurement",
    "{{ value_json.phase_" + phase + ".voltage | float(0) }}"
  );

  publishDiscoverySensor(
    "phase_" + phase + "_current",
    "Phase " + label + " Current",
    "A", "current", "measurement",
    "{{ value_json.phase_" + phase + ".current | float(0) }}"
  );

  publishDiscoverySensor(
    "phase_" + phase + "_power_factor",
    "Phase " + label + " Power Factor",
    "", "power_factor", "measurement",
    "{{ value_json.phase_" + phase + ".pf | float(0) }}"
  );
}

function publishDiscovery() {
  publishDiscoverySensor(
    "total_active_power",
    "Total Active Power",
    "W", "power", "measurement",
    "{{ value_json.total_act_power | float(0) }}"
  );

  publishDiscoverySensor(
    "total_current",
    "Total Current",
    "A", "current", "measurement",
    "{{ value_json.total_current | float(0) }}"
  );

  publishPhaseDiscovery("a", "A");
  publishPhaseDiscovery("b", "B");
  publishPhaseDiscovery("c", "C");
}

function phasePayload(status, phase) {
  return {
    act_power: status[phase + "_act_power"],
    aprt_power: status[phase + "_aprt_power"],
    current: status[phase + "_current"],
    voltage: status[phase + "_voltage"],
    pf: status[phase + "_pf"],
    freq: status[phase + "_freq"]
  };
}

function publishState() {
  if (publishing) return;
  publishing = true;

  Shelly.call("EM.GetStatus", { id: 0 }, function (status, err, msg) {
    publishing = false;

    if (err !== 0) {
      print("EM:0 error:", err, msg);
      setAvailability(false);
      return;
    }

    let totalPower = status.total_act_power;
    if (totalPower === null || typeof totalPower === "undefined") {
      totalPower =
        (status.a_act_power || 0) +
        (status.b_act_power || 0) +
        (status.c_act_power || 0);
    }

    let payload = {
      total_act_power: totalPower,
      total_aprt_power: status.total_aprt_power,
      total_current: status.total_current,
      neutral_current: status.n_current,

      phase_a: phasePayload(status, "a"),
      phase_b: phasePayload(status, "b"),
      phase_c: phasePayload(status, "c")
    };

    setAvailability(true);
    MQTT.publish(STATE_TOPIC, JSON.stringify(payload), 0, false);
  });
}

publishDiscovery();
publishState();
Timer.set(STATE_INTERVAL_MS, true, publishState);
```

## Uninstalling

Stop and disable the script in the Shelly web interface's **Scripts** page. The retained MQTT discovery and availability messages remain on the broker until they expire or are cleared; if you do not plan to reinstall, remove them from Home Assistant by publishing an empty, retained payload to each `homeassistant/sensor/<device-id>_*/config` topic, or delete the entities from **Settings → Devices & services → MQTT**.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| No sensors appear in Home Assistant | MQTT Discovery is disabled, or the broker is unreachable from the Shelly | The MQTT integration's discovery prefix matches `homeassistant`; the Shelly's MQTT connection status in its web interface |
| Sensors appear but never update | The script is not running, or the wrong profile's script is installed | The script is enabled and running (Shelly **Scripts** page, script log); the device profile matches the script |
| Sensors go `unavailable` after ~5 seconds | The script stopped publishing (device reboot, script error, MQTT disconnect) | The `expire_after: 5` availability window; the script's log for `EM1:*` or `EM:0` errors |
| Grid power reads the wrong sign | Import and export are reversed for this meter's wiring | The **Inverted meter sign** toggle on the [main sensor](../configuration/main-sensor.md) — do not edit the script's sign convention |
