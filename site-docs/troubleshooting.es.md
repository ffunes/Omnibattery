# Solución de problemas

## Precio Dinámico muestra un *shortfall* de plazo

Consulta `deadline_shortfall_kwh`, `earliest_projected_depletion`, `slot_deadlines` y `chronological_plan_reason` en `binary_sensor.omnibattery_predictive_charging_active`. El *shortfall* indica que el mejor plan físicamente realizable no puede entregar toda la energía antes del cruce previsto del SOC mínimo. Las causas habituales son un techo máximo de precio explícito, ausencia de slots elegibles antes del plazo, potencia de carga insuficiente, falta de hueco o ownership manual/por franja. Un slot barato posterior no se presenta deliberadamente como cobertura de una necesidad anterior. La integración continúa el control normal y nunca ignora límites explícitos de seguridad.

## Compatibilidad con la app de Marstek

**No es necesario realizar ningún cambio en la app de Marstek** para que la integración funcione — incluyendo desactivar el medidor de energía o modificar cualquier configuración. La integración opera junto a la app sin requerir ningún ajuste desde ella.

Sin embargo, **no cambies ningún modo de operación ni configuración desde la app de Marstek mientras la integración de Home Assistant esté en ejecución**. Hacerlo romperá la compatibilidad y necesitarás deshabilitar y volver a habilitar la integración para restaurar el funcionamiento normal.

---

## La batería no responde a los comandos

1. Verifica que el conversor Modbus TCP (Elfin-EW11 o similar) está accesible por IP desde Home Assistant.
2. Comprueba que el puerto configurado es correcto (por defecto `502`).
3. Revisa que el switch **RS485 Control Mode** está activado.
4. Asegúrate de que la versión de batería configurada coincide con el hardware real.

!!! note "Delay para v3/vA/vD"
    Las baterías v3, vA y vD requieren al menos 150 ms entre mensajes Modbus consecutivos. La integración lo aplica automáticamente según la versión configurada.

---

## El controlador PD oscila

El sistema cambia continuamente entre carga y descarga.

**Posibles causas y soluciones:**

| Causa | Solución |
|---|---|
| Deadband demasiado pequeño | El ±40 W por defecto es adecuado para la mayoría de instalaciones |
| Sensor de red con latencia alta | Usa un sensor con actualización frecuente (1–2 s) |
| Cargas con arranque repentino | Configura la carga como [dispositivo excluido](configuration/excluded-devices.md) |

---

## Recibo una notificación de alarma o fallo de batería

La integración monitoriza los registros `Alarm Status` y `Fault Status` de la batería (solo v2) cada 5 segundos. Cuando se activa un nuevo bit, aparece una notificación persistente en Home Assistant con el nombre exacto de la condición (p. ej. *BAT Overvoltage*, *Fan Abnormal Warning*). La notificación se descarta automáticamente cuando todas las condiciones se resuelven.

**Niveles de severidad de la notificación:**

| Prefijo del título | Significado |
|---|---|
| 🚨 Battery Fault | Al menos un bit de fallo está activo — requiere atención inmediata |
| ⚠️ Battery Warning | Al menos un bit de alarma está activo — conviene monitorizar la situación |

**Qué hacer al recibir una notificación:**

1. Consulta el sensor **`System Alarm Status`** en el dispositivo *Omnibattery System* — sus atributos indican qué batería está afectada y qué condiciones están activas.
2. Revisa los sensores **Alarm Status** y **Fault Status** individuales en el dispositivo de la batería afectada para ver el estado completo.
3. Consulta la documentación de Marstek Venus o la app de Marstek para el código de fallo concreto.
4. Si la condición no se resuelve sola, considera reiniciar la batería o contactar con el soporte de Marstek.

!!! note "Solo baterías v2"
    La monitorización de registros de alarma y fallo solo está disponible para hardware v2. Las baterías v3, vA y vD no exponen estos registros vía Modbus.

---

## La carga predictiva no se activa

1. Verifica que el sensor de previsión solar está disponible y tiene valor.
2. Comprueba el atributo `price_data_status` del sensor `predictive_charging_active` (modo Precio Dinámico).
3. Revisa las notificaciones de HA: la evaluación de las 00:05 reporta el resultado.
4. Asegúrate de que el balance energético realmente requiere carga (puede que haya suficiente energía).

