# Resumen de registros Modbus de Marstek

Esta referencia cubre los controladores basados en registros de Marstek Venus que utiliza Omnibattery. Los demás controladores de batería usan su propia API local, MQTT o contrato de entidades de Home Assistant, y no utilizan este mapa.

!!! warning "La escritura de registros puede interrumpir el control normal"
    Prioriza las entidades y controles de Omnibattery. Las escrituras directas pueden entrar en conflicto con el control automático, seleccionar un modo de funcionamiento inseguro o dejar activa una consigna obsoleta. No escribas una dirección sin haber comprobado el modelo, la familia de firmware, el tipo de dato, la escala y el valor permitido.

La [tabla completa de registros](registers.md) es una referencia técnica **solo en inglés**.

## Familias de firmware

| Código en la tabla completa | Selección de hardware de Omnibattery |
|---|---|
| `a` | Venus A |
| `d` | Venus D |
| `e_v12` | Venus E v2 |
| `e_v3` | Venus E v3 |

Una celda vacía significa que Omnibattery no define esa clave para la familia seleccionada. No demuestra que la dirección física no sea utilizada por el firmware.

## Cómo leer la tabla

- Un registro Modbus son dos bytes. Los valores de varios registros ocupan direcciones consecutivas.
- Los tipos con y sin signo deben decodificarse de forma distinta.
- Aplica la escala indicada tras decodificar el valor sin procesar.
- Los campos de bits representan varias marcas independientes.
- Las filas calculadas no tienen registro físico.

| Tipo | Anchura habitual | Significado |
|---|---:|---|
| `uint16`, `int16` | un registro | Entero sin signo o con signo |
| `uint32`, `int32` | dos registros | Entero sin signo o con signo |
| `uint48` | tres registros | Entero sin signo |
| `uint64` | cuatro registros | Entero sin signo o campo de bits |
| `char` | variable | Texto |
| `bit` | específico del modelo | Marcas |

## Registros clave utilizados por el control

| Finalidad | Venus E v2 | Venus E v3 | Venus A | Venus D |
|---|---:|---:|---:|---:|
| SOC de batería | `32104` | `37005` | `32104` | `32104` |
| Potencia de batería | `32102` | `30001` | `30001` | `30001` |
| Control RS-485 | `42000` | `42000` | `42000` | `42000` |
| Modo forzado | `42010` | `42010` | `42010` | `42010` |
| Consigna de carga | `42020` | `42020` | `42020` | `42020` |
| Consigna de descarga | `42021` | `42021` | `42021` | `42021` |
| Corte de carga por hardware | `44000` | No disponible | No disponible | No disponible |
| Corte de descarga por hardware | `44001` | No disponible | No disponible | No disponible |
| Potencia máxima de carga | `44002` | `44002` | `44002` | `44002` |
| Potencia máxima de descarga | `44003` | `44003` | `44003` | `44003` |

Los límites de estado de carga de Venus E v3, Venus A y Venus D se aplican mediante software porque esos mapas de firmware no exponen los registros de corte de v2.

## Antes de diagnosticar con registros directos

1. Confirma la familia de hardware seleccionada en la entrada de configuración.
2. Compara la entidad de Home Assistant relacionada con la fila de la tabla completa.
3. Comprueba si la fila es de telemetría, configuración o una orden de control inmediata.
4. Descarga los diagnósticos de Omnibattery antes de modificar nada.
5. Usa primero la [guía de solución de problemas](../troubleshooting.md) y las entidades de batería.

??? "Temporización del transporte"
    Omnibattery aplica por sí mismo la cadencia y los tiempos de espera específicos de cada firmware. Venus E v3, Venus A y Venus D requieren un mínimo de 150 ms entre mensajes Modbus TCP; Venus E v2 requiere 50 ms. El sondeo o las escrituras externas en la misma conexión pueden retrasar las respuestas e interferir con el tráfico del controlador. Una pasarela RS-485 usa un perfil de cadencia distinto y más corto que el servidor Modbus TCP nativo de la batería.
