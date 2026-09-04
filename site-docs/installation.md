# Installation

## Requirements

### Hardware

The table shows the connection path used by Omnibattery for each supported
battery. Adapters and bridges are only required where noted.

| Battery / component | Supported connection | Additional requirement |
|---|---|---|
| **Marstek Venus E/C (v2/v3), Venus A, Venus D** | Modbus TCP; Modbus RTU over USB–RS485; or LilyGo RS485/ESPHome bridge *(Venus E v2)* | **Modbus TCP:** Venus E v2 needs an RS485 → TCP converter (e.g. Elfin-EW11); Venus E v3, Venus A and Venus D use native Ethernet. **Modbus RTU:** USB–RS485 adapter. **ESPHome:** the LilyGo bridge must expose its required entities in Home Assistant. |
| **Zendure SolarFlow 4000 Mix Pro, 4000 Mix AC+, 2400 AC+, 2400 AC Pro, 1600 AC+, 800 Pro, 800 Plus, 800** | Local HTTP API | Keep **HEMS disabled** in the Zendure app. HEMS overrides Omnibattery's manual power setpoint when enabled. |
| **Anker SOLIX Solarbank Max AC, 4 E5000 Pro** | Modbus TCP | Enable **Third-Party Control** in the Anker app. Only one Modbus client can connect at a time. |
| **Huawei SUN2000 + LUNA2000** | Modbus TCP through the SUN2000 inverter | The default control path requires the Huawei Solar integration and a shared Modbus proxy. Direct writes are optional. See the [Huawei setup guide](configuration/batteries/huawei.md). |
| **Sessy Home Battery** | Local HTTP API through the Sessy dongle | The dongle must be reachable from Home Assistant. Enter its IP/hostname, port and dongle credentials; port `80` is the default. |
| **Hoymiles MS-A2 / HiBattery** | MQTT through Home Assistant's configured MQTT integration | A working local MQTT broker is required (for example, Mosquitto; an existing broker can be reused). Enable **MQTT Service** in S-Miles Home and make the broker reachable from the battery. |
| **Grid sensor** | Home Assistant entity | Sensor measuring total grid consumption (e.g. Shelly EM3, Neurio, smart-meter integration). |
| **Solar production meter** *(optional)* | Home Assistant entity | Real-time PV production power sensor in W or kW. It lets Omnibattery derive Home Consumption accurately and populate the Solar node in the integration dashboard. Leave it empty when the panels feed the battery's MPPT inputs directly. |

!!! warning "Grid-meter update rate"
    The grid meter/sensor must publish a new value in **less than 10 seconds**.
    An update interval of **1–2 seconds** is recommended because the controller
    is event-driven and uses each publication to adjust battery power.

### Software

- Home Assistant **2024.1.0** or later
- For **Hoymiles MQTT batteries** only: the Home Assistant MQTT integration and a working local MQTT broker. Omnibattery uses the broker through Home Assistant; it does not install one.
- For the default **Huawei** control method: the Huawei Solar integration. It is not required when direct Modbus writes are enabled.
- (Optional) Solar forecast sensor for predictive charging (Solcast, Forecast.Solar, etc.)

### Network

- For Modbus TCP and local HTTP, the battery or bridge must be reachable from Home Assistant by IP on the same network segment or via routing. 
- For Modbus RTU, connect the USB adapter to the Home Assistant host. 
- For the LilyGo/ESPHome path, add the bridge to Home Assistant.
- For MQTT based batteries, the battery must be able to reach the MQTT broker over the local network.

---

## Installation via HACS (recommended)

1. Click the button to add the repository to HACS:

    [![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ffunes&repository=Omnibattery&category=integration)

2. Search for **"Omnibattery"** and install.
3. Restart Home Assistant.

![HACS search](assets/screenshots/installation/hacs-search.png){ width="700"  style="display: block; margin: 0 auto;"}

---

## Manual installation

1. Download the zip from the latest release at [GitHub Releases](https://github.com/ffunes/Omnibattery/releases).
2. Extract the `omnibattery` folder.
3. Copy it to the `custom_components/` directory of your Home Assistant instance.
4. Restart Home Assistant.

---

## Adding the integration

After installing and restarting:

1. Go to **Settings** → **Devices & Services**.
2. Click **+ ADD INTEGRATION**.
3. Search for **Omnibattery**.
4. Follow the [configuration wizard](configuration/index.md).

![Add integration in HA](assets/screenshots/installation/add-integration.png){ width="600"  style="display: block; margin: 0 auto;"}

---

## Blueprint installation

Blueprints are optional and are installed in the Home Assistant configuration folder, not inside `custom_components/`.

The blueprint folder for your Home Assistant instance is:

```text
/config/blueprints/automation/omnibattery/
```

If you access Home Assistant through Samba, Studio Code Server or File Editor, the same path is usually shown as:

```text
config/blueprints/automation/omnibattery/
```

### Install from the Home Assistant UI

1. Go to **Settings** → **Automations & Scenes** → **Blueprints**.
2. Click **Import Blueprint**.
3. Paste the URL of the blueprint you want to import, for example:

    ```text
    https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/different_grid_target_blueprint.yaml
    ```

4. Click **Preview Blueprint** and then **Import Blueprint**.
5. Create a new automation from the imported blueprint and configure its inputs. For the Marstek active-balance blueprint, select the Omnibattery battery device; its standard entities are discovered automatically.

### Manual installation

1. Create the `/config/blueprints/automation/omnibattery/` folder if it does not already exist.
2. Copy the `.yaml` files from this repository's `blueprints/` folder into it.
3. In Home Assistant, go to **Settings** → **Automations & Scenes** → **Blueprints** and click **Reload Blueprints**. If the option is not available, restart Home Assistant.
4. Create a new automation from the installed blueprint.
