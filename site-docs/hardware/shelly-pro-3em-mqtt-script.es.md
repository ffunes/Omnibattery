# Scripts MQTT para Shelly Pro 3EM

Un Shelly Pro 3EM no proporciona de forma nativa una cadencia de telemetría MQTT de 1–2 segundos, y el [sensor principal](../configuration/main-sensor.md) de Omnibattery funciona mejor con un sensor de red que se actualice rápidamente. Los scripts de esta página se ejecutan en el propio dispositivo y publican telemetría cada segundo mediante MQTT, con MQTT Discovery de Home Assistant para que los sensores aparezcan automáticamente.

## ¿Qué script necesito?

Un Shelly Pro 3EM se configura con uno de dos perfiles de medición. Comprueba el tuyo en la interfaz web de Shelly, en **Ajustes → Perfil del dispositivo**, antes de elegir un script: los dos scripts no son intercambiables y solo debe ejecutarse uno en el dispositivo a la vez.

| Perfil de tu Shelly | Usa este script |
|---|---|
| Tres contadores monofásicos (`monophase` / tres canales `EM1` independientes) | [Perfil de tres contadores monofásicos](#perfil-de-tres-contadores-monofasicos) |
| Trifásico (un componente `EM` en las tres fases) | [Perfil trifásico](#perfil-trifasico) |

## Antes de empezar

- MQTT activado y conectado en el dispositivo Shelly.
- Home Assistant conectado al mismo broker MQTT.
- MQTT Discovery activado en Home Assistant (el prefijo predeterminado es `homeassistant`).

## Seleccionar el sensor en Omnibattery

Cuando el script esté en ejecución, abre la configuración del [sensor principal](../configuration/main-sensor.md) de Omnibattery y selecciona el sensor de potencia total detectado (**Total Active Power**) como sensor de consumo de red. Observa el valor del sensor mientras importas y exportas: la convención estándar de Omnibattery es un valor **positivo** para la importación y **negativo** para la exportación. Si tu instalación informa de lo contrario, activa **Inverted meter sign** en lugar de editar el script.

## Perfil de tres contadores monofásicos

!!! warning "Ámbito"
    Usa este script con un Shelly Pro 3EM configurado con el perfil de tres contadores monofásicos. Lee `EM1.GetStatus` para los canales `0`, `1` y `2`, y expone cada canal como una pinza independiente.

### Instalar y verificar

1. Abre la interfaz web de Shelly y ve a **Scripts**.
2. Crea un script nuevo y pega el código siguiente.
3. Guarda el script y, después, actívalo y ejecútalo.
4. En Home Assistant, confirma que aparecen **Total Active Power** y los tres sensores por pinza, y que se actualizan aproximadamente una vez por segundo.
5. Selecciona **Total Active Power** como se describe en [Seleccionar el sensor en Omnibattery](#seleccionar-el-sensor-en-omnibattery).

El script publica el estado en `shellypro3em/<device-id>/state` y la disponibilidad en `shellypro3em/<device-id>/availability`. El broker MQTT retiene los mensajes de descubrimiento, mientras que el estado se publica una vez por segundo.

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

## Perfil trifásico

!!! warning "Ámbito"
    Usa este script con un Shelly Pro 3EM configurado con el perfil **triphase**. Lee **EM.GetStatus** con **id: 0** y publica las tres fases como **phase_a**, **phase_b** y **phase_c**. Ejecuta solo uno de los dos scripts de perfil a la vez.

### Instalar y verificar

1. Abre la interfaz web de Shelly y ve a **Scripts**.
2. Crea un script nuevo o sustituye el script del otro perfil de medición.
3. Pega el código siguiente, guárdalo y, después, actívalo y ejecútalo.
4. En Home Assistant, confirma que aparecen **Total Active Power** y los sensores por fase, y que se actualizan aproximadamente una vez por segundo.
5. Selecciona **Total Active Power** como se describe en [Seleccionar el sensor en Omnibattery](#seleccionar-el-sensor-en-omnibattery).

Este script usa los mismos tópicos de estado y disponibilidad que el script del perfil monofásico, así que no ejecutes ambos scripts simultáneamente para el mismo dispositivo.

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

## Desinstalación

Detén y desactiva el script en la página **Scripts** de la interfaz web de Shelly. Los mensajes retenidos de descubrimiento y disponibilidad MQTT permanecen en el broker hasta que caducan o se borran; si no piensas reinstalarlo, elimínalos de Home Assistant publicando una carga útil vacía y retenida en cada tema `homeassistant/sensor/<device-id>_*/config`, o elimina las entidades desde **Ajustes → Dispositivos y servicios → MQTT**.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| No aparecen sensores en Home Assistant | MQTT Discovery está desactivado o el broker no es accesible desde el Shelly | El prefijo de descubrimiento de la integración MQTT coincide con `homeassistant`; el estado de conexión MQTT del Shelly en su interfaz web |
| Los sensores aparecen pero nunca se actualizan | El script no se está ejecutando o está instalado el script del perfil equivocado | El script está activado y en ejecución (página **Scripts** de Shelly, registro del script); el perfil del dispositivo coincide con el script |
| Los sensores pasan a `unavailable` después de ~5 segundos | El script dejó de publicar (reinicio del dispositivo, error del script, desconexión de MQTT) | La ventana de disponibilidad `expire_after: 5`; el registro del script para errores de `EM1:*` o `EM:0` |
| La potencia de red tiene el signo equivocado | La importación y la exportación están invertidas debido al cableado de este contador | El interruptor **Inverted meter sign** del [sensor principal](../configuration/main-sensor.md): no edites la convención de signo del script |
