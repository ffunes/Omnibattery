# Plantilla de requisitos para integrar un driver de batería

Esta plantilla sirve para auditar la documentación oficial de una batería antes
de desarrollar su driver para Omnibattery. La referencia funcional es **Marstek
Venus E v3**, pero no se exige que otra marca copie sus registros ni sus modos.
Lo que debe conservarse es el contrato semántico de Omnibattery: leer el estado
real de la batería y ordenar una potencia neta segura.

La plantilla debe completarse por combinación de **marca, modelo y familia de
firmware**. Una API documentada para un modelo no se debe dar por válida para
toda la gama.

## Resultado de la evaluación

Usa estas clasificaciones:

| Código | Significado |
|---|---|
| **B** | Bloqueante. Sin esta capacidad no debe habilitarse el control automático. |
| **R** | Requerida para una integración robusta. Puede admitirse provisionalmente si el riesgo está documentado y mitigado. |
| **O** | Opcional. Su ausencia elimina entidades o funcionalidades concretas, pero no el control básico. |

Para el origen de cada dato o control:

| Código | Origen |
|---|---|
| **N** | Nativo: la batería lo expone directamente. |
| **D** | Derivado: el driver lo calcula a partir de datos nativos. |
| **C** | Configurado: lo aporta el usuario o una constante validada por modelo. |
| **X** | No soportado: la entidad o funcionalidad queda fuera. |

Dictamen final:

- **APTO**: están cubiertos todos los requisitos B y R.
- **APTO CON LIMITACIONES**: están cubiertos todos los B, pero falta algún R u
  O. Deben enumerarse las funciones desactivadas y los riesgos residuales.
- **NO APTO**: falta al menos un B, la documentación no permite confirmar la
  semántica de los comandos o el control depende de una interfaz inestable/no
  autorizada.

## 1. Identificación y evidencia documental

| Campo | Valor a completar |
|---|---|
| Fabricante | `...` |
| Modelo comercial | `...` |
| Identificador devuelto por el equipo | `...` |
| Firmware mínimo/máximo verificado | `...` |
| Región o variante de hardware | `...` |
| Capacidad y potencia nominales | `...` |
| Tipo de acoplamiento | `AC / DC / híbrido` |
| Documento oficial, versión y fecha | `...` |
| URL o fichero archivado | `...` |
| Contacto/canal de soporte del fabricante | `...` |
| Equipo real usado para validar | `...` |
| Fecha de la prueba | `...` |

Documentar también:

- [ ] La interfaz está publicada o autorizada por el fabricante.
- [ ] Se conocen los modelos y firmwares a los que aplica.
- [ ] Se han guardado ejemplos reales de petición y respuesta, sin secretos.
- [ ] Cada campo tiene tipo, unidad, escala, signo, rango y valor centinela.
- [ ] Cada escritura tiene rango, granularidad, persistencia y respuesta de error.
- [ ] Se conocen límites de frecuencia, concurrencia y tamaño de petición.
- [ ] Se conoce qué ocurre al reiniciar, perder la conexión o cerrar Omnibattery.

### Matriz de compatibilidad de firmware

| Modelo | Firmware | Transporte | Lectura | Escritura | Diferencias conocidas | Estado |
|---|---|---|---|---|---|---|
| `...` | `...` | `...` | `sí/no` | `sí/no` | `...` | `probado/no probado` |

### Ficha de transporte y acceso

| Aspecto | Valor a completar |
|---|---|
| Alcance | `local / cloud / ambos` |
| Protocolo y versión | `...` |
| Dirección, puerto, endpoint o topic | `...` |
| Descubrimiento | `manual / mDNS / broadcast / cloud / ...` |
| Autenticación y renovación | `...` |
| Cifrado/TLS y validación de certificado | `...` |
| Identificador de unidad/dispositivo | `...` |
| Timeout y reintentos recomendados | `...` |
| Máximo de conexiones simultáneas | `...` |
| Límite de lecturas/escrituras | `...` |
| Orden o atomicidad de escrituras múltiples | `...` |
| Timestamp, secuencia o TTL de telemetría | `...` |
| Comandos volátiles frente a persistentes | `...` |
| Comportamiento sin red/cloud | `...` |

Una API exclusivamente cloud no es un rechazo automático, pero su latencia,
caducidad de token, cuotas y comportamiento durante una caída deben permitir un
reposo seguro y una cadencia de control estable. Estas condiciones forman parte
del dictamen, no son meros detalles de implementación.

