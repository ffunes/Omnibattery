# Marstek Venus

Usa esta conexión para una batería Marstek Venus a la que se llega directamente por Ethernet, mediante una pasarela Modbus o mediante un adaptador USB a RS-485. Un puente LilyGo utiliza su propia opción en el asistente.

## ¿Lo necesito?

| Batería | Versión en el asistente | Potencia máxima de carga y descarga |
|---|---|---:|
| Venus E v2 | **Ev2** | 2.500 W |
| Venus E v3 | **Ev3** | 2.500 W |
| Venus A | **A** | 1.500 W |
| Venus D | **D** | 2.200 W antes del firmware EMS 149, o cuando se desconoce el firmware; 2.500 W desde el firmware 149 |

**Usa esta página si** una de estas opciones del asistente coincide con tu batería y puedes acceder a ella mediante Modbus TCP o Modbus RTU. **Usa en su lugar la [vía ESPHome / LilyGo](esphome-lilygo.md)** cuando un puente ESPHome compatible ya exponga la batería en Home Assistant.

## Antes de empezar

- Para Venus E v2, prepara una pasarela de RS-485 a TCP o un adaptador USB a RS-485.
- Para Venus E v3, Venus A o Venus D, confirma que Home Assistant puede acceder a la dirección Ethernet de la batería.
- Conoce el ID de esclavo Modbus y la versión exacta de Venus.
- Si varias baterías comparten una pasarela, asigna a cada unidad su propio ID de esclavo.

## Cómo añadirla

1. En el asistente de configuración de Omnibattery, elige **Marstek Venus**.
2. Introduce un nombre de batería y **Host IP** o **Puerto serie (Modbus RTU)**. Deja **Puerto serie (Modbus RTU)** vacío para una conexión de red.
3. Mantén **Puerto Modbus** en `502` salvo que la batería o pasarela use otro puerto.
4. Introduce el **ID de esclavo Modbus** y selecciona **Ev2**, **Ev3**, **A** o **D**.
5. Continúa al formulario de límites y elige valores no superiores al límite del modelo indicado arriba.

![Formulario de conexión Marstek](../../assets/screenshots/configuration/battery-connection-form.png){ width="650" style="display: block; margin: 0 auto;" }

## Qué verás

Marstek expone control de carga/descarga automático y manual, estado de carga (SOC), lecturas de potencia y energía, y los sensores disponibles en el mapa de registros seleccionado. Las lecturas solares y de alarmas solo aparecen en los modelos cuyo mapa las proporciona.

Venus E v2 puede usar cortes de SOC de hardware. Omnibattery aplica los límites de SOC configurados por software para Venus E v3, Venus A y Venus D. Venus A y Venus D también preguntan si hay solar de corriente continua conectada para que los cálculos de flujo de potencia usen la fuente correcta.

![Formulario de configuración Marstek](../../assets/screenshots/configuration/battery-config-form.png){ width="650" style="display: block; margin: 0 auto;" }

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El asistente indica **Cannot connect** | La dirección, ruta serie, puerto, ID de esclavo, cableado o mapa seleccionado es incorrecto | Prueba la conectividad de red o el adaptador serie y después verifica el modelo y el ID de esclavo |
| Los valores son inverosímiles o no están disponibles | La versión Venus seleccionada usa otro mapa de registros | Vuelve a configurar la batería con la versión exacta del modelo |
| Venus D está limitada a 2.200 W | El firmware EMS es anterior al 149 o no se pudo leer su versión | Comprueba el firmware EMS antes de esperar el límite superior |
| Falta una batería conectada por LilyGo | Se seleccionó la vía Marstek directa | Vuelve al paso de marca y selecciona **Marstek via LilyGo RS485 (ESPHome)** |
| La carga se ralentiza cerca del máximo | Está activa la reducción gradual de carga completa opcional | Comprueba **100% Charge Voltage Taper** y los sensores de tensión de celda |

??? "Detalles avanzados"
    Marstek usa modo forzado nativo y consignas de carga y descarga independientes. Los registros disponibles dependen de la versión seleccionada; Omnibattery obtiene de ese mapa el modo forzado, solar, alarmas, cortes de SOC de hardware y la capacidad de control RS-485.

    En Venus E v2 y v3, los registros de potencia máxima de carga y descarga de la batería son el mismo selector de dos valores que muestra la app Marstek: `800 W` o `2,500 W`. Omnibattery escribe `800 W` cuando tu **Max Charge Power** o **Max Discharge Power** es `800 W` o menos, y `2,500 W` en caso contrario; después aplica tu límite exacto por software. Por tanto, leer esos registros directamente muestra `800` o `2500`, no tu ajuste.

    La **100% Charge Voltage Taper** opcional es específica de Marstek. Con un objetivo del 100%, limita la carga a `200 W` cuando la celda medida más alta alcanza `3.48 V`. Venus E hace una pausa a `3.60 V` y espera `60 s` antes de evaluar el desequilibrio de celdas. Los sistemas Venus A y D con paquetes acoplados continúan a la potencia de reducción gradual hasta que el sistema de gestión de batería (BMS) termina la carga. Consulta [Monitor de equilibrio de celdas](../../features/cell-balance-monitor.md).

    Una conexión Modbus TCP normalmente usa el puerto `502`; el ID de esclavo acepta el rango de unidades Modbus `1–247`. Una ruta serie como `/dev/ttyUSB0` o `COM3` selecciona Modbus RTU en lugar de la dirección de host.

    Para controles compartidos de estado de carga, límites del sistema y comportamiento de respaldo, consulta [Elige la conexión de tu batería](index.md).
