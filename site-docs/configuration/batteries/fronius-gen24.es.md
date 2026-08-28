# Fronius GEN24 con BYD Battery-Box

Omnibattery puede supervisar y controlar una BYD Battery-Box mediante la
interfaz de almacenamiento de un inversor Fronius GEN24. El controlador usa
Modbus TCP local para el control y la telemetría rápida, y la Solar API local
del inversor para el modelo, número de serie, temperatura, tensión, corriente y
capacidad de la batería BYD.

## Requisitos

- Un inversor Fronius GEN24 con almacenamiento BYD compatible
- Modbus TCP habilitado en el inversor
- Control de almacenamiento habilitado en el inversor
- Acceso desde Home Assistant al puerto TCP `502` y a la API HTTP del inversor

Selecciona **Fronius GEN24 / BYD** e introduce el host del inversor, el puerto
Modbus y el ID de unidad SunSpec. Los valores predeterminados son el puerto
`502` y el ID `1`.

## Representación SunSpec compatible

Este controlador admite la representación SunSpec de **entero más factor de
escala (`int+SF`)**. Lee el bloque de control de almacenamiento y sus factores
de escala de los registros `40355`–`40378`; el modelo SunSpec de coma flotante
no se detecta automáticamente ni es compatible actualmente.

## Límites de SOC

Omnibattery aplica `min_soc` y `max_soc` mediante su bucle de control. En
particular, `max_soc` **no es un corte de hardware garantizado**: el GEN24 u
otra automatización puede seguir cargando desde la energía fotovoltaica y la
BYD puede superar el valor configurado. El inversor y el BMS conservan la
responsabilidad de sus límites físicos de seguridad.

## Identidad y persistencia

El número de serie físico de la BYD se lee desde
`GetStorageRealtimeData.cgi`. Omnibattery utiliza ese número para la copia de
seguridad de energía sintética, de modo que puede recuperar la energía
acumulada al eliminar y volver a añadir la batería aunque cambie la dirección
del inversor. Hasta obtener el número real, no se usa un sustituto derivado del
host.

## Confirmación de consignas

En la instalación GEN24/BYD usada para validar el controlador, los registros
escritos se podían leer tras una espera de `0,2 s`. La respuesta física de
potencia puede tardar más; Omnibattery anuncia una latencia de lectura de
`1,5 s` al controlador y sigue comprobando la potencia medida en las
actualizaciones normales de telemetría.