## 2. Puerta de entrada: mínimos para control automático

Todos los puntos siguientes son bloqueantes:

- [ ] Existe un transporte programático con conexión, reconexión y cierre
  controlables (`Modbus TCP/RTU`, HTTP local, MQTT, API equivalente).
- [ ] Puede leerse un **SOC real** y actualizado en porcentaje.
- [ ] Puede obtenerse la **potencia real de batería**, directa o derivada de
  medidas simultáneas, con la convención de signo de Omnibattery.
- [ ] Puede ordenarse una **carga** a potencia limitada.
- [ ] Puede ordenarse una **descarga** a potencia limitada.
- [ ] Puede ordenarse y mantener un estado de **reposo** (`0 W`) seguro.
- [ ] Se conocen los límites máximos seguros de carga y descarga por unidad.
- [ ] La protección BMS independiente del fabricante continúa activa bajo
  control externo. Omnibattery no sustituye protecciones eléctricas del BMS.
- [ ] La cadencia de escritura necesaria no desgasta memoria flash ni incumple
  límites de la API. Si hay comandos volátiles y persistentes, están distinguidos.
- [ ] Se puede detectar una comunicación obsoleta o perdida sin reutilizar datos
  antiguos indefinidamente.

Si falta SOC, potencia medida, una de las dos direcciones o un reposo fiable, el
driver es **NO APTO** para el bucle automático bidireccional. Se puede estudiar
un modo de solo monitorización, pero no debe presentarse como soporte completo.

## 3. Contrato canónico de Omnibattery

El driver traduce el protocolo de la marca al contrato de
`drivers/base.py::BatteryDriver`. La capa de control nunca debe conocer
direcciones de registro, endpoints, topics ni nombres propietarios.

### Convenciones obligatorias

- Potencia neta: `+W` significa **carga**, `-W` significa **descarga** y `0 W`
  significa reposo.
- `battery_power` usa la misma convención y representa potencia **medida**, no
  solamente el último setpoint solicitado.
- Potencia en `W`, energía/capacidad en `kWh`, SOC en `%`, tensión en `V` y
  temperatura en `°C` después de aplicar escala.
- Un valor fallido se omite o se entrega como desconocido; nunca se inventa `0`
  si cero es una medida válida.
- `apply_setpoint()` limita el valor al sobre de potencia del equipo y devuelve
  un `SetpointResult` coherente aunque el protocolo no tenga readback inmediato.

### Superficie mínima del driver

| Superficie | Requisito | Nivel |
|---|---|---|
| Identidad/capacidades | `capabilities`, `model_label` y, si existe, `serial` estable | R |
| Ciclo de vida | `connected`, `connect()`, `close()`, `set_shutting_down()` | B |
| Lectura | `read_groups` y `read_telemetry(keys)` con caché si la fuente es push | B |
| Control neto | `apply_setpoint(+W/-W/0)` | B |
| Controles de entidad | `write_control(key, value)`; devuelve `False` para claves no soportadas | R |
| Eco de orden | `net_power_from_data(data)`; puede devolver `None` si no hay eco | R |
| Dependencias | `control_dependency_keys` para datos que deben leerse aunque su entidad esté deshabilitada | R |
| Configuración | `apply_config(...)`, omitiendo de forma explícita los ajustes no aplicables | R |
| Parada | `standby()` que deje el equipo en un estado seguro antes de cerrar | B |
| Corte de carga | `set_charge_cutoff()` o retorno controlado `False` cuando se aplique por software | O/condicional |
| Puerta externa | `set_rs485_control()`/`get_rs485_control()` o equivalente si el equipo la necesita | B/condicional |

Aunque algunos métodos semánticos todavía no estén declarados abstractos en la
clase base, el coordinador los usa y el nuevo driver debe implementarlos.

### Capacidades a declarar

| `DriverCapabilities` | Valor | Evidencia/justificación |
|---|---:|---|
| `hardware_soc_cutoff` | `...` | `...` |
| `has_force_mode` | `...` | `...` |
| `push_telemetry` | `...` | `...` |
| `max_charge_power_w` | `...` | `...` |
| `max_discharge_power_w` | `...` | `...` |
| `min_charge_power_w` | `...` | `...` |
| `min_discharge_power_w` | `...` | `...` |
| `has_mppt_pv` | `...` | `...` |
| `has_alarm_registers` | `...` | `...` |
| `has_rs485_control` | `...` | `...` |
| `has_energy_counters` | `...` | `...` |
| `setpoint_confirm_reliable` | `...` | `...` |
| `actuator_latency_s` | `...` | Medida de peor caso, no promedio |

