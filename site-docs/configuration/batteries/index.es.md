# Elige la conexión de tu batería

Elige la vía que coincida con tu batería y con cómo está conectada a Home Assistant. Omnibattery puede coordinar hasta diez baterías compatibles, incluidas instalaciones con varias marcas.

## Elige una marca de batería

| Lo que tienes | Lo que necesitas | Selecciona en el asistente | Guía de configuración |
|---|---|---|---|
| Marstek Venus conectada directamente o mediante una pasarela Modbus | Una conexión Ethernet, RS-485 o USB accesible | **Marstek Venus** | [Marstek](marstek.md) |
| Marstek Venus conectada mediante un puente LilyGo | El firmware ESPHome compatible y su dispositivo en Home Assistant | **Marstek via LilyGo RS485 (ESPHome)** | [ESPHome / LilyGo](esphome-lilygo.md) |
| Zendure SolarFlow 800, 800 Plus, 800 Pro, 1600 AC+, 2400 AC Pro/+, 3000 Mix AC+, 4000 Mix AC+ o 4000 Mix Pro | La dirección IP local del dispositivo y HEMS desactivado | **Zendure SolarFlow** | [Zendure](zendure.md) |
| Anker SOLIX Solarbank Max AC, 4 E5000 Pro o XE AC | Modbus TCP y Third-Party Control activados | **Anker SOLIX Solarbank Max AC / 4 E5000 Pro** (también detecta XE AC) | [Anker SOLIX](anker.md) |
| Sessy Home Battery | Un dongle Sessy accesible y sus credenciales | **Sessy** | [Sessy](sessy.md) |
| Hoymiles MS-A2 o HiBattery compatible | MQTT configurado en Home Assistant y el ID completo del dispositivo | **Hoymiles MQTT** | [Hoymiles MQTT](hoymiles.md) |
| Huawei SUN2000 con LUNA2000 | Un extremo Modbus TCP y Huawei Solar o escrituras Modbus directas | **Huawei SUN2000 + LUNA2000** | [Huawei](huawei.md) |

![Selector de marca de batería](../../assets/screenshots/configuration/battery-brand-form.png){ width="650" style="display: block; margin: 0 auto;" }

La opción LilyGo es una vía de conexión independiente para una batería Marstek. Elígela solo cuando ESPHome ya exponga el puente como dispositivo en Home Assistant.

## Añade cada batería

1. Abre **Ajustes → Dispositivos y servicios → Añadir integración** y selecciona **Omnibattery**. En una instalación existente, abre Omnibattery y elige **Configurar**.
2. Elige el número de unidades de batería de la instalación.
3. Selecciona la marca o vía de conexión de la primera batería.
4. Sigue su guía de configuración y repite los pasos de marca y conexión para cada unidad restante.
5. Completa los límites comunes de potencia y estado de carga.

![Deslizador de número de baterías](../../assets/screenshots/configuration/battery-slider.png){ width="650" style="display: block; margin: 0 auto;" }

![Formulario de configuración de batería](../../assets/screenshots/configuration/battery-config-form.png){ width="650" style="display: block; margin: 0 auto;" }

## Ajustes comunes

| Ajuste | Qué cambia |
|---|---|
| **Name** | Identifica la batería en Home Assistant y en el panel de Omnibattery. |
| **Max charge power** / **Max discharge power** | Limita lo que Omnibattery puede solicitar. Un límite de hardware comunicado por el dispositivo puede reducir el valor efectivo. |
| **Max SOC** / **Min SOC** | Establece el límite superior de carga y el límite inferior de descarga. SOC significa estado de carga. |
| **Charge hysteresis** | Evita ciclos rápidos tras llegar la batería a su límite superior. El mínimo es del 2%. |
| **Backup Offgrid Threshold** | Mantiene una batería fuera del control automático mientras su salida de respaldo alimenta una carga por encima de este valor. |
| **Nominal capacity** | Activa los cálculos de energía almacenada y eficiencia cuando la batería no comunica su capacidad. |

La integración crea controles en tiempo de ejecución para los límites de estado de carga y potencia, así que puedes ajustarlos sin volver a ejecutar el asistente de configuración. **Battery Manual Control** reserva una batería para tus propias órdenes de carga, descarga o reposo; consulta el [control manual en una instalación con varias baterías](../../features/multi-battery.md#control-manual-por-bateria).

## Si la vía de batería no está clara

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| Tu modelo no aparece en la tabla | El driver podría admitir una familia aún no documentada, o el modelo podría no ser compatible | Compara el nombre exacto del modelo con la página de marca correspondiente antes de configurarlo |
| El asistente no puede conectarse | Falta una opción del producto, protocolo local, dirección o credencial | Completa la lista **Antes de empezar** de la página de la marca |
| La batería se conecta pero ignora órdenes | El modo de gestión energética del fabricante sigue teniendo el control | Comprueba el ajuste específico de la app de la marca y **Battery Manual Control** |
| Un límite es menor de lo previsto | La envolvente detectada del dispositivo es menor que el límite de software configurado | Comprueba la tabla del modelo y el límite comunicado por la batería |

??? "Detalles avanzados"
    Omnibattery solicita cada batería por separado, por lo que pueden mezclarse marcas compatibles en una instalación. Los límites individuales de cada batería siempre se aplican incluso cuando están configurados límites de carga o descarga a nivel del sistema. Establecer cualquiera de los límites del sistema en `0 W` desactiva ese límite.

    Los límites de potencia y estado de carga en tiempo de ejecución se conservan y restauran después de reiniciar Home Assistant. **Battery Manual Control** primero verifica un objetivo en reposo, saca la batería del grupo automático y también restaura ese control tras el reinicio.

    **Backup Offgrid Threshold** tiene como valor predeterminado `50 W`. Déjalo en cero cuando no haya ninguna carga permanente conectada, o establécelo por encima de la carga normal permanente del puerto de respaldo para que un router o conmutador de red no parezca un evento de respaldo. Cuando **Backup Function** está activado y la carga medida supera el umbral, Omnibattery excluye la batería del control proporcional–derivativo (PD). Espera `5 min` después de que baje la carga antes de devolver la batería al grupo automático.

    Configuración relacionada:

    - [Franjas horarias](../time-slots.md) controla cuándo las baterías pueden cargar o descargar.
    - [Carga predictiva](../predictive-charging/index.md) puede programar la carga desde la red.
    - [Gestión de varias baterías](../../features/multi-battery.md) explica el reparto de potencia y el control manual.
