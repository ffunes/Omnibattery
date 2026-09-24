# Seguir el consumo de casa

El control proporcional–derivativo (PD) ajusta la potencia de la batería según cambia la demanda del hogar y mantiene la importación o exportación de red cerca del objetivo que elijas. Empieza con **Balanced** y ajusta solo si observas un problema repetible.

## ¿Lo necesito?

**Úsalo si** quieres que Omnibattery siga automáticamente los cambios del consumo de casa. Es el modo de control normal y sirve para la mayoría de instalaciones.

**No necesitas ajustarlo si** el flujo de red se mantiene cerca del objetivo sin cambios repetidos de carga y descarga. Las diferencias pequeñas y estables dentro de la banda muerta son intencionadas y evitan microciclos ineficientes.

## Antes de empezar

- Configura un sensor de consumo de red que funcione y se actualice con frecuencia.
- Permite que Omnibattery controle la batería automáticamente; el modo manual y otras reglas activas pueden tomar el control temporalmente.
- Comprueba que la batería puede cargar y descargar y que no está ya en un límite de estado de carga o de potencia.

## Cómo activarlo

1. Abre el panel lateral de Omnibattery y selecciona **Control**.
2. En **PD controller**, activa **PD control**. Esto desactiva **No-PD Direct Tracking** porque ambos modos son mutuamente excluyentes.
3. Selecciona **Balanced** en **PD tuning profile** y deja sin cambios los controles manuales de ganancias.
4. Observa **PD Control Quality** mientras cambian las cargas normales de casa.

![Entidades del controlador PD en Home Assistant](../assets/screenshots/features/pd-controller-entities.png){ width="700" style="display: block; margin: 0 auto;"}

## Qué verás

**PD Control Quality** muestra un veredicto práctico:

| Estado | Significado | Acción |
|---|---|---|
| **Stable** | El flujo de red sigue el objetivo sin oscilación persistente | Mantén la configuración actual |
| **Oscillating** | La importación y la exportación se alternan repetidamente fuera de la banda muerta | Sigue la fila sobre oscilación de abajo |
| **Sluggish** | Un error sostenido se corrige demasiado despacio | Sigue la fila sobre respuesta lenta de abajo |
| **Battery limited** | La batería está llena, vacía o en un límite de potencia | Comprueba los límites de batería; el ajuste no puede añadir capacidad |
| **Blocked** | Una programación, retraso de carga, regla de precio o carga excluida impide la acción necesaria | Busca la regla activa antes de ajustar |
| **Collecting data** | La métrica se está calentando o no ha recibido datos de control utilizables recientemente | Espera a que se reanude el control automático normal |
| **Disabled** | Está activo el seguimiento directo No-PD | Usa sus controles en lugar del ajuste PD |

Después de cambiar un ajuste, deja que la métrica de calidad refleje el nuevo comportamiento antes de cambiar otro.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| Hay una importación o exportación pequeña y estable cerca del objetivo | El error está dentro de la banda muerta | Déjalo como está salvo que la diferencia importe para tu tarifa; estrechar la banda muerta puede provocar más conmutaciones |
| La carga y descarga se alternan repetidamente | La banda muerta es demasiado estrecha, el perfil demasiado agresivo o la derivada reacciona al ruido del medidor | Aumenta primero **PD Deadband**; después elige el siguiente perfil más suave; en **Custom**, reduce **Kp** y luego **Kd** |
| La respuesta sigue siendo lenta ante una carga sostenida | La batería está limitada o el perfil es demasiado suave | Descarta primero **Battery limited** o **Blocked**; después elige el siguiente perfil más rápido; en **Custom**, aumenta **Kp** y luego **PD Max Power Change** si la rampa es el límite |
| Aparecen picos grandes de importación/exportación tras cambios de carga | El inversor aún está acelerando, o la primera corrección es demasiado brusca | Pueden esperarse picos breves mientras la potencia medida se estabiliza; si se repite el sobreimpulso, elige un perfil más suave y después reduce **PD Max Power Change** |
| La batería hace clic al entrar y salir de reposo | La demanda oscila alrededor del borde de la banda muerta | Aumenta gradualmente **PD Relay Cooldown**; solo afecta a transiciones de activo a reposo |
| Un medidor rápido provoca muchas escrituras en la batería | Los ciclos de control llegan más rápido de lo que el puente puede manejar | Aumenta **PD Min Cycle Interval**; en modo de seguimiento directo, aumenta **No-PD Command Delay** |
| La calidad sigue en **Battery limited** o **Blocked** | El controlador no puede aplicar la dirección requerida | Comprueba el estado de carga, los límites de potencia de batería, franjas horarias, retraso de carga, reglas de precios y cargas excluidas |