## 4. Mapa mínimo de palancas y telemetría

### Núcleo obligatorio

| Clave/operación Omnibattery | Nivel | Qué debe aportar la marca | Sustitución admitida |
|---|---|---|---|
| `battery_soc` | B | SOC real 0–100 %, con cadencia y antigüedad conocidas | No se acepta una estimación simple por tensión como soporte completo |
| `battery_power` | B | Potencia instantánea real en ambos sentidos | Fórmula D a partir de flujos simultáneos y validados |
| `apply_setpoint(+W)` | B | Orden de carga con límite de potencia | Combinación de modo + límite o propiedad única |
| `apply_setpoint(-W)` | B | Orden de descarga con límite de potencia | Combinación de modo + límite o propiedad única |
| `apply_setpoint(0)` / `standby()` | B | Reposo mantenido, sin quedar en un modo autónomo que exporte | Secuencia documentada de modo y límites a cero |
| Límites máx. de potencia | B | Valores por modelo o lectura del equipo | Configuración C limitada por máximos oficiales |
| Estado de conexión/frescura | B | Error, timestamp, disponibilidad o mecanismo equivalente | Temporizador de caducidad en el driver |
| Eco del setpoint | R | Modo/límite aplicado o aceptado | Caché de orden solo para optimizar; no sustituye potencia medida |
| Latencia de actuador | R | Tiempo escritura → aplicación → reflejo en telemetría | Medición sobre equipo real y margen conservador |
| Potencia mínima fiable | R | Mínimo no nulo sostenible y pasos aceptados | Constante C por modelo, validada físicamente |

### Telemetría y controles que amplían funciones

| Clave canónica | Nivel | Función que habilita | Si falta |
|---|---|---|---|
| `battery_total_energy` | R | Energía almacenada, reparto y carga predictiva | Capacidad C introducida por el usuario |
| `total_charging_energy` / `total_discharging_energy` | O | Energía y eficiencia acumuladas | Integración D de `battery_power`, con persistencia |
| `max_cell_voltage` | O | Taper y pausa segura al 100 %, recalibración y balance | Desactivar las funciones dependientes de tensión de celda |
| `min_cell_voltage` | O | Delta de celdas y monitor de balance | No publicar delta/balance |
| `internal_temperature` | O | Derating térmico de carga/descarga | No habilitar el límite térmico |
| `inverter_state` | O | Confirmación adicional de standby/corte BMS | Usar solo potencia medida; omitir detecciones que exijan estado |
| `ac_offgrid_power` | O | Detectar carga de backup y excluir la batería del PD | Desactivar exclusión automática por backup |
| `backup_function` o equivalente | O | Saber/controlar el modo backup | Omitir la entidad y su lógica específica |
| `mppt1_power`…`mppt4_power` | O | Producción DC y eficiencia por plano | `has_mppt_pv=False`; no crear esas entidades |
| `alarm_status` / `fault_status` | O | Alarmas de sistema | No crear el sensor/notificador dependiente |
| `battery_voltage` | O | Diagnóstico | Omitir la entidad |
| Identidad, firmware, RSSI | O | Diagnóstico y soporte | Omitir entidades; preferir `serial` estable si existe |
| Corte SOC hardware | O | Persistencia autónoma de límites SOC | `hardware_soc_cutoff=False` y corte por software |
| Límite potencia escribible | O | Ajuste persistente en el equipo | Límite C en software, sin fingir una entidad hardware |
| Puerta de control externo | Condicional | Activar/devolver el control al firmware | Solo obligatoria si los setpoints no funcionan sin ella |

## 5. Referencia: Marstek Venus E v3

La v3 define el comportamiento de referencia, no la forma obligatoria del
protocolo:

