# Añadir un controlador de batería

Esta guía lleva una nueva integración de baterías desde la evidencia del fabricante hasta una solicitud de extracción (pull request) revisable en Omnibattery. Un controlador puede utilizar registros, una interfaz de programación de aplicaciones (API) local, Mensajería con Cola de Transmisión Telemétrica (MQTT), o entidades de Home Assistant, pero debe exponer el mismo comportamiento semántico de batería a través de `drivers/base.py::BatteryDriver`.

Comienza con evidencia de hardware real. Un documento de protocolo por sí solo no puede establecer la polaridad, la escalada, la persistencia de comandos, la latencia o un comportamiento seguro tras una escritura parcial.

## Decidir si el dispositivo es adecuado

Utiliza estos niveles de requisitos al evaluar el dispositivo:

| Código | Significado |
|---|---|
| **B** | Bloqueante. No habilitar el control automático bidireccional sin él. |
| **R** | Necesario para un soporte de producción robusto. Documenta cualquier mitigación provisional. |
| **O** | Opcional. Su ausencia elimina una característica o entidad, no el control central. |

Registra de dónde proviene cada valor:

| Código | Fuente |
|---|---|
| **N** | Valor nativo del dispositivo o control. |
| **D** | Derivado por el controlador a partir de datos nativos validados. |
| **C** | Constante de modelo configurada por el usuario o validada. |
| **X** | No compatible; omitir o desactivar la entidad o característica dependiente. |

Un dispositivo es:

- **ADECUADO** cuando cada requisito B y R está cubierto.
- **ADECUADO CON LIMITACIONES** cuando cada requisito B está cubierto pero falta un elemento R u O, documentando las características y riesgos afectados.
- **NO ADECUADO** cuando falta un elemento B, no se pueden confirmar los semánticos de comandos, o el control depende de una interfaz inestable o no autorizada.

### Puerta de acceso para control automático

Cada elemento en esta lista es bloqueante:

- [ ] Un transporte programable soporta conexión, reconexión y cierre controlados.
- [ ] Un estado de carga (SOC) fresco está disponible como porcentaje.
- [ ] La potencia de batería medida está disponible directamente o puede derivarse de mediciones simultáneas.
- [ ] El dispositivo acepta comandos de carga y descarga limitados por la energía.
- [ ] El dispositivo acepta y mantiene un comando de reposo seguro (`0 W`).
- [ ] Se conocen los máximos seguros de carga y descarga por dispositivo.
- [ ] Las protecciones del sistema de gestión de baterías (BMS) del fabricante permanecen activas bajo control externo.
- [ ] El ritmo de escritura no desgasta la memoria flash ni viola los límites de la API.
- [ ] Se puede detectar comunicación obsoleta o perdida sin reproducir valores antiguos indefinidamente.

Sin SOC, potencia medida, ninguna dirección de control, o reposo fiable, el dispositivo no es adecuado para el control automático bidireccional. Se puede proponer soporte solo de monitorización por separado, pero no es un controlador de batería completo.

## Recopilar evidencia del fabricante y hardware

Abre una incidencia o nota de ingeniería con una evaluación por fabricante, modelo y familia de firmware. Rellena esta tabla antes de programar:

| Campo | Evidencia |
|---|---|
| Fabricante y modelo comercial | `...` |
| Modelo reportado por el dispositivo | `...` |
| Versiones de firmware probadas | `...` |
| Región o variante de hardware | `...` |
| Capacidad nominal y potencia de carga/descarga | `...` |
| Topología acoplada por CA, CC o híbrida | `...` |
| Documento oficial, revisión, fecha y enlace | `...` |
| Autorización del fabricante o contacto de soporte | `...` |
| Hardware utilizado para validación | `...` |
| Fecha de prueba | `...` |

Recopila suficiente detalle para responder a todas estas preguntas:

