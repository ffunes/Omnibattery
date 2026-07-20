# Instalación

## Requisitos

### Hardware

| Componente | Descripción |
|---|---|
| Batería | Marstek Venus E v2/v3, Venus A, Venus D **o** Zendure SolarFlow 2400 AC+ / AC Pro **o** Anker SOLIX Solarbank Max AC |
| Conversor Modbus | Dispositivo RS485 → Modbus TCP (p. ej. Elfin-EW11) — **solo necesario para Marstek Venus E v2**. Las Venus E v3, Venus A y Venus D se conectan por Ethernet y soportan Modbus TCP de forma nativa. Anker Solarbank Max AC usa Modbus TCP nativo (activar en la app Anker bajo Third-Party Control; solo un cliente Modbus a la vez). No necesario para Zendure (HTTP local). |
| Adaptador serie *(opcional)* | Adaptador USB–RS485 para conexión serie directa (Modbus RTU) a baterías Marstek. |
| Sensor de red | Sensor HA que mide el consumo total de la red (p. ej. Shelly EM3, Neurio, contador inteligente) |

### Software

- Home Assistant **2024.1.0** o superior
- (Opcional) Sensor de previsión solar para la carga predictiva (Solcast, Forecast.Solar, etc.)

### Red

La batería debe ser accesible desde Home Assistant por IP en el mismo segmento de red o mediante enrutamiento.

---

## Instalación con HACS (recomendado)

1. Haz clic en el botón para añadir el repositorio a HACS:

    [![Añadir a HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ffunes&repository=Omnibattery&category=integration)

2. Busca **"Omnibattery"** e instala.
3. Reinicia Home Assistant.

![Búsqueda en HACS](assets/screenshots/installation/hacs-search.png){ width="700"  style="display: block; margin: 0 auto;"}

---

## Instalación manual

1. Descarga el zip de la última release desde [GitHub Releases](https://github.com/ffunes/Omnibattery/releases).
2. Extrae la carpeta `omnibattery`.
3. Cópiala en el directorio `custom_components/` de Home Assistant.
4. Reinicia Home Assistant.

---

## Instalación de blueprints

Los blueprints son opcionales y se instalan en la carpeta de configuración de Home Assistant, no dentro de `custom_components/`.

La carpeta de blueprints de tu Home Assistant es:

```text
/config/blueprints/automation/omnibattery/
```

Si accedes a Home Assistant mediante Samba, Studio Code Server o File Editor, la misma ruta suele verse como:

```text
config/blueprints/automation/omnibattery/
```

### Instalación desde la interfaz de Home Assistant

1. Ve a **Ajustes** → **Automatizaciones y escenas** → **Blueprints**.
2. Pulsa **Importar blueprint**.
3. Pega la URL del blueprint que quieras importar, por ejemplo:

    ```text
    https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/different_grid_target_blueprint.yaml
    ```

4. Pulsa **Previsualizar blueprint** y después **Importar blueprint**.
5. Crea una automatización nueva desde el blueprint importado y selecciona tus entidades.

### Instalación manual

1. Crea la carpeta `/config/blueprints/automation/omnibattery/` si no existe.
2. Copia dentro los archivos `.yaml` de la carpeta `blueprints/` de este repositorio.
3. En Home Assistant, ve a **Ajustes** → **Automatizaciones y escenas** → **Blueprints** y pulsa **Recargar blueprints**. Si no aparece la opción, reinicia Home Assistant.
4. Crea una automatización nueva desde el blueprint instalado.

---

## Añadir la integración

Después de instalar y reiniciar:

1. Ve a **Ajustes** → **Dispositivos y servicios**.
2. Pulsa **+ AÑADIR INTEGRACIÓN**.
3. Busca **Omnibattery**.
4. Sigue el [asistente de configuración](configuration/index.md).

![Añadir integración en HA](assets/screenshots/installation/add-integration.png){ width="600"  style="display: block; margin: 0 auto;"}