| Semántica | Implementación v3 de referencia |
|---|---|
| SOC | `battery_soc`, registro `37005`, `uint16`, `%` |
| Potencia medida | `battery_power`, registro `30001`, `int16`; positiva al cargar y negativa al descargar |
| Orden de carga | `set_discharge_power=0`, `set_charge_power=W`, `force_mode=charge` |
| Orden de descarga | `set_discharge_power=W`, `set_charge_power=0`, `force_mode=discharge` |
| Reposo | Ambos setpoints a `0` y `force_mode=stop` |
| Sobre de potencia | Setpoints `0–2500 W`, paso documentado `50 W`; límites de instalación en `max_charge_power`/`max_discharge_power` |
| Potencia mínima declarada | `800 W` para los límites v3; se refleja en las capacidades del driver |
| Control externo | `rs485_control_mode`, comandos específicos `0x55AA`/`0x55BB` |
| Corte SOC | No hay registros de corte en v3; Omnibattery aplica min/max SOC por software |
| Confirmación | Readback de modo, setpoints y potencia; tolerancia durante la rampa |
| Transporte | Modbus, polling, un único slot TCP y pacing específico |

La consecuencia importante es que **`force_mode` no es obligatorio**. Otra
batería puede cumplir exactamente el mismo contrato mediante un único límite
con signo, dos límites, un enum distinto o una API HTTP.

## 6. Sustituciones válidas: patrón Zendure

Zendure demuestra qué ausencias se pueden resolver dentro de Omnibattery:

| Ausencia/diferencia de la marca | Adaptación válida del driver |
|---|---|
| No existe `battery_power` directo | Se deriva como `outputPackPower - packInputPower`, tras validar signo y simultaneidad |
| No hay contadores kWh | Omnibattery integra `battery_power` y persiste los totales sintéticos |
| No se informa capacidad nominal | El usuario configura `battery_total_energy` en kWh |
| No existe `force_mode` Marstek | `acMode` + `inputLimit`/`outputLimit` implementan el setpoint neto |
| El límite de carga es de solo lectura | Se combina el máximo real del equipo con un techo de software del usuario |
| Las celdas llegan como una lista de packs | El driver calcula extremos globales y publica también claves por pack |
| El readback tarda varios segundos | Se declara `setpoint_confirm_reliable=False` y una `actuator_latency_s` conservadora |
| La escritura frecuente podría tocar flash | Los setpoints usan modo volátil; la configuración persistente usa escritura explícita a flash |

Reglas para aceptar una sustitución:

- [ ] La fórmula y la convención de signo están probadas con carga, descarga y reposo.
- [ ] Las entradas de una fórmula corresponden al mismo instante o su desfase es acotado.
- [ ] El dato derivado conserva unidad, rango y precisión suficientes.
- [ ] Los acumuladores se restauran tras reinicio y toleran huecos de telemetría.
- [ ] Un valor C queda identificado como configuración, no como lectura del equipo.
- [ ] La UI no crea una entidad hardware que en realidad no existe.

No deben sintetizarse sin una fuente física fiable: SOC real, alarmas, temperatura,
tensiones de celda ni confirmación de potencia entregada. Una caché del último
comando representa **intención**, no estado real.

## 7. Matriz de degradación funcional

Completarla antes de aprobar el desarrollo:

| Funcionalidad | Dependencias mínimas | Alternativa | Estado para este modelo |
|---|---|---|---|
| Control PD carga/descarga | SOC, potencia medida, setpoint ±W/0, límites | Ninguna para soporte completo | `...` |
| Gestión multi-batería | Lo anterior por unidad; capacidad mejora el reparto energético | Capacidad C | `...` |
| Límites min/max SOC | SOC + mando de reposo | Hardware o software | `...` |
| Carga predictiva/precios | SOC + capacidad kWh + control de carga | Capacidad C | `...` |
| Energía/ciclos/eficiencia | Contadores o potencia con tiempo fiable | Integración D | `...` |
| Taper/protección al 100 % | `max_cell_voltage`, SOC y potencia | Sin alternativa equivalente | `...` |
| Monitor/diagnóstico de balance | `max_cell_voltage` + `min_cell_voltage` | Extremos D desde celdas/packs | `...` |
| Carga semanal completa | SOC, potencia y control; corte hardware si existe | Corte software | `...` |
| Límite térmico | Temperatura interna | Ninguna | `...` |
| Exclusión por backup | Potencia off-grid y estado/modo backup | Ninguna fiable | `...` |
| Producción MPPT/DC | Potencia por MPPT o total DC | Suma D de canales | `...` |
| Alarmas | Bits/códigos de fallo con tabla oficial | Ninguna | `...` |
| Persistencia de energía sintética | Potencia + identificador estable | Clave de dispositivo menos estable | `...` |