- ¿Qué modelos y versiones de firmware utilizan el mismo protocolo y distribución de campos?
- ¿Es el acceso local, basado en la nube o ambos? ¿Qué sucede sin acceso a internet?
- ¿Cómo se maneja la autenticación, renovación de tokens, encriptación de transporte y validación de certificados?
- ¿Qué dispositivo, unidad, punto final o tema identifica una batería física individual?
- ¿Cuáles son los límites de tiempo, reintentos, concurrencia, conexión, tamaño de solicitud y velocidad?
- ¿El telescopio lleva una marca de tiempo, número de secuencia o vida útil?
- Para cada campo, ¿cuáles son su tipo, orden de bytes, unidad, escala, signo, rango válido y valores sentinel?
- Para cada escritura, ¿cuáles son su rango, paso, persistencia, confirmación y respuesta de error?
- ¿Son atómicos los comandos de multi-escritura? Si no, ¿qué orden y vuelta atrás alcanzan un estado seguro?
- ¿Un comando sobrevive el reinicio del dispositivo, pérdida de red, recarga de Home Assistant y apagado de Omnibattery?
- ¿Una escritura frecuente actualiza memoria volátil o flash persistente?

Mantén capturas de solicitud/respuesta borradas y observaciones de hardware. Nunca incluyas credenciales, tokens, direcciones de red o números de serie completos en pruebas, diagnósticos, documentación o solicitudes de extracción.

### Matriz de compatibilidad de firmware

| Modelo | Firmware | Transporte | Lectura | Escritura | Diferencias conocidas | Hardware probado |
|---|---|---|---|---|---|---|
| `...` | `...` | `...` | `yes/no` | `yes/no` | `...` | `yes/no` |

Una API solo en la nube no se rechaza automáticamente. Su latencia, expiración, cuotas y comportamiento durante interrupciones deben seguir soportando reposo seguro y el ritmo de control requerido.

## Mapear el protocolo del fabricante a Omnibattery

El controlador traduce detalles del protocolo en claves lógicas canónicas y operaciones. Registros, puntos finales, temas, nombres de servicio y modos propietarios deben permanecer dentro del controlador o su cliente de transporte.

Usa estas convenciones:

- La potencia neta firmada es positiva mientras carga, negativa mientras descarga y cero mientras está inactivo.
- `battery_power` es una medición física con la misma convención de signo, no el último comando.
- Publica unidades finales en W, kWh, %, V y °C.
- Omitir un valor fallido o no disponible. Nunca reemplazarlo con cero cuando cero es válido.
- Clampear comandos a la envoltura declarada del dispositivo.
- Devolver un `SetpointResult` coherente de cada ruta `apply_setpoint()`, incluyendo fallos y escrituras sin retroalimentación inmediata.
- Tratar una caché de intenciones como historial de comandos. No es entrega medida.

### Telemetría y controles obligatorios

| Clave canónica u operación | Nivel | Requisito del fabricante | Sustituto aceptado |
|---|---|---|---|
| `battery_soc` | B | SOC real fresco como porcentaje | Un estimado de voltaje no es soporte completo. |
| `battery_power` | B | Potencia instantánea en ambas direcciones | Fórmula D de flujos simultáneos validados. |
| `apply_setpoint(+W)` | B | Carga limitada por la energía | Modo más límite, o una propiedad firmada. |
| `apply_setpoint(-W)` | B | Descarga limitada por la energía | Modo más límite, o una propiedad firmada. |
| `apply_setpoint(0)` y `standby()` | B | Reposo mantenido sin importación/exportación autónoma | Secuencia documentada de cero-límite y modo. |
| Potencia máxima | B | Valores seguros por modelo o dispositivo | Valores C acotados por máximos oficiales. |
| Disponibilidad y frescura | B | Error, marca de tiempo, secuencia o equivalente | Temporizador de expiración de caché del controlador. |
| Eco de setpoint | R | Modo aplicado y límite | Una caché de intenciones puede optimizar escrituras pero no confirma la entrega. |
| Latencia de actuador | R | Retraso comando-respuesta física | Medición de hardware con margen conservador. |
| Latencia de retroalimentación | R | Retraso comando-telemetría establecida | Reutilizar latencia de actuador solo cuando pruebas demuestran que coinciden. |
| Potencia mínima fiable | R | Mínimo no-cero sostenible y paso de comando | Constante C validada por modelo. |

### Telemetría opcional y degradación de características

