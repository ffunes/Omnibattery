# Baterías Hoymiles MQTT

Omnibattery controla las baterías Hoymiles compatibles mediante la integración MQTT ya configurada en Home Assistant. Home Assistant controla la conexión con el broker, por lo que Omnibattery solo necesita el ID de dispositivo de la batería.

## ¿Lo necesito?

| Producto compatible | Capacidad nominal | Límite de potencia de integración | Solar visible para Omnibattery |
|---|---:|---:|---|
| MS-A2 | 2,24 kWh por unidad; hasta 4,48 kWh | 1.000 W por unidad; hasta 2.000 W | No |
| HiBattery 1920 AC | 1,92 kWh por unidad; hasta 11,52 kWh | 1.000 W por unidad; hasta 6.000 W | No |
| HiBattery 4020 X | 4,02 kWh por paquete; hasta 16,08 kWh | 2.500 W de carga y descarga | Sí |
| HiBattery 4020 AC | 4,02 kWh por paquete; hasta 16,08 kWh | 2.500 W de carga y descarga | No |

**Úsalo si** el firmware de la batería expone **MQTT Service**, la batería puede acceder a tu broker MQTT local y conoces su ID de dispositivo completo. La envolvente MQTT publicada por el dispositivo sigue siendo la fuente autorizada y puede reducir el límite de la tabla.

## Antes de empezar

- Pon en servicio la batería en **S-Miles Home**.
- Instala firmware que exponga **MQTT Service**.
- Configura un broker MQTT local mediante Home Assistant y haz que sea accesible desde la batería.
- Anota el ID de dispositivo MQTT completo.
- Para una MS-A2, sigue la [guía de instalación y MQTT de MS-A2](../hoymiles-ms-a2.md).

## Cómo añadirla

1. En el asistente de configuración de Omnibattery, elige **Hoymiles MQTT**.
2. Introduce un **Name** descriptivo y el **MQTT device ID** completo.
3. Deja **Battery model** en **Auto-detect** salvo que el firmware publique un modelo incorrecto o genérico.
4. Espera mientras Omnibattery recibe telemetría en vivo y el mensaje retenido de descubrimiento del control de potencia.
5. Revisa la capacidad y los límites de potencia detectados, y después elige los límites comunes de estado de carga.

## Qué verás

Omnibattery envía objetivos automáticos y manuales de carga, descarga y reposo por MQTT. Activa **Battery Manual Control** antes de usar **Force Mode** y los controles de potencia por software. La orden se actualiza mientras el control permanece activo para que la batería no vuelva a su estrategia interna.

El estado de carga (SOC), potencia de batería, tensión, temperatura, capacidad y lecturas de energía aparecen cuando el modelo las publica. Solo HiBattery 4020 X se considera una fuente solar del sistema. Ningún perfil Hoymiles compatible expone telemetría MPPT para la corrección MPPT específica de Marstek.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El asistente indica **Cannot connect** | MQTT está desconectado, el servicio está desactivado o el ID de dispositivo está incompleto | Comprueba la integración MQTT de Home Assistant, S-Miles Home y el ID completo |
| Aparece el modelo o capacidad equivocados | El firmware publicó un modelo genérico o incorrecto | Vuelve a configurar y elige explícitamente el modelo instalado |
| La potencia es menor que la tabla | La envolvente MQTT retenida es menor o asimétrica | Inspecciona el mensaje de descubrimiento del control de potencia del dispositivo |
| La batería vuelve al control autónomo | Las actualizaciones de órdenes no pueden llegar al broker | Comprueba la disponibilidad del broker y las desconexiones MQTT |
| Falta solar en un modelo de CA | Solo HiBattery 4020 X declara una fuente solar independiente | Usa el sensor solar externo de la instalación si lo necesitas |

??? "Detalles avanzados"
    Los alias de modelo MQTT detectados incluyen `MS-A2`, `MS-A2-FX`, `MS-A2-ZZ`, `HB-1920-AC-SV`, `HB-4020-X`, `HB-4020-XM`, `HB-4020-AC` y `HB-4020-ACM`. La capacidad escala con las unidades o paquetes detectados hasta el total del modelo indicado arriba.

    Los perfiles HiBattery 4020 X y 4020 AC usan un límite de integración simétrico de `2,500 W` incluso cuando pilas de expansión mayores pueden admitir más. El funcionamiento a mayor potencia queda fuera del alcance actual de la integración. Una envolvente MQTT `min`/`max` retenida menor siempre prevalece.

    Hoymiles MQTT no expone cortes de SOC escribibles ni tensiones de celdas individuales. Omnibattery aplica los límites de SOC por software y las funciones Marstek de equilibrio de celdas y reducción gradual por tensión no están disponibles.

    Omnibattery publica `mqtt_ctrl` más el objetivo con signo y después actualiza la orden exacta cada `30 s`. Una actualización fallida se reintenta tras `5 s`. Al descargar la integración envía reposo y restaura el modo general del dispositivo.

    Las entradas existentes del anterior asistente exclusivo para MS-A2 se corrigen al reconfigurarlas cuando el descubrimiento identifica un modelo distinto. Solo se sustituyen los valores predeterminados antiguos de MS-A2; se conservan los valores ajustados por el usuario.

    Referencias del fabricante:

    - [Guía del protocolo MQTT de Hoymiles](https://www.hoymiles.com/uploadfile/1/202511/9350aa1077.txt)
    - [HiBattery 1920 AC](https://www.hoymiles.com/products/hibattery-1920-ac.html)
    - [Ficha técnica HiBattery 4020 X](https://www.hoymiles.com/uploadfile/1/202606/95d670b3a3.pdf)
    - [Manual de usuario HiBattery 4020 X](https://www.hoymiles.com/downloads/user-manual-hb-4020-x-global-en-de-fr-nl.html)
    - [HiBattery 4020 AC](https://www.hoymiles.com/products/hibattery-4020-ac.html)
    - [Manual de usuario HiBattery 4020 AC](https://www.hoymiles.com/downloads/user-manual-hb-4020-ac-global-en-de-fr-nl.html)

    Para controles compartidos en tiempo de ejecución y límites del sistema, consulta [Elige la conexión de tu batería](index.md).
