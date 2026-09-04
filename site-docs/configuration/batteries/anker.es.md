# Anker SOLIX

Omnibattery es compatible con Anker SOLIX Solarbank Max AC y Solarbank 4 E5000
Pro mediante Modbus TCP. La prueba de conexión lee los límites de hardware en
tiempo real y el identificador del modelo.

!!! warning "Activa Third-Party Control"
    Activa **Third-Party Control** y Modbus TCP en la aplicación de Anker antes de añadir la batería. Solo un cliente Modbus puede conectarse a la Solarbank a la vez.

## Conexión

Introduce el nombre de la batería, su IP local, el puerto Modbus y el ID de
esclavo.

| Campo | Descripción | Por defecto |
|---|---|---|
| **Nombre** | Nombre usado para el dispositivo de batería | — |
| **IP del host** | IP local de la Solarbank | — |
| **Puerto Modbus** | Puerto TCP | `502` |
| **ID de esclavo Modbus** | ID de unidad usado por el dispositivo | `1` |

La prueba de conexión consulta el mapa Modbus antes de continuar. Cierra la
aplicación de Anker o desconéctala si ya está usando la única sesión Modbus
disponible.

## Límites de potencia y SOC

Anker comunica sus límites de carga y descarga, por lo que el asistente no
solicita sliders de potencia manuales durante la configuración. Omnibattery usa
esos límites de hardware en vivo y los restringe a una envolvente de software de
3500 W.

La página de límites también incluye:

- SOC máximo: 80–100 % (por defecto `100 %`);
- SOC mínimo: 0–20 % (por defecto `10 %`);
- histéresis de carga obligatoria (mínimo 2 %);
- umbral de backup offgrid.

Anker no ofrece la reducción de carga por tensión de celdas de Marstek. Para los
controles de SOC en tiempo de ejecución, los límites del sistema y los umbrales
de backup, consulta la [configuración de baterías](index.md).

### Control manual

Anker no ofrece entidades de modo forzado y consignas de potencia al estilo de
Marstek. Omnibattery guarda los valores de software `Modo forzado`, `Potencia
de carga` y `Potencia de descarga`, y reaplica las consignas distintas de reposo
mediante el driver local mientras **Control Manual de Batería** esté activado.
Mantén **Third-Party Control** activado en la aplicación de Anker; el driver y
el BMS siguen siendo responsables de sus propios límites de seguridad de
hardware.

## Diagnóstico

| Lectura | Entidad | Fuente |
|---|---|---|
| **Estado de salud (SoH)** | `sensor.<battery>_battery_soh` | Registro de entrada Modbus **10015** |

El SoH se expone para todos los modelos Anker Solarbank compatibles que comparten
el mapa de registros común. Solo se ha verificado en campo en **Solarbank Max AC**
(código de producto **DMWH**); otros modelos pueden informar el mismo registro,
pero aún no están confirmados en campo.

Anker no expone tensión del pack, tensiones por celda ni telemetría de equilibrio
de celdas por Modbus. Por eso Omnibattery no crea `battery_voltage`,
`max_cell_voltage`, `min_cell_voltage` ni sensores del monitor de equilibrio para
baterías Anker.

### Panel

La sección **Salud y celdas** de la tarjeta de batería del panel de Omnibattery
muestra solo las métricas disponibles en cada dispositivo:

- **Anker**: temperatura interna y **Estado de salud (SoH)** cuando está
  disponible.
- **Marstek / Zendure / otros**: temperatura, tensión, celdas mín./máx., delta
  de celda y cualquier otro sensor expuesto por el driver.

Las filas de tensión y celdas se omiten automáticamente cuando la integración no
tiene entidades equivalentes, de modo que las tarjetas Anker ya no muestran
marcadores vacíos.