| Datos canónicos | Habilita | Si falta |
|---|---|---|
| `battery_total_energy` | Energía almacenada, asignación y carga predictiva | Requiere capacidad nominal configurada. |
| Totales de energía de carga/descarga | Entidades de energía y eficiencia | Integrar potencia medida y persistir el resultado. |
| `max_cell_voltage`, `min_cell_voltage` | Comportamiento en la parte superior de la carga y monitorización de equilibrio | Desactivar características dependientes del voltaje. |
| `internal_temperature` | Limitación de potencia térmica | Desactivar limitación de temperatura. |
| `inverter_state` | Reposo y confirmación de corte BMS | Usar potencia medida y omitir detección dependiente. |
| `ac_offgrid_power` | Exclusión de carga de respaldo | Desactivar exclusión automática de respaldo. |
| MPPT o potencia solar agregada | Producción CC y cálculos solares | Declarar las capacidades solares aplicables como falsas. |
| Estado de alarma o fallo | Notificaciones de alarma | Omitir la entrada del sensor y notificador dependiente. |
| Voltaje de batería | Diagnósticos | Omitir la entidad. |
| Serial estable y firmware | Identidad del dispositivo y soporte | Usar la mejor clave de dispositivo estable y omitir entidades no disponibles. |
| Cortafoche de SOC en hardware | Límites autónomos persistentes | Permitir que el control compartido imponga límites por software. |
| Tope de potencia de hardware escribible | Configuración del dispositivo persistente | Usar un tope de software sin exponer un control de hardware falso. |
| Puerta de acceso de control externo | Entrar y restaurar control externo | Necesario solo cuando los setpoints dependen de la puerta. |
| Entrega del puerto AC del dispositivo | Comprobaciones de entrega correctas con DC solar compartido | Omitir `ac_delivered_power`; el código compartido solo regresa donde sea válido. |

Las características no compatibles deben estar bloqueadas mediante capacidades, definiciones de entidad o configuración. No crear entidades ni decisiones desde ceros fabricados.

## Implementar el contrato del controlador

Crea `custom_components/omnibattery/drivers/<brand>.py` y subclasea `BatteryDriver`. Usa un controlador existente con el transporte más cercano como punto de partida:

- `marstek.py`, `anker.py` y `huawei.py` muestran diseños Modbus consultados.
- `zendure.py` y `sessy.py` muestran diseños HTTP locales.
- `esphome.py` y `hoymiles.py` muestran diseños de entidades de Home Assistant alimentadas por impulso.

### Miembros abstractos

Implementa cada miembro abstracto en `drivers/base.py`:

| Miembro | Comportamiento requerido |
|---|---|
| `capabilities` | Devolver un objeto inmutable `DriverCapabilities` para el modelo conectado. |
| `connected` | Informar si el transporte o fuente upstream es actualmente usable. |
| `connect()` | Establecer o validar acceso; sea seguro llamar de nuevo tras un fallo. |
| `close()` | Liberar sesiones, suscripciones, clientes y recursos de conexión única. |
| `set_shutting_down(value)` | Suprimir ruido de transporte esperado durante descargo. |
| `read_groups` | Agrupar claves lógicas en unidades programables con nombres de ritmo válidos. |
| `read_telemetry(keys)` | Devolver valores lógicos decodificados, honrando el subconjunto de claves opcional. |
| `apply_setpoint()` | Clampear, traducir, escribir, confirmar opcionalmente y devolver `SetpointResult`. |
| `write_control()` | Escribir un control de entidad lógica o devolver `False` cuando no compatible. |
| `net_power_from_data()` | Reconstruir el comando firmada ecocado o devolver `None` cuando incompleto. |
| `control_dependency_keys` | Nombrar valores que las necesidades de control necesitan incluso cuando las entidades están deshabilitadas. |

`BatteryDriver` también proporciona ganchos semánticos opcionales: `dc_coupled`, `model_label`, `serial`, `balance_dependency_keys`, `supplemental_discharge_dependency_keys`, `supplemental_discharge_power_w()`, y `dynamic_discharge_limit_w()`.

### Ganchos llamados por el coordinador

El coordinador también invoca estos métodos por convención. No son actualmente abstractos en `BatteryDriver`, así que verifícalos explícitamente durante la revisión:

