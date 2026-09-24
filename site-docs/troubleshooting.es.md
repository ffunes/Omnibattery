# Solución de problemas por síntoma

Empieza por lo que puedes ver en Home Assistant. **Integration Status** y sus atributos de bloqueadores suelen explicar por qué Omnibattery espera, limita potencia o excluye una batería.

!!! note "Compatibilidad con la aplicación Marstek"
    No necesitas cambiar nada en la aplicación Marstek para que Omnibattery funcione, incluido el ajuste de su medidor de energía. Cuando Omnibattery esté funcionando, no cambies el modo de funcionamiento ni ningún ajuste desde la aplicación Marstek: hacerlo rompe la compatibilidad hasta que desactives y vuelvas a activar la integración.

## La batería no hace nada

La batería puede estar en reposo intencionadamente cuando la red ya está cerca del objetivo. Si cambia la carga de casa y la batería sigue sin cargar ni descargar, usa esta tabla.

| Causa probable | Qué comprobar | Acción |
|---|---|---|
| El control automático está pausado | **Manual Mode** y **Manual Battery Control** por batería | Desactiva el control manual cuando quieras control automático |
| La batería no es apta | **Allow Charge**, **Allow Discharge** y `battery_charge_blockers` / `battery_discharge_blockers` en **Integration Status** | Activa el sentido requerido o elimina el bloqueador indicado |
| La batería no está disponible o está excluida | Entidades de batería, **Non-Responsive Batteries** y **Repairs** de Home Assistant | Sigue [Las entidades no están disponibles](#las-entidades-no-están-disponibles) |
| Hay una salida de respaldo activa | **Backup Function**, potencia de respaldo y `backup_cooldown_batteries` | Deja que termine la actividad de respaldo antes de esperar control de red |
| El medidor de red no es válido | Estado y hora de actualización del sensor de red principal | Sigue [El medidor de red no está disponible o está bloqueado](#el-medidor-de-red-no-está-disponible-o-está-bloqueado) |

**Resultado esperado:** **Integration Status** cambia de un estado bloqueado/manual a carga, descarga o reposo cuando cambia el flujo de red. Consulta [varias baterías](features/multi-battery.md) para el control por batería.

## La batería no carga

| Causa probable | Qué comprobar | Acción |
|---|---|---|
| Se alcanzó el SOC máximo o la histéresis de carga | `battery_charge_blockers`, SOC de corte/objetivo de carga y **Charge Hysteresis** | Reduce el objetivo solo si es intencionado; de lo contrario espera a que baje el SOC |
| La carga está desactivada para esta batería | **Allow Charge** | Actívalo |
| Charge Delay espera al sol | **Charge Delay** y **Charge Delay Status** | Revisa el [retraso de carga solar](features/solar-charge-delay.md) |
| Una franja horaria bloquea la carga | **Discharge Window**, interruptores de franja horaria y `charge_blockers` | Revisa las [franjas horarias](configuration/time-slots.md) |
| La carga predictiva no encontró déficit | **Predictive Charging Active**, su motivo, previsión y estimación de consumo | Es lo esperado; revisa la [carga predictiva](configuration/predictive-charging/index.md) |
| La temperatura o la protección de batería limitan la carga | **Integration Status**, estado de temperatura, alarma de batería y entidades de fallo | Revisa el [límite de carga por temperatura](features/temperature-charge-limit.md) y el manual de la batería |

**Resultado esperado:** el bloqueador desaparece y la potencia de carga sube cuando el excedente solar, una solicitud manual o un periodo predictivo apto requieren carga.

## La batería no descarga

| Causa probable | Qué comprobar | Acción |
|---|---|---|
| Se alcanzó el SOC mínimo | `battery_discharge_blockers` y el SOC mínimo de batería | Espera a cargar o ajusta el límite deliberadamente |
| La descarga está desactivada | **Allow Discharge** | Actívalo |
| La franja horaria actual bloquea la descarga | **Discharge Window** e interruptores de franja horaria | Revisa las [franjas horarias](configuration/time-slots.md) |
| El control de precio o reserva retiene energía | **Integration Status**, **Price-Based Discharge**, **Discharge Reserve** y estados relacionados | Revisa [Precio dinámico](configuration/predictive-charging/dynamic-pricing.md) |
| La protección de capacidad o lógica de carga excluida posee la respuesta | **Capacity Protection**, dispositivos excluidos activos y bloqueadores | Revisa [protección de capacidad](features/peak-shaving.md) y [exclusión de carga](features/load-exclusion.md) |
| La batería no está disponible o está excluida | **Non-Responsive Batteries** y **Repairs** de Home Assistant | Sigue [Las entidades no están disponibles](#las-entidades-no-están-disponibles) |

**Resultado esperado:** la descarga se reanuda cuando existe demanda doméstica y ninguna regla de seguridad, horario, precio o participación la bloquea.

## Importa o exporta más de lo esperado

| Causa probable | Qué comprobar | Acción |
|---|---|---|
| El signo del medidor de red está invertido | Compara el sensor de red configurado con el contador de la compañía mientras importas | Corrige **Invert grid meter** en la [configuración del sensor principal](configuration/main-sensor.md) |
| El objetivo de red no es cero intencionadamente | **PD Target Grid Power** | Establece el objetivo que corresponda con tu importación o exportación prevista |
| Las actualizaciones del medidor llegan tarde | `last_updated` del sensor de red y **PD Control Quality** | Usa un medidor local más rápido o ajusta el controlador tras corregir la latencia |
| Se excluye una carga grande | Estado del dispositivo excluido y sus controles de exclusión | Revisa la [exclusión de carga](features/load-exclusion.md) |
| La protección de fase limita una o varias baterías | **Three-Phase Protection Status** y asignación de fase | Revisa la [protección trifásica](configuration/three-phase.md) |
| La potencia CA y de celdas describen puntos distintos | **AC Power**, **Battery Cell Power** y entradas solares | Usa **Home Consumption** y la entidad de potencia correcta para la tarea |

**Resultado esperado:** el flujo de red se estabiliza alrededor del objetivo configurado después de que reaccionen el medidor y la batería. Los errores pequeños y breves pueden ser normales.

## Las entidades no están disponibles

| Causa probable | Qué comprobar | Acción |
|---|---|---|
| El host, puerto, ID esclavo o modelo de batería es incorrecto | Entrada de integración y configuración específica de batería | Corrige la conexión en la [guía de batería](configuration/batteries/index.md) pertinente |
| El control RS-485 de Marstek está desactivado | **RS485 Control Mode** | Actívalo antes de enviar órdenes de control |
| La pasarela o puente está desconectado | Estado del dispositivo de pasarela, ESPHome, MQTT o API | Restaura la conexión local y recarga la integración si es necesario |
| El controlador rechazó lecturas o escrituras repetidas | **Repairs** de Home Assistant, registros y **Non-Responsive Batteries** | Sigue las instrucciones de Repair; adjunta diagnósticos si se repite |
| Una entidad antigua pertenece a un controlador previo | Dispositivo e integración del registro de entidades | Elimina la entidad obsoleta no disponible si el controlador actual ha creado su sustituta |

**Resultado esperado:** el coordinador se actualiza y las entidades admitidas vuelven a estados numéricos o con nombre. Para detalles del protocolo Marstek, consulta el [resumen Modbus](reference/modbus-registers.md).

## El medidor de red no está disponible o está bloqueado

!!! warning "Un medidor no disponible puede dejar activa la última orden de batería"
    Cuando el sensor de red pasa a `unavailable` o `unknown`, el bucle de control no envía una orden nueva. Por tanto, una batería puede continuar a su última potencia solicitada —por ejemplo, 2000 W de descarga— hasta que regresen los datos del medidor. No hay un tiempo de espera automático que lleve la batería a reposo; solo siguen aplicándose los límites de SOC y otros de seguridad.

| Causa probable | Qué comprobar | Acción |
|---|---|---|
| Falló la conexión Wi-Fi o del broker | Integración del medidor, broker MQTT y disponibilidad del sensor | Restaura la conectividad antes de depender del control automático |
| El valor del sensor dejó de cambiar | `last_updated` mientras cambia la potencia doméstica | Reinicia o repara la integración fuente |
| Se seleccionó la entidad incorrecta | Sensor de red principal configurado | Selecciona la entidad de potencia neta de red descrita en la [configuración del sensor principal](configuration/main-sensor.md) |
| Shelly publica demasiado despacio | Cadencia de actualización del sensor MQTT | Usa el [script MQTT de Shelly Pro 3EM](hardware/shelly-pro-3em-mqtt-script.md) correspondiente |

Durante hasta 65 segundos después de la última lectura, el valor bloqueado sigue considerándose autoritativo. Después, el controlador puede realizar un recálculo de seguridad con el término derivativo suprimido, pero sigue usando el valor obsoleto; es más seguro restaurar la fuente rápidamente que depender de esto.

## Alterna continuamente entre carga y descarga

| Causa probable | Qué comprobar | Acción |
|---|---|---|
| El control es demasiado agresivo | **PD Control Quality**, perfil de ajuste, banda muerta y ajuste derivativo | Selecciona un perfil más suave o aumenta la banda muerta en [seguir el consumo de casa](features/pd-controller.md) |
| El sensor de red tiene ruido o retraso | Gráfica del sensor e intervalos de actualización | Corrige la fuente del medidor antes de seguir ajustando |
| Una carga pulsante cruza repetidamente el objetivo | Historial de carga y estado de dispositivo excluido | Configúrala mediante [exclusión de carga](features/load-exclusion.md) |
| La potencia mínima del relé provoca arranques repetidos | Controles de potencia mínima de carga/descarga y temporización del relé | Revisa los ajustes del actuador en [seguir el consumo de casa](features/pd-controller.md) |

**Resultado esperado:** **PD Control Quality** pasa a estable tras observar el controlador suficiente funcionamiento normal.

## No cargó durante la noche

| Causa probable | Qué comprobar | Acción |
|---|---|---|
| No se previó déficit de energía | Motivo de **Predictive Charging Active** y energía objetivo | No hace falta actuar si la energía almacenada y la previsión solar cubren la demanda |
| Faltan datos de precio o previsión | `price_data_status`, previsión solar y disponibilidad de entidad fuente | Restaura la fuente descrita por tu [modo predictivo](configuration/predictive-charging/index.md) |
| No hay periodo apto antes de la demanda | `chronological_plan_reason`, plazos, franjas seleccionadas e interruptores de franja | Ajusta el horario o el techo de precio |
| La potencia de carga o capacidad libre es insuficiente | `deadline_shortfall_kwh`, SOC de batería, límite de carga y SOC objetivo | Aumenta un límite intencionado o acepta el déficit informado |
| Otra función poseía la carga | `charge_blockers`, modo manual, retraso de carga o estado de franja horaria | Elimina la regla en conflicto |

**Resultado esperado:** el sensor de diagnóstico muestra un plan de carga de red viable o informa claramente de por qué la carga es innecesaria o físicamente imposible. Consulta [Precio dinámico](configuration/predictive-charging/dynamic-pricing.md) o [modo Franja horaria](configuration/predictive-charging/time-slot.md).

??? "La fuente de consumo muestra `legacy_daily` o el perfil solar recurre a un método alternativo"
    Una fuente de previsión de consumo `legacy_daily` es esperable mientras el perfil de 28 días sigue aprendiendo, o cuando el intervalo solicitado no cumple su contrato de cobertura. Comprueba **Expected Home Consumption Profile** y los diagnósticos de integración. Cambiar una fuente o ajuste de carga excluida conserva todos los días aprendidos; un cambio de zona horaria los redistribuye según el desplazamiento entre las dos zonas, y el relleno de Recorder reconstruye en segundo plano lo que siga faltando. No se interpola un hueco de más de cinco minutos.

    También es esperable que una previsión solar permanezca inmadura o recurra a un método alternativo durante los primeros días. El aprendizaje necesita potencia FV directa del sensor externo configurado o canales MPPT legibles, al menos siete días de calidad cerrados, cobertura reciente y evidencia suficiente en el intervalo futuro solicitado; se excluyen muestras no válidas, negativas y con huecos largos, y las señales de limitación de producción pueden excluir intervalos. Un cambio de fuente o capacidad inicia una generación nueva. Comprueba la sección `solar_profile` de los diagnósticos y `solar_timeline_fallback_reason`; el perfil no puede reparar una previsión meteorológica incorrecta ni modelar una limitación de producción que no puede observar.

## La carga completa semanal o el balance de celdas no terminó

| Causa probable | Qué comprobar | Acción |
|---|---|---|
| Día incorrecto o función desactivada | **Weekly Full Charge** y **Weekly Full Charge Day** | Activa y programa la [carga completa semanal](features/weekly-full-charge.md) |
| La carga está retrasada o bloqueada | Estado de carga semanal, `charge_blockers` y control manual | Elimina el bloqueador o desactiva el retraso configurado para esa ejecución |
| El sistema de gestión de batería paró en la parte superior | SOC, tensión de celda, potencia de carga y entidades de alarma y fallo | Deja que la integración aplique su reducción gradual admitida; inspecciona los fallos persistentes |
| Falta la telemetría de celda requerida | Tensión máxima/mínima de celda y entidades de balance | Comprueba la compatibilidad en [monitor de balance de celdas](features/cell-balance-monitor.md) |
| Un blueprint posee la batería | **Manual Battery Control** y traza de automatización | Revisa el [blueprint de balance activo](automations/blueprints.md) |

## Una batería de un sistema multibatería no participa

| Causa probable | Qué comprobar | Acción |
|---|---|---|
| Su SOC o prioridad hace preferible otra batería | **Active Batteries**, **Primary Battery** y **Charge Priority** | Puede ser esperado; revisa [varias baterías](features/multi-battery.md) |
| El sentido está desactivado | **Allow Charge** / **Allow Discharge** por batería | Activa el sentido requerido |
| La batería tiene control manual | **Manual Battery Control** | Libera el control manual tras devolver la batería a reposo |
| Se alcanzó el límite de potencia o SOC | Bloqueadores y entidades de límite por batería | Ajusta únicamente el límite que pretendes cambiar |
| Falló la entrega o la comunicación | **Non-Responsive Batteries** y **Repairs** | Sigue la Repair e inspecciona los diagnósticos |

## Aparece una alarma o un fallo de batería

| Causa probable | Qué comprobar | Acción |
|---|---|---|
| La batería informa de una advertencia o protección | **System Alarm Status**, **Alarm Status** y **Fault Status** por batería | Sigue las indicaciones del fabricante de la batería para la condición indicada |
| La condición ya se ha resuelto | Estado actual y notificación persistente | Confirma que se despeja la notificación; recarga solo si el estado sigue obsoleto |
| El modelo no expone registros de alarma | Disponibilidad de entidad de ese controlador | Usa la aplicación o interfaz local del fabricante |

Los registros de alarma y fallo (sondeados cada 5 segundos) solo están disponibles en hardware v2; v3, vA y vD no los exponen mediante Modbus. Cuando se establece un bit nuevo, Omnibattery crea una notificación persistente titulada con 🚨 para un fallo o ⚠️ para una alarma, con el nombre exacto de la condición (por ejemplo, *BAT Overvoltage* o *Fan Abnormal Warning*); se descarta automáticamente cuando se borran todos los bits.

## Antes de pedir ayuda

1. Abre **Ajustes → Dispositivos y servicios → Omnibattery**.
2. Abre la entrada de configuración afectada y selecciona **Download diagnostics**.
3. Revisa el JSON y elimina cualquier cosa que no quieras compartir.
4. Activa el registro de depuración desde la página de integración, reproduce el problema y después desactiva el registro para descargar el archivo de registro.
5. Incluye en tu informe el síntoma observado, la hora aproximada, estados de entidades relevantes, diagnósticos y registro.

Los diagnósticos ocultan campos de conexión e identificadores conocidos, pero aun así debes revisar el archivo antes de compartirlo.
