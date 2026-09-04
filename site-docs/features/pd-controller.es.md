# Controlador PD

El controlador PD (Proporcional-Derivativo) es el núcleo de la integración. Se ejecuta **dirigido por eventos** —recalcula cada vez que el sensor de consumo de red publica un valor nuevo— y ajusta la potencia de la batería para mantener el flujo de red cercano al objetivo configurado (por defecto, 0 W).

## Algoritmo

```
error = grid_power - target_power

P = Kp × error
D = Kd × (error - error_anterior) / dt

ajuste = P + D
nueva_potencia = potencia_actual + ajuste
```

Como el lazo es dirigido por eventos (cadencia variable), el término `P` y el límite de rampa se escalan internamente por el tiempo real transcurrido entre actualizaciones del sensor, de modo que el ajuste se comporta igual independientemente de la rapidez con que publique tu sensor.

### Parámetros por defecto

| Parámetro | Valor | Descripción |
|---|---|---|
| `Kp` | `0.35` | Ganancia proporcional |
| `Kd` | `0.3` | Ganancia derivativa |
| Deadband | `±40 W` | Zona muerta: ignora errores pequeños |
| Rate limit | `±800 W/ciclo` | Límite de cambio por ciclo |

## Perfiles de ajuste

En vez de ajustar las ganancias a mano, elige un **perfil de ajuste** (`select.*_pd_tuning_profile`): un preset de un clic que fija `Kp`, `Kd` y el límite de rampa a la vez. Ordenados de más suave a más rápido:

| Perfil | Kp | Kd | Rate limit | Cuándo |
|---|---|---|---|---|
| Muy suave | 0.22 | 0.15 | 400 W | Medidor ruidoso, cero cabeceo; calmo pero lento |
| Suave | 0.30 | 0.25 | 600 W | Conservador |
| Equilibrado | 0.35 | 0.30 | 800 W | Por defecto — vale para la mayoría |
| Agresivo | 0.55 | 0.45 | 1200 W | Medidor limpio, respuesta rápida |
| Muy agresivo | 0.75 | 0.45 | 2000 W | Medidor limpio + batería a plena potencia; respuesta más rápida |
| Personalizado | — | — | — | Manual: ajusta tú los sliders |

- Elegir un perfil escribe sus tres ganancias y las recarga en caliente (sin reinicio).
- Mover a mano cualquiera de esos tres sliders pasa el perfil a **Personalizado** automáticamente; tu valor se conserva.
- **El deadband no forma parte de los perfiles.** Es tu preferencia de precisión / ruido del medidor *y* la referencia contra la que mide el sensor de calidad, así que queda como un slider aparte que controlas tú. Cambiarlo no cambia el perfil activo.

En el dashboard, el selector de perfil y el sensor de calidad están al principio de la sección **Controlador PD** de la pestaña Control.

## Configuración desde el dashboard

!!! warning "Solo para usuarios expertos"
    No modifiques estos valores salvo que entiendas la teoría de control PD y cómo interactúa con los tiempos de respuesta del inversor. **Los valores por defecto funcionan correctamente en la gran mayoría de instalaciones.**

Los siguientes controles ajustan los parámetros internos del controlador PD. También se pueden modificar en tiempo de ejecución desde las entidades de configuración de la integración, sin reiniciar Home Assistant.

!!! tip "Mejor usa perfiles"
    La mayoría de usuarios no necesita cambiar estos valores a mano. El selector de **perfil de ajuste PD** aplica presets validados de `Kp`/`Kd`/límite de rampa en un clic, y el sensor de **calidad de control PD** muestra si el resultado es estable, oscilante o lento.

| Parámetro | Por defecto | Rango | Descripción |
|---|---|---|---|
| **Kp** | `0.35` | 0.1–2.0 | Ganancia proporcional. Un valor mayor produce una respuesta más rápida, pero más sobreoscilación. |
| **Kd** | `0.3` | 0.0–2.0 | Ganancia derivativa. Un valor mayor suaviza las transiciones, pero ralentiza la respuesta. |
| **Deadband** | `40 W` | 0–200 W | Zona muerta. El controlador no actúa si el error es menor que este valor. |
| **Cambio máximo de potencia** | `800 W/ciclo` | 100–2000 W | Cambio máximo por ciclo. Protege frente a variaciones bruscas. |
| **Histéresis direccional** | `60 W` | 0–200 W | Margen necesario para cambiar entre carga y descarga. |
| **Potencia mínima de carga** | `0 W` | 0–2000 W | Si la carga calculada está por debajo de este valor, el controlador permanece inactivo. `0` lo desactiva. |
| **Potencia mínima de descarga** | `0 W` | 0–2000 W | Igual que el anterior, para la descarga. `0` lo desactiva. |
| **Potencia objetivo de red** | `0 W` | −(descarga total configurada) … +(carga total configurada) | Consigna de red que regula el PD. Positivo = importar de red (la batería carga), negativo = exportar a red (la batería descarga), `0` = balance neto cero. El rango sigue tus baterías: tres unidades de 2500 W dan ±7500 W. Activar los límites de potencia del sistema estrecha cada dirección hasta su límite configurado. |
| **Activar límites de potencia del sistema** | desactivado | activado/desactivado | Activa el límite combinado de carga/descarga de todas las baterías activas. |
| **Potencia máxima de carga del sistema** | `0 W` | Dinámico: suma de potencias de carga configuradas | Límite opcional para la potencia de carga combinada. `0` lo desactiva. |
| **Potencia máxima de descarga del sistema** | `0 W` | Dinámico: suma de potencias de descarga configuradas | Límite opcional para la potencia de descarga combinada. `0` lo desactiva. |