| Gancho | Comportamiento |
|---|---|
| `apply_config(max_soc_pct, min_soc_pct, max_charge_power_w, max_discharge_power_w)` | Aplicar valores de configuración soportados y saltar deliberadamente configuraciones inaplicables. |
| `standby()` | Dejar el dispositivo en un estado de reposo seguro antes de cerrar el transporte. |
| `set_charge_cutoff(soc_pct)` | Cambiar un cortafuegos de hardware cuando se soporta; de lo contrario devolver `False`. |
| `set_rs485_control(enable)` | Conmutar la puerta de acceso de control externo cuando se soporta; de lo contrario devolver `False`. |
| `get_rs485_control()` | Confirmar el estado de la puerta para controladores que declaran `has_rs485_control=True`. |

No afirmar una capacidad cuando su gancho correspondiente no puede cumplir el contrato.

### Declarar cada capacidad

Construye `DriverCapabilities` con evidencia para cada campo:

| Campo | Qué establecer |
|---|---|
| `hardware_soc_cutoff` | Si el hardware impone todo el rango de SOC visible por el usuario. |
| `has_force_mode` | Si un modo forzado distinto es parte de la secuencia de comandos. |
| `push_telemetry` | Si `read_telemetry()` devuelve una caché alimentada por impulso. |
| `max_charge_power_w`, `max_discharge_power_w` | Envoltura segura inclusiva para este modelo. |
| `min_charge_power_w`, `min_discharge_power_w` | Menor comando no-cero sostenible, o cero cuando no existe un suelo. |
| `has_mppt_pv` | Si existen canales de seguimiento del punto de máxima potencia (MPPT) distintos. |
| `has_solar_telemetry` | Si existe una producción solar agregada independiente. |
| `has_alarm_registers` | Si el estado nativo de alarma o fallo se expone. |
| `has_rs485_control` | Si una puerta de acceso de control externo puede conmutarse y confirmarse. |
| `has_energy_counters` | Si los contadores de energía acumulativa son nativos. |
| `has_daily_energy_counters` | Si los contadores nativos se reinician diariamente. |
| `has_nominal_capacity` | Si la capacidad nominal se reporta por el hardware. |
| `cycles_from_discharge_only` | Si el cálculo del ciclo debe usar solo energía descargada. |
| `setpoint_confirm_reliable` | Si la retroalimentación inmediata del comando es confiable. |
| `actuator_latency_s` | Tiempo de respuesta física medido conservador. |
| `readback_latency_s` | Peor tiempo antes de que la telemetría se establezca, o `None` para reutilizar latencia de actuador. |
| `engage_grace_s` | Permiso extra de reposo-a-activo, o `None` para el valor por defecto del controlador. |
| `telemetry_liveness_checked` | Si una lectura de caché prueba que la transmisión de datos frescos reanudó. |
| `charge_cutoff_range`, `discharge_cutoff_range` | Valores que la ruta de escritura de hardware acepta realmente. |

Los valores por defecto en la clase de datos son comportamiento de compatibilidad para controladores existentes. Un nuevo controlador debe establecer campos intencionalmente y explicar los valores dependientes del modelo en comentarios y pruebas.

### Definir entidades junto al decodificador

Expon estas propiedades del controlador incluso cuando una plataforma no tenga definiciones nativas:

```python
sensor_definitions
number_definitions
select_definitions
switch_definitions
binary_sensor_definitions
button_definitions
all_definitions
```

Cada definición usa claves canónicas e incluye la metadata consumida por su plataforma de Home Assistant, tales como unidad, clase de dispositivo, clase de estado, escala, precisión, ritmo de consulta, categoría y habilitación por defecto. Semilla definiciones durante `__init__`; `connect()` puede refinarlas después del descubrimiento de modelo o paquete. Una batería que comienza inaccesible aún necesita suficientes definiciones para que las entidades se suscriban y disparen una recuperación posterior.

Añade nombres y descripciones visibles a `custom_components/omnibattery/strings.json` y cada archivo bajo `custom_components/omnibattery/translations/`. Reutiliza una clave canónica existente y traducción cuando el significado y unidad sean idénticos.

## Conectar el controlador en la configuración

Un archivo de controlador por sí solo no es soporte seleccionable. Completa cada punto de integración:

1. Exporta la clase de `drivers/__init__.py` y añádela a `__all__`.
2. Añade la marca a los selectores añadir-batería y editar-batería en `config_flow.py`.
3. Añade un paso específico de flujo de configuración que valide credenciales o acceso al transporte en hardware real y almacene solo los campos necesarios en tiempo de ejecución.
4. Construye el controlador en `MarstekVenusDataUpdateCoordinator.__init__()` y pasa límites de modelo probados o identidad cuando sea necesario.
5. Establece indicadores de control y límite por software desde capacidades y comportamiento hardware real; no inferirlos solo desde el nombre de marca.
6. Expo solo definiciones de entidad soportadas, luego añade todas las claves de traducción.
7. Añade cualquier valor C configurado por el usuario, tal como capacidad nominal, con validación y etiquetas claras.
8. Comprobar la configuración mientras la batería es accesible e inaccesible, luego comprobar recarga, reconexión y eliminación.
9. Verificar el nuevo dispositivo en una flota de múltiples marcas para que selección, asignación, propiedad manual y apagado no dependan de controladores homogéneos.

Si la configuración requiere una biblioteca nueva, documenta por qué es necesaria, áncoral según política de repositorio e incluye licencia y información de mantenimiento en la solicitud de extracción.

## Probar el controlador

Crea `tests/test_<brand>_driver.py`. Usa un transporte falso, estados falsos de Home Assistant o una capa de servicio falsa para que las pruebas nunca contacten hardware real. Archivos existentes como `test_huawei_driver.py`, `test_zendure_driver.py` y `test_esphome_driver.py` muestran los acoples esperados.

### Pruebas de controlador

Cubre estos comportamientos:

- construcción y valores de capacidad completa;
- éxito de conexión, fallo de autenticación, conexión repetida, cierre y reconexión;
- composición de grupos de lectura y ritmo de consulta;
- decodificación, escalado, conversión de signo, sentinels, respuestas parciales, claves faltantes y lecturas filtradas por clave;
- expiración de caché de impulso y comprobaciones de vitalidad donde aplicable;
- setpoints positivos, negativos y cero;
- clampeo de comandos, potencia mínima fiable y límites específicos del modelo;
- transiciones carga-a-descarga, descarga-a-carga, activo-a-reposo y reposo-a-activo;
- orden de escritura y resultado seguro de cada fallo parcial;
- retroalimentación inmediata, retardada, ausente, obsoleta y no coincidente;
- campos `SetpointResult` y `net_power_from_data()`;
- `apply_config()`, `standby()`, cortafuegos, puertas de control externo y controles no compatibles;
- detección de modelo, variantes de firmware, filtrado de definición de entidad y dependencias de control;
- fórmulas de telemetría derivadas en límites de signo y rango;
- persistencia de energía sintética cuando contadores nativos faltan.

### Pruebas de integración

Añade o extiende pruebas para:

- construcción del coordinador y reenvío de capacidades;
- serialización y edición de flujo de configuración;
- definiciones de entidad y traducciones;
- configuración con batería inaccesible y recuperación posterior;
- selección de múltiples marcas y distribución de potencia;
- límites de SOC o potencia por software cuando el hardware no los impone;
- apagado alcanzando reposo y restaurando control del fabricante cuando aplicable;
- características opcionales desapareciendo limpiamente cuando su telemetría no es compatible.

Ejecuta primero la prueba enfocada, luego la suite completa de unidades:

```bash
python -m pytest tests/test_<brand>_driver.py
python -m pytest
```

Las pruebas que solicitan el fixture `hass` de Home Assistant necesitan habilitado el plugin de pytest de Home Assistant. Sigue el patrón de comando separado en `.github/workflows/tests.yml` y añade el nuevo archivo de prueba allí si la suite predeterminada lo salta:

```bash
python -m pytest -o addopts="" tests/test_<integration_flow>.py
```

Antes de abrir la solicitud de extracción, también construye la documentación exactamente como hace la integración continua:

```bash
python -m mkdocs build --strict
```

## Validar en hardware

Las pruebas de unidad demuestran lógica de traducción; no demuestran el comportamiento del fabricante. Registra una prueba de hardware para cada modelo y familia de firmware soportados:

- [ ] Conectar, leer identidad y SOC, y cerrar sin recursos filtrados.
- [ ] Recuperar tras un tiempo agotado, reinicio del dispositivo, recarga de Home Assistant y pérdida temporal de red.
- [ ] Rechazar respuestas malformadas, sentinels y valores fuera de rango.
- [ ] Confirmar signo físico de potencia durante carga, descarga y reposo.
- [ ] Confirmar rango de comando, paso, clamps y potencia mínima estable.
- [ ] Medir latencia de actuador y retroalimentación normal y en peor caso.
- [ ] Confirmar que comandos repetidos son idempotentes y no escriben flash persistente innecesariamente.
- [ ] Interrumpir cada etapa de una secuencia de multi-escritura y verificar el estado seguro documentado.
- [ ] Confirmar que escrituras fallidas no entran en la caché como estado confirmado.
- [ ] Ejercitar comportamiento de SOC mínimo y máximo con protecciones BMS aún activas.
- [ ] Detener Omnibattery y verificar `standby()` más cualquier restauración de control del fabricante.
- [ ] Ejecutar en una piscina de múltiples marcas y verificar que la entrega medida coincide con la asignación.

Mantén registros o trazas borrados que muestren el comando solicitado, confirmación y respuesta física medida. Estado claramente qué entradas de matriz permanecen sin probar.

## Qué incluir en la solicitud de extracción

Un revisor debería poder juzgar el protocolo, comportamiento de seguridad, superficie del producto y evidencia de prueba sin reconstruir tu investigación. Incluye:

- fabricantes soportados, modelos, regiones y firmware probados;
- fuente oficial de protocolo y estado de autorización;
- transporte, autenticación, descubrimiento y comportamiento offline;
- el mapeo de telemetría y control, incluyendo signo, escalado, unidades, sentinels y persistencia;
- cada valor `DriverCapabilities` con su evidencia o justificación;
- secuencia de comando, clampeo, comportamiento de fallo parcial y ruta de reposo seguro;
- latencia medida de actuador y retroalimentación;
- características no compatibles y cómo están bloqueadas;
- todos los archivos añadidos o cambiados a través de exportación del controlador, coordinador, flujo de configuración, entidades, traducciones, pruebas y documentación de usuario;
- comandos enfocados y completos de prueba con resultados;
- evidencia de prueba de hardware borrada y modelos y firmware exactos probados;
- limitaciones conocidas, riesgos restantes y trabajo de seguimiento explícito.

No afirmar soporte basado solo en pruebas simuladas. La marca debe ser seleccionable, la configuración debe completarse, las entidades soportadas deben poblar el control automático debe alcanzar hardware real de forma segura, y la matriz de prueba documentada debe identificar qué fue verificada físicamente.