### El origen del consumo indica `legacy_daily`

Es normal mientras el perfil de 28 días está aprendiendo o cuando los intervalos
solicitados no cumplen su contrato de cobertura. Comprueba
`sensor.omnibattery_expected_home_consumption_profile` y los diagnósticos de la
integración. Cambiar la fuente o un ajuste de cargas conserva todos los días
aprendidos, y un cambio de zona horaria los reubica según el desfase entre ambas
zonas; después el backfill del Recorder reconstruye en segundo plano lo que siga
faltando. Los huecos de más de cinco minutos no se interpolan.

### El perfil solar sigue inmaduro o usa fallback

Es seguro y esperado durante los primeros días. El aprendizaje necesita
potencia FV directa del sensor externo configurado o canales MPPT legibles, al
menos siete días cerrados de calidad, cobertura reciente y evidencia suficiente
en el rango futuro solicitado. Se excluyen muestras inválidas, negativas y
huecos largos. Las señales de curtailment pueden excluir intervalos y un cambio
de fuente o capacidad inicia otra generación. Revisa `solar_profile` en los
diagnósticos y `solar_timeline_fallback_reason`; el perfil no corrige una
previsión meteorológica errónea ni modela curtailment no observable.

---

## El dispositivo de medida no está disponible o pierde conexión

Si el sensor de red (por ejemplo, un medidor con conexión Wi-Fi inestable) se desconecta, el controlador se comporta de forma diferente según cómo falle el sensor.

### El sensor reporta `unavailable` o `unknown`

El bucle de control sale inmediatamente sin enviar ningún nuevo comando. Las baterías **mantienen el último nivel de potencia comandado** hasta que el sensor vuelva a estar disponible.

### El sensor se congela (el valor deja de actualizarse)

La integración detecta que la marca de tiempo del sensor no ha cambiado:

- Durante hasta **15 ciclos (~30 segundos)** mantiene el último comando sin cambios.
- Pasado ese período de gracia, realiza un nuevo cálculo de seguridad usando el valor congelado, con el término derivativo suprimido para evitar picos de potencia.

### Resumen

| Estado del sensor | Comportamiento |
|---|---|
| `unavailable` / `unknown` | El bucle de control sale — las baterías mantienen la última potencia |
| Valor congelado (sin nuevas lecturas) | ~30 s de gracia, luego recalcula con el valor obsoleto |

!!! warning "Sin fallback automático a 0 W"
    Si el medidor se pierde mientras la batería estaba descargando a, por ejemplo, 2000 W, **seguirá descargando a 2000 W** hasta que el medidor se recupere. No hay ningún temporizador integrado que lleve la batería a reposo. Considera mejorar la fiabilidad del Wi-Fi de tu medidor, o usar una alternativa cableada o Zigbee si los cortes son frecuentes.

---

## Reportar un problema — Descargar diagnósticos

Al abrir un informe de error o pedir ayuda, adjunta el JSON generado por la acción **Descargar diagnósticos** de Home Assistant para la entrada de configuración de Omnibattery. El archivo contiene la configuración persistida junto con el estado de conexión de las baterías, las capacidades de los drivers, el seguimiento de baterías sin respuesta y los detalles de ejecución de los precios dinámicos. La integración redacta los campos sensibles de conexión y los identificadores conocidos.

**Cómo descargarlo:**

1. Ve a **Configuración → Dispositivos y servicios**.
2. Abre la integración **Omnibattery** y su entrada de configuración.
3. Pulsa **Descargar diagnósticos**.
4. Adjunta el archivo JSON resultante al caso de soporte.

Revisa el archivo antes de compartirlo y elimina cualquier dato específico de tu instalación que no quieras revelar.

---

## Registros de depuración

Activa el nivel de log `debug` para la integración pulsando en "Activar registro de depuración" en la configuración de la integración. Una vez que lo hayas ejecutado durante el tiempo apropiado, desactívalo para no llenar los logs, y se creará un archivo de log con la información de depuración.