Las potencias mínimas de carga/descarga son útiles para evitar microciclos ineficientes cuando la demanda de red es muy baja.

Los límites del sistema son útiles cuando la instalación tiene un límite compartido de hardware o cableado. No reducen el máximo individual de cada batería: una única batería activa puede seguir usando su límite configurado, mientras que varias baterías activas se limitan al máximo combinado.

Cuando **Activar límites de potencia del sistema** está desactivado, ambos límites se ignoran y no se crean sus entidades `number` de runtime. Cuando está activado, se exponen como sliders en el dispositivo Omnibattery System.

![Configuración avanzada del controlador PD](../assets/screenshots/configuration/advanced-pd-controller-config.png){ width="650" style="display: block; margin: 0 auto;"}

## Sensor de calidad de control

`sensor.marstek_venus_system_pd_control_quality` muestra de un vistazo cómo de bien mantiene el PD el objetivo de red, para que veas el efecto de un cambio de perfil/slider en vez de adivinar.

El **estado es un veredicto**, no un número:

| Estado | Significado | Qué hacer |
|---|---|---|
| Estable | El PD sigue bien el objetivo | Nada |
| Oscilando | Cabeceo (carga↔descarga frecuente) | Usa un perfil más suave, o sube el deadband |
| Lento | Demasiado lento para alcanzar | Usa un perfil más agresivo |
| Limitado por batería | Batería llena/vacía o en su límite de potencia — el PD no puede actuar | No es problema de ajuste |
| Recopilando datos | Calentando (recién arrancado) | Espera |

Los atributos llevan las cifras crudas: `rms_error_w` (error medio de seguimiento), `oscillation_per_min`, las ganancias activas y `active_profile`.

**Cómo ajustar:**

1. Mira el veredicto (y `rms_error_w`).
2. `Oscilando` → baja un perfil (Agresivo → Equilibrado → Suave). `Lento` → sube.
3. Espera **1–2 minutos** — la métrica es una media móvil de 60 s, así que va con retraso.
4. Repite hasta `Estable`.

La métrica es robusta frente a lecturas falsas: se pausa brevemente tras cualquier cambio de objetivo (balance neto horario, protección de capacidad, cambio manual de objetivo…) y mientras la batería está limitada, para no inflar la lectura.

## Cadencia de control

El controlador es **dirigido por eventos**: recalcula en el instante en que el sensor de consumo de red publica un valor nuevo, por lo que reacciona a la cadencia nativa del sensor (a menudo una vez por segundo) en lugar de esperar a un tick de temporizador fijo.

En paralelo corre un **watchdog de 2 segundos**. Mientras el sensor se actualiza con normalidad casi no hace nada —el evento ya procesó el último valor—; su función es mantener en marcha los subsistemas basados en tiempo y forzar una **recálculo de seguridad si el sensor se queda en silencio** (tras ~30 s sin actualizaciones el controlador reevalúa en vez de mantener el último comando indefinidamente).

Un lock evita ejecuciones solapadas: si un ciclo sigue en curso cuando se dispara el siguiente trigger, ese trigger se descarta (el ciclo en curso ya lee el estado actual). Así las escrituras Modbus a la batería quedan serializadas.

## Mecanismos de estabilización

### Deadband (zona muerta)

Si el error es menor de ±40 W, el controlador no ajusta la potencia. Evita micro-oscilaciones continuas por ruido del sensor.

### Rate limiting

El cambio de potencia se limita por ciclo para suavizar las transiciones y proteger la batería de cambios bruscos. Un «ciclo» es una actualización de control, que se dispara con cada valor nuevo del sensor. El límite por ciclo configurado se escala internamente por el tiempo real transcurrido entre actualizaciones, de modo que la tasa efectiva de rampa (W/s) se mantiene constante independientemente de la rapidez con que publique el sensor. Baja el límite si la respuesta se siente brusca.

### Detección de oscilaciones

El controlador monitoriza reversiones de dirección (carga↔descarga) frecuentes. Si detecta oscilación sostenida, reduce temporalmente la ganancia efectiva.

### Histéresis direccional

Evita cambios de dirección por variaciones de carga momentáneas (como el arranque de electrodomésticos). El controlador requiere que el error supere un umbral durante varios ciclos antes de cambiar de carga a descarga o viceversa.

### Filtrado del término derivativo

El término derivativo se filtra con un paso-bajo (constante de tiempo corta) antes de llegar a la salida. Derivar una señal de red apenas suavizada amplificaría el ruido de cuantización del medidor y el PWM del inversor, inyectándolo en la potencia de la batería; el filtrado mantiene el derivativo útil sin ese ruido.