??? "Fichas de evaluación de protocolo"
    Usa estas tablas en la incidencia o solicitud de extracción cuando el mapeo sea demasiado grande para el resumen.

    **Transporte y acceso**

    | Aspecto | Valor |
    |---|---|
    | Local, nube o ambos | `...` |
    | Protocolo y versión | `...` |
    | Dirección, punto final, unidad o tema | `...` |
    | Método de descubrimiento | `...` |
    | Autenticación y renovación | `...` |
    | Encriptación y validación de certificado | `...` |
    | Tiempo agotado y política de reintento | `...` |
    | Límite de conexión simultánea | `...` |
    | Límite de velocidad de lectura/escritura | `...` |
    | Orden o atomicidad de multi-escritura | `...` |
    | Marca de tiempo de telemetría, secuencia o TTL | `...` |
    | Comandos volátiles versus persistentes | `...` |
    | Comportamiento offline | `...` |

    **Mapeo de telemetría**

    | Clave Omnibattery | B/R/O | Campo del fabricante | R/E | Tipo/orden | Escala y unidad | Rango/sentinels | Ritmo/TTL | N/D/C/X | Evidencia | Probado |
    |---|---|---|---|---|---|---|---|---|---|---|---|
    | `battery_soc` | B | `...` | E | `...` | `... → %` | `...` | `...` | `...` | `...` | [ ] |
    | `battery_power` | B | `...` | E | `...` | `... → W` | `...` | `...` | `...` | `...` | [ ] |
    | Eco de setpoint | R | `...` | E | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
    | `battery_total_energy` | R | `...` | R/C | `...` | `... → kWh` | `...` | `...` | `...` | `...` | [ ] |
    | Totales de energía | O | `...` | E | `...` | `... → kWh` | `...` | `...` | `...` | `...` | [ ] |
    | Voltajes de celda | O | `...` | E | `...` | `... → V` | `...` | `...` | `...` | `...` | [ ] |
    | `internal_temperature` | O | `...` | E | `...` | `... → °C` | `...` | `...` | `...` | `...` | [ ] |
    | `inverter_state` | O | `...` | E | enum | `map: ...` | `...` | `...` | `...` | `...` | [ ] |
    | `ac_offgrid_power` | O | `...` | E | `...` | `... → W` | `...` | `...` | `...` | `...` | [ ] |
    | Alarmas o fallos | O | `...` | E | bitmap/enum | `map: ...` | `...` | `...` | `...` | `...` | [ ] |
    | Solar o MPPT | O | `...` | E | `...` | `... → W` | `...` | `...` | `...` | `...` | [ ] |
    | Identidad y firmware | O | `...` | E | string | `...` | `...` | `...` | `...` | `...` | [ ] |

    **Mapeo de control**

    | Operación | B/R/O | Comando del fabricante | Secuencia | Rango/paso | Volátil/persistente | Confirmación/lectura | Latencia | Estado seguro de fallo | Evidencia | Probado |
    |---|---|---|---|---|---|---|---|---|---|---|---|
    | Conectar/autenticar | B | `...` | `...` | — | — | `...` | `...` | sin control | `...` | [ ] |
    | Carga | B | `...` | `...` | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
    | Descarga | B | `...` | `...` | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
    | Reposo | B | `...` | `...` | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
    | Máximos/mínimos de potencia | R | `...` | `...` | `...` | `...` | `...` | `...` | Límite C | `...` | [ ] |
    | Cortafuegos de SOC | O | `...` | `...` | `...` | `...` | `...` | `...` | límite software | `...` | [ ] |
    | Habilitar control externo | Condicional | `...` | `...` | `...` | `...` | `...` | `...` | restaurar control | `...` | [ ] |
    | Restaurar control del fabricante | Condicional | `...` | `...` | — | `...` | `...` | `...` | `...` | `...` | [ ] |
    | Otros controles de entidad | O | `...` | `...` | `...` | `...` | `...` | `...` | omitir entidad | `...` | [ ] |

??? "Ejemplos de adaptación existentes"
    Marstek Venus E v3 demuestra una implementación respaldada por registros. Lee `battery_soc` del registro `37005` y `battery_power` firmada del registro `30001`. La carga escribe un límite de carga y modo de carga forzado; la descarga escribe un límite de descarga y modo de descarga forzado; el reposo escribe ambos setpoints direccionales a cero y selecciona ninguna dirección forzada. Los setpoints direccionales exponen un rango de `0–2,500 W` y paso de `50 W`. El controlador declara una potencia operativa mínima fiable de `0 W` porque los registros de comando aceptan valores por debajo de las opciones separadas del tope de potencia de hardware.

    Zendure demuestra sustituciones válidas para un dispositivo basado en propiedades:

    | Diferencia del fabricante | Adaptación del controlador |
    |---|---|
    | Sin `battery_power` directo | Derivar potencia de paquete de salida menos potencia de entrada del paquete tras validar signo y simultaneidad. |
    | Sin contadores de energía nativos | Integrar potencia medida y persistir totales sintéticos. |
    | Sin capacidad nominal | Requerir `battery_total_energy` configurado por el usuario. |
    | Sin modo de fuerza Marstek | Traducir potencia neta en modo del fabricante y límites de entrada/salida. |
    | Tope de carga de hardware solo lectura | Combinar tope del dispositivo con un techo de software del usuario. |
    | Celdas reportadas por paquete | Derivar extremos globales y exponer claves específicas de paquete solo cuando sea útil. |
    | Retroalimentación retardada | Declarar confirmación inmediata no fiable y tiempo conservador. |
    | Preocupación de escritura persistente | Usar setpoints volátiles y reservar escrituras persistentes para cambios de configuración explícitos. |

    Acepta una sustitución solo tras validar su signo, tiempo, rango, persistencia y comportamiento de fallo en hardware. Los valores configurados deben permanecer como valores visibles configurados; no presentarlos como telemetría del dispositivo.