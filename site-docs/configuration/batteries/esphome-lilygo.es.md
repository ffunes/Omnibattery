# ESPHome / LilyGo RS485

Usa esta vía para incorporar una Marstek Venus E a Omnibattery mediante una placa LilyGo con ESPHome. Mantiene el control de la batería en Home Assistant cuando la placa ya está conectada al puerto RS-485 de la batería, en lugar de conectarse a la batería con Modbus TCP.

## ¿Lo necesito?

| Batería | Vía de conexión | Elige esta vía cuando |
|---|---|---|
| Marstek Venus E con el mapa de registros v2 | Puente LilyGo RS485 expuesto mediante ESPHome | El puente ya está instalado y aparece como dispositivo ESPHome en Home Assistant |

**Úsala si** tienes el puente LilyGo/ESPHome compatible y quieres que Omnibattery use las entidades de batería que expone. **No la necesitas si** Home Assistant llega a la batería directamente mediante Modbus TCP, una pasarela Modbus o un adaptador serie; usa en su lugar [Marstek Venus](marstek.md).

Esta vía es para Venus E. No es una vía para Venus A, Venus D ni Venus E v3.

## Antes de empezar

- Instala el [proyecto de firmware marstek-lilygo-rs485](https://github.com/whyisthisbroken/marstek-lilygo-rs485) en una placa LilyGo T-CAN485 y añádela a Home Assistant mediante ESPHome. Para el cableado e instalación del firmware, consulta ese proyecto.
- Confirma que el dispositivo ESPHome está en línea y que sus entidades de batería usan los nombres estándar del firmware.
- Confirma que las entidades **Battery State Of Charge**, **Battery Power** y **AC Power** de la batería tienen valores utilizables.

## Cómo añadirla

1. Abre **Ajustes → Dispositivos y servicios → Omnibattery → Configurar**. Añade una batería si la instalación aún no tiene una.
2. En el paso de marca de batería, selecciona **Marstek via LilyGo RS485 (ESPHome)**.
3. En **Configure battery {battery number} — Connection (LilyGo/ESPHome)**, introduce un **Name** y selecciona el **ESPHome device** que representa el puente LilyGo.
4. Continúa a **Configure battery {battery number} — Limits** y establece **Maximum charge power (W)**, **Maximum discharge power (W)**, **Maximum SOC (%)** y **Minimum SOC (%)** para la batería.

![Selector de marca de batería con la vía LilyGo ESPHome](../../assets/screenshots/configuration/battery-brand-form.png){ width="650" style="display: block; margin: 0 auto;" }

## Qué verás

Omnibattery proporciona control automático de carga/descarga y los controles Marstek habituales, incluidos **Force Mode**, **Set Forcible Charge Power**, **Set Forcible Discharge Power**, **Max Charge Power**, **Max Discharge Power** y los cortes de estado de carga de hardware.

También verás estado de carga (SOC), potencia de batería y CA, contadores de energía, temperatura, tensión, tensión de celda, estado del inversor y diagnósticos de conexión cuando el firmware los publique. La telemetría se actualiza normalmente en el sondeo de batería del puente de aproximadamente 3 segundos.

A diferencia de una conexión Modbus directa, Omnibattery no se comunica con la propia batería en esta vía: lee y escribe las entidades ESPHome. No hay telemetría solar MPPT y las entidades individuales de avisos sustituyen a los registros de alarma agregados.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| La placa LilyGo no se ofrece como **ESPHome device** | ESPHome no ha añadido la placa como dispositivo o está sin conexión | Comprueba la integración ESPHome y confirma que la placa está en línea en Home Assistant |
| El asistente indica que faltan entidades requeridas | La placa no usa el firmware compatible o se cambiaron sus nombres de entidad | Instala el firmware compatible y restaura sus nombres de entidad estándar; consulta los nombres requeridos abajo |
| La configuración termina pero la batería no está disponible | El puente está en línea, pero **Battery State Of Charge** es `unknown` o `unavailable` | Comprueba que la batería y el puente se comunican y espera un valor de SOC utilizable |
| Las órdenes no tienen efecto | Falta una entidad de control ESPHome requerida o el puente no llega a la batería | Comprueba **Forcible Charge-Discharge**, **Forcible Charge Power**, **Forcible Discharge Power** y **RS485 Control Mode** en el dispositivo ESPHome |
| Los valores dejan de cambiar mientras la placa sigue pareciendo en línea | Se ha detenido el sondeo de batería del puente | Comprueba los registros de ESPHome y la conexión RS-485; Omnibattery deja de usar telemetría de batería obsoleta en lugar de controlar con valores antiguos |

??? "Detalles avanzados"
    Omnibattery identifica esta conexión por el dispositivo ESPHome de Home Assistant seleccionado, no por una dirección IP o extremo Modbus. El puente controla la conexión RS-485, por lo que esta vía no tiene una vía Modbus directa paralela.

    La coincidencia de entidades usa el nombre original del registro de entidades ESPHome, convertido a un slug. Por tanto, renombrar un ID de entidad en Home Assistant normalmente no rompe la coincidencia. Los nombres de entidad estándar requeridos son **Battery State Of Charge**, **Battery Power**, **AC Power**, **Forcible Charge-Discharge**, **Forcible Charge Power**, **Forcible Discharge Power** y **RS485 Control Mode**.

    Las órdenes usan los servicios `select.select_option` y `number.set_value` de Home Assistant. El puente sondea la batería aproximadamente cada 3 segundos; Omnibattery trata toda la telemetría procedente de la batería como obsoleta tras 120 segundos sin ningún informe, incluso si las entidades Wi-Fi locales de ESP siguen actualizándose.