### Anti-windup por potencia medida

El controlador asume que cada batería entrega exactamente la potencia comandada. Cuando no puede —por ejemplo por reducción (taper) de SOC/voltaje o por retardo de rampa—, el controlador detecta el déficit sostenido comparando el comando con la potencia AC medida y reancla su línea base interna a la realidad. Así evita que la salida de control «se acumule» (windup) por encima de lo que el hardware entregó realmente, lo que de otro modo causaría un sobreimpulso o una breve exportación a red cuando la carga baja después.

## Protección de relé y de tasa de escritura

Dos sliders opcionales protegen el hardware del traqueteo cuando la red ronda el borde del deadband o un medidor rápido publica ráfagas. Ambos vienen con un valor casi desactivado, así que las instalaciones existentes no cambian.

| Slider | Por defecto | Qué hace |
|---|---|---|
| **PD Relay Cooldown** (`number.*_pd_relay_cooldown`, s) | `0` (desactivado) | Tiempo mínimo que la batería sigue activa antes de volver a reposo. Frena el traqueteo de relé durante las rampas solares. El tiempo se cuenta **desde el momento en que se pide el reposo**, así que de verdad se mantiene. Mientras se mantiene corre a la potencia mínima de carga/descarga configurada (o 100 W si es 0). Los desbalances grandes lo saltan. Solo gobierna activo→reposo, no los cambios carga↔descarga. |
| **PD Min Cycle Interval** (`number.*_pd_min_cycle_interval`, s) | `1` | Limita con qué frecuencia corre el lazo dirigido por eventos — las actualizaciones de red más cercanas que esto se descartan, así un medidor rápido no inunda bridges Modbus lentos (p. ej. Elfin EW11) con ráfagas de escritura. El watchdog de seguridad de 2 s nunca se limita, así que el control nunca se detiene. `0` = desactivado. |

## Modo de seguimiento directo No-PD

Una alternativa **opcional** a la ley de control PD, para quien quiere que la batería siga el sensor de consumo **1:1 en un único ciclo** — sin integral, derivativo, suavizado, limitador de rampa ni histéresis. Actívalo con el switch **No-PD Direct Tracking** (`switch.*_no_pd_mode`); el controlador PD queda intacto mientras esté desactivado (los dos son mutuamente excluyentes en el dashboard).

En cada ciclo reconstruye la carga del hogar a partir de la potencia AC **medida** de la batería (`nueva = medida − error`) en vez del último comando, así se mantiene estable durante la rampa de varios segundos del inversor en lugar de oscilar de extremo a extremo.

Reutiliza el deadband, la potencia mínima de carga/descarga, el min-ON de relé y los sliders de setpoint de red existentes, más una perilla específica del modo:

- **No-PD Command Delay** (`number.*_no_pd_command_delay`, s) — amortigua medidores rápidos colapsando una ráfaga de actualizaciones en un único comando sobre el último valor.

!!! tip "Cuándo usarlo"
    No-PD encaja con un medidor limpio y rápido donde quieres la respuesta más directa posible y el ajuste PD te sobra. Con un medidor ruidoso, el filtrado del controlador PD suele ser mejor opción.

## Exclusión por función de reserva

Una batería queda excluida del controlador PD cuando se cumplen **las dos** condiciones siguientes:

1. El switch **Función de reserva** (`switch.*_backup_function`) está activado.
2. El sensor **Potencia AC offgrid** (`sensor.*_ac_offgrid_power`) reporta un valor distinto de 0 W, lo que confirma que la batería está proporcionando energía offgrid activamente.

Tener el switch activado por sí solo no es suficiente. Si el switch está activo pero la potencia AC offgrid lee 0 W (la batería no está sirviendo ninguna carga offgrid), la batería sigue participando en el control PD con normalidad.

Mientras está excluida, el controlador no envía ningún comando de potencia, cambio de modo forzado ni escritura de registros de configuración. La batería sigue siendo consultada con normalidad, por lo que todos los sensores de solo lectura (SOC, potencia, temperatura, etc.) se mantienen actualizados.

### Cooldown post-backup

Cuando la carga offgrid vuelve a 0 W, la batería no se reincorpora inmediatamente al control PD. Se aplica un **cooldown de 5 minutos** que mantiene la batería excluida tras el fin del evento de reserva, evitando enviar comandos de escritura a una batería que puede estar aún estabilizándose.

Desactivar el switch de **Función de reserva** elimina el cooldown de forma inmediata.

!!! info
    La exclusión también aplica a las escrituras de registro de la carga semanal completa y a la secuencia de apagado.

## Potencia objetivo por franja

Cada [franja horaria](../configuration/time-slots.md) puede tener su propia **potencia objetivo de red** (`target_grid_power`), permitiendo distintas estrategias según el momento del día.

![Entidades del controlador PD en Home Assistant](../assets/screenshots/features/pd-controller-entities.png){ width="700"  style="display: block; margin: 0 auto;"}