??? "Detalles avanzados"
    ### Ley de control y cadencia

    El controlador se ejecuta cuando el sensor de red publica un valor nuevo. En paralelo, una **vigilancia de seguridad de 2 segundos** mantiene en marcha las demás funciones basadas en tiempo y fuerza una reevaluación de seguridad, en lugar de conservar indefinidamente la última orden, si el sensor queda en silencio durante unos **65 segundos**; un bloqueo serializa las ejecuciones solapadas.

    El controlador usa una ley de control incremental. Una potencia de orden positiva significa carga de batería y una negativa, descarga:

    ```text
    error = grid_power - target_power
    P = Kp × error
    D = Kd × filtered_change_in_error / elapsed_time
    new_power = current_power − (P + D)
    ```

    El término proporcional y el límite de rampa se escalan según el tiempo transcurrido, de modo que un medidor más rápido no multiplica la tasa de corrección prevista. La derivada se filtra paso bajo para reducir la cuantización del medidor y el ruido del inversor. Cuando la potencia CA medida muestra que la batería no puede entregar su orden, la protección contra acumulación del control vuelve a anclar la siguiente corrección a la salida medida.

    El objetivo predeterminado es `0 W`: una potencia de red positiva es importación y una negativa, exportación. Una [franja horaria](../configuration/time-slots.md) puede establecer un objetivo distinto durante su periodo activo.

    ### Perfiles de ajuste

    Un perfil establece conjuntamente **Kp**, **Kd** y **PD Max Power Change**. Al mover uno de esos controles, el selector cambia a **Custom**. **PD Deadband** es independiente.

    | Perfil | Kp | Kd | Cambio máx. | Comportamiento previsto |
    |---|---:|---:|---:|---|
    | **Very Smooth** | `0.22` | `0.15` | `400 W` | Respuesta más tranquila para un medidor ruidoso |
    | **Smooth** | `0.30` | `0.25` | `600 W` | Respuesta conservadora |
    | **Balanced** | `0.35` | `0.30` | `800 W` | Ganancias de fábrica y punto de partida normal |
    | **Aggressive** | `0.55` | `0.45` | `1,200 W` | Respuesta más rápida con mayor riesgo de sobreimpulso |
    | **Very Aggressive** | `0.75` | `0.45` | `2,000 W` | El preajuste más rápido para baterías que pueden aprovechar todo el paso |
    | **Custom** | — | — | — | Control manual de los tres valores del perfil |

    | Control | Predeterminado | Rango | Efecto |
    |---|---:|---:|---|
    | **PD Target Grid Power** | `0 W` | `±2,500 W` (alternativa) | Consigna de red que regula el PD. Positivo = importación de red (la batería carga), negativo = exportación a red (la batería descarga). El rango sigue tus baterías configuradas: tres unidades de 2,500 W dan un rango de ±7,500 W. Activar los límites de potencia del sistema estrecha cada dirección a su tope configurado. Una [franja horaria](../configuration/time-slots.md) puede establecer un objetivo distinto durante su periodo activo |
    | **PD Kp** | `0.35` | `0.1–2.0` | Aumenta o reduce la corrección aplicada a un error sostenido |
    | **PD Kd** | `0.30` | `0.0–2.0` | Reacciona a los cambios de error; un valor excesivo puede amplificar lecturas ruidosas o retrasadas |
    | **PD Deadband** | `40 W` | `0–200 W` | Ignora errores pequeños alrededor del objetivo |
    | **PD Max Power Change** | `800 W per nominal cycle` | `100–2,000 W` | Limita lo brusco que puede ser el cambio de orden; se escala internamente por el tiempo transcurrido |
    | **PD Direction Hysteresis** | `60 W` | `0–200 W` | Rechaza pequeñas peticiones de invertir la dirección carga/descarga |
    | **PD Min Charge Power** | `0 W` | `0–2,000 W` | Mantiene en reposo peticiones de carga pequeñas; `0 W` desactiva el mínimo |
    | **PD Min Discharge Power** | `0 W` | `0–2,000 W` | Mantiene en reposo peticiones de descarga pequeñas; `0 W` desactiva el mínimo |
    | **PD Relay Cooldown** | `0 s` | `0–60 s` | Mantiene una batería activada antes de una transición de activo a reposo; `0 s` lo desactiva |
    | **PD Min Cycle Interval** | `1.0 s` | `0–2.0 s` | Descarta ciclos activados por el sensor demasiado cercanos; `0 s` desactiva el intervalo |

    Si la salida de la batería está limitada por debajo del cambio máximo del perfil, el limitador de rampa puede no actuar antes de que la batería alcance su tope. Usa un perfil más suave o establece un cambio máximo personalizado menor cuando ese primer paso produzca sobreimpulso.

    Las potencias mínimas de carga y descarga pueden evitar un funcionamiento ineficiente a baja potencia. Durante la espera del relé, Omnibattery mantiene la dirección activa en el mínimo configurado o en `100 W` cuando ese mínimo está desactivado. Un gran desequilibrio evita esta retención. Las inversiones de carga a descarga usan la protección de cruce por cero independiente de abajo.

    Los límites de potencia del sistema pueden limitar opcionalmente la potencia combinada de carga y descarga sin reducir el límite propio de cada batería. Cuando se activan, aparecen los dos controles de tope en el dispositivo Omnibattery System; `0 W` desactiva un tope.

    ![Configuración avanzada del controlador PD](../assets/screenshots/configuration/advanced-pd-controller-config.png){ width="650" style="display: block; margin: 0 auto;"}

    ### Estabilización automática

    - **Banda muerta:** no se realiza ninguna corrección mientras el error se mantenga dentro de la banda configurada.
    - **Histéresis de dirección:** una pequeña petición en dirección opuesta se mantiene en reposo.
    - **Detección de oscilación:** las inversiones repetidas del signo del error fuera de la banda muerta reinician el estado acumulado del controlador para que el control proporcional pueda recuperarse.
    - **Feedforward:** en modo PD, un gran escalón de carga que persiste hasta la siguiente muestra recibe una corrección directa anclada a la potencia medida. Se ignora un pico de una sola muestra, se protegen cargas pulsantes opuestas y el ajuste PD normal se reanuda en el ciclo siguiente. No hay ajuste de usuario para feedforward.
    - **Retención de cruce por cero:** todas las rutas de control fijan temporalmente en reposo una inversión de carga a descarga o de descarga a carga. La petición debe persistir al menos `5 s`, o el doble de la latencia del actuador de la batería más lenta si es mayor. Esto puede producir una breve orden de `0 W` tras un cambio de dirección real.

    ### Seguimiento directo No-PD

    **No-PD Direct Tracking** es una alternativa opcional para un medidor limpio y rápido. Reconstruye la carga de casa a partir de la potencia CA medida de batería y el error de red, y pide directamente el resultado en un ciclo de control:

    ```text
    new_power = measured_battery_power − error
    ```

    Esta vía evita las ganancias PD, el filtro derivativo, el límite de rampa gradual y la histéresis de dirección. Sigue usando la banda muerta, la potencia mínima de carga/descarga, la espera de relé, el ajuste de objetivo de red, las restricciones de operación y la retención de cruce por cero. **No-PD Command Delay** agrupa actualizaciones rápidas del medidor en una orden con el último valor; su valor predeterminado es `0.0 s` y su rango es `0–3.0 s`.

    ### Diagnóstico de calidad de control

    La métrica de calidad usa una ventana de promedio exponencial de `60 s` y se pausa tras cambios de objetivo o mientras el control está limitado. Si no avanza durante `300 s`, el estado vuelve a **Collecting data**.

    Los atributos de diagnóstico son `rms_error_w`, `oscillation_per_min`, `metric_age_s`, `kp`, `kd`, `deadband_w`, `max_power_change_w` y `active_profile`.

    ### Exclusión de salida de respaldo

    Una batería con **Backup Function** activada queda excluida cuando **AC Offgrid Power** supera su **Backup Offgrid Threshold**, o cuando esa lectura de potencia no está disponible. El umbral predeterminado es `50 W`, por lo que pequeñas cargas permanentes en la salida de respaldo no retiran la batería del control normal.

    Mientras está excluida, Omnibattery sigue consultando datos de solo lectura, pero no envía a esa batería órdenes de potencia, modo forzado, configuración ni carga completa semanal. Tras volver la potencia aislada por debajo del umbral, la exclusión se mantiene `5 min`; desactivar **Backup Function** elimina el periodo de espera inmediatamente.
