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

## Detección del modelo SunSpec

El controlador admite las configuraciones SunSpec **`float`** e **`int+SF`**
de Fronius y detecta automáticamente el diseño activo mediante la cabecera del
modelo Basic Storage Control (124). No es necesario seleccionar el tipo de
modelo en Omnibattery.

Fronius aplica la representación elegida al modelo de inversor anterior. Los
modelos 160 y 124 siguen usando enteros y factores de escala en ambos diseños,
pero sus direcciones se desplazan diez registros:

| Bloque | `float` | `int+SF` |
|---|---:|---:|
| Datos del modelo Multiple MPPT 160 | `40265` | `40255` |
| Datos del modelo Basic Storage 124 | `40355` | `40345` |

Estas posiciones corresponden a la
[documentación oficial de Modbus para Fronius GEN24](https://manuals.fronius.com/html/4204102649/es.html#BasicStorageControlsRegister).

Todas las lecturas, escrituras de consignas y comprobaciones usan el diseño
detectado. El tipo aparece como **Modelo SunSpec** en la caja de información de
la batería.

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