Una función marcada como no soportada debe quedar fuera mediante capacidades,
definiciones de entidades o configuración. No debe recibir ceros ficticios.

## 8. Ficha de mapeo de telemetría

Añadir una fila por cada clave. En “evidencia” indicar página/sección del manual
y adjuntar una muestra real anonimizada.

| Clave Omnibattery | B/R/O | Campo/registro/topic fabricante | R/W | Tipo/endian | Escala y unidad final | Rango/centinelas | Cadencia/TTL | N/D/C/X | Evidencia | Validado |
|---|---|---|---|---|---|---|---|---|---|---|
| `battery_soc` | B | `...` | R | `...` | `... → %` | `...` | `...` | `...` | `...` | [ ] |
| `battery_power` | B | `...` | R | `...` | `... → W; +carga/-descarga` | `...` | `...` | `...` | `...` | [ ] |
| Estado/eco de setpoint | R | `...` | R | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
| `battery_total_energy` | R | `...` | R/C | `...` | `... → kWh` | `...` | `...` | `...` | `...` | [ ] |
| `total_charging_energy` | O | `...` | R | `...` | `... → kWh` | `...` | `...` | `...` | `...` | [ ] |
| `total_discharging_energy` | O | `...` | R | `...` | `... → kWh` | `...` | `...` | `...` | `...` | [ ] |
| `max_cell_voltage` | O | `...` | R | `...` | `... → V` | `...` | `...` | `...` | `...` | [ ] |
| `min_cell_voltage` | O | `...` | R | `...` | `... → V` | `...` | `...` | `...` | `...` | [ ] |
| `internal_temperature` | O | `...` | R | `...` | `... → °C` | `...` | `...` | `...` | `...` | [ ] |
| `inverter_state` | O | `...` | R | enum | `mapa: ...` | `...` | `...` | `...` | `...` | [ ] |
| `ac_offgrid_power` | O | `...` | R | `...` | `... → W` | `...` | `...` | `...` | `...` | [ ] |
| Alarmas/fallos | O | `...` | R | bitmap/enum | `mapa: ...` | `...` | `...` | `...` | `...` | [ ] |
| MPPT/PV | O | `...` | R | `...` | `... → W` | `...` | `...` | `...` | `...` | [ ] |
| Identidad/firmware | O | `...` | R | string | `...` | `...` | `...` | `...` | `...` | [ ] |

## 9. Ficha de mapeo de controles

| Operación semántica | B/R/O | Campo(s)/comando(s) fabricante | Secuencia | Rango/paso | Volátil/persistente | ACK/readback | Timeout/latencia | Estado seguro al fallar | Evidencia | Validado |
|---|---|---|---|---|---|---|---|---|---|---|
| Conectar/autenticar | B | `...` | `...` | — | — | `...` | `...` | sin control | `...` | [ ] |
| Cargar a `W` | B | `...` | `...` | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
| Descargar a `W` | B | `...` | `...` | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
| Reposo `0 W` | B | `...` | `...` | `...` | `...` | `...` | `...` | `...` | `...` | [ ] |
| Límite máximo carga | R | `...` | `...` | `...` | `...` | `...` | `...` | límite C | `...` | [ ] |
| Límite máximo descarga | R | `...` | `...` | `...` | `...` | `...` | `...` | límite C | `...` | [ ] |
| Corte SOC máximo | O | `...` | `...` | `...` | `...` | `...` | `...` | software | `...` | [ ] |
| Corte SOC mínimo | O | `...` | `...` | `...` | `...` | `...` | `...` | software | `...` | [ ] |
| Activar control externo | Cond. | `...` | `...` | `...` | `...` | `...` | `...` | devolver control | `...` | [ ] |
| Restablecer control del fabricante | Cond. | `...` | `...` | — | `...` | `...` | `...` | `...` | `...` | [ ] |
| Otros controles UI | O | `...` | `...` | `...` | `...` | `...` | `...` | omitir entidad | `...` | [ ] |

## 10. Pruebas mínimas de aceptación

No basta con que el documento mencione una clave; debe verificarse sobre equipo
real o simulador oficial representativo.

### Transporte y datos

- [ ] Conecta, lee identidad/SOC y cierra sin dejar recursos o sesiones abiertos.
- [ ] Reconecta después de timeout, reinicio del equipo y cambio temporal de red.
- [ ] Rechaza respuestas parciales, valores centinela y datos fuera de rango.
- [ ] Caduca la caché push o los últimos datos cuando deja de recibir mensajes.
- [ ] Mantiene las unidades y el signo en carga, descarga y reposo.
- [ ] Respeta exclusión mutua si el dispositivo solo admite una conexión.

### Control

- [ ] `+W`: carga, queda limitada al máximo y la potencia medida cambia de signo correcto.
- [ ] `-W`: descarga, queda limitada al máximo y la potencia medida cambia de signo correcto.
- [ ] `0 W`: detiene ambos sentidos y no vuelve solo a exportar/importar.
- [ ] Transiciones carga → descarga, descarga → carga y movimiento → reposo.
- [ ] Comandos repetidos son idempotentes y no desgastan flash.
- [ ] Si una orden usa varias escrituras, un fallo parcial converge a reposo o a
  otro estado definido; se ha verificado el orden obligatorio.
- [ ] El readback distingue orden aceptada de potencia realmente entregada.
- [ ] Se mide latencia en caso normal y peor caso; se configura el valor conservador.
- [ ] Una escritura fallida devuelve razón y no actualiza la caché como confirmada.
- [ ] Al descargar con SOC mínimo y cargar con SOC máximo, el sistema queda seguro.
- [ ] Al cerrar Omnibattery se aplica `standby()` y, si procede, se devuelve el control al firmware.

### Sustituciones y degradación

- [ ] Cada fórmula D tiene pruebas unitarias con valores límite y signos.
- [ ] Energía sintética sobrevive a reinicios y no integra durante huecos de datos.
- [ ] La capacidad C se valida contra rangos razonables y aparece como configurada.
- [ ] Las funciones X no crean entidades, avisos ni decisiones con valores ficticios.
- [ ] El driver convive con otra marca en un pool multi-batería.

### Cobertura de código esperada

- [ ] Contrato del driver: conexión, grupos de lectura, escalado y claves ausentes.
- [ ] `apply_setpoint`: carga, descarga, cero, clamp, fallo, ACK tardío y sin readback.
- [ ] `net_power_from_data` y `control_dependency_keys`.
- [ ] Configuración/cortes/standby y puerta de control condicional.
- [ ] Detección de modelo y validación del flujo de configuración.
- [ ] Matriz de firmware/modelo soportado y no soportado.

## 11. Informe de decisión para copiar y completar

```text
Marca/modelo:
Firmware probado:
Documentación oficial (versión/fecha/enlace):

Dictamen: APTO / APTO CON LIMITACIONES / NO APTO

Bloqueantes B:
- SOC real: N/D/C/X — evidencia:
- Potencia real: N/D/C/X — evidencia/fórmula:
- Carga regulable: sí/no — rango/paso:
- Descarga regulable: sí/no — rango/paso:
- Reposo seguro: sí/no — secuencia:
- Límites seguros: origen/valores:
- Frescura y pérdida de conexión: mecanismo:

Adaptaciones en Omnibattery:
- Datos derivados:
- Datos configurados por usuario:
- Límites aplicados por software:

Funcionalidades excluidas:
-

Riesgos abiertos:
-

Pruebas en hardware pendientes:
-

Responsable de aprobación y fecha:
```

## 12. Checklist de implementación después de aprobar

- [ ] Crear el driver sin filtrar detalles propietarios fuera de `drivers/`.
- [ ] Añadir selección/detección de marca y modelo en el flujo de configuración.
- [ ] Instanciar el driver en el coordinador y declarar correctamente capacidades.
- [ ] Definir solo las entidades realmente soportadas y sus traducciones.
- [ ] Añadir campos de configuración para valores C, como capacidad nominal.
- [ ] Desactivar por capacidad las funciones que dependan de claves X.
- [ ] Añadir pruebas unitarias del driver y pruebas de integración multi-marca.
- [ ] Documentar prerrequisitos del equipo, firmwares y limitaciones conocidas.
- [ ] Actualizar diagnósticos ocultando credenciales, tokens y números de serie sensibles.

La aprobación documental autoriza iniciar el driver, pero no sustituye la prueba
en hardware. Una clave que aparece en el manual puede tener signo invertido,
escala distinta, latencia, clamp interno o comportamiento diferente según
firmware; todo ello debe quedar validado antes de declarar soporte estable.
