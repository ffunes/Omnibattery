# Balance neto horario

El balance neto horario ajusta la potencia de la batería durante cada hora de reloj para que la importación de red menos la exportación se acerque al objetivo energético que elijas. Es útil cuando tu tarifa o compensación se liquida por horas y no por cada instante del flujo de potencia.

## ¿Lo necesito?

**Úsalo si** tu contrato eléctrico valora la energía neta de red dentro de cada hora de reloj y quieres que Omnibattery corrija una importación o exportación inicial antes de que termine esa hora.

**No lo necesitas si** tu facturación usa otro periodo de liquidación, el control instantáneo de red cero ya cumple tu objetivo o no quieres que la batería gaste energía corrigiendo la hora actual.

## Antes de empezar

- Configura el sensor de consumo de red que usa Omnibattery. Es la fuente alternativa de esta función.
- Activa el control automático de batería y asegúrate de que al menos una batería pueda actuar en la dirección requerida.
- Si usas franjas horarias, la función solo opera mientras una franja configurada permita descargar. Sin franjas activadas, opera todo el día.
- Comprueba si está activa la [protección de capacidad (peak shaving)](peak-shaving.md). Cuando interviene, su objetivo de seguridad tiene prioridad sobre la corrección horaria.

## Cómo activarlo

1. Abre el panel lateral de Omnibattery y selecciona **Control**.
2. Busca **Hourly net balance** y actívalo.
3. Deja **Hourly Balance Target** en `0.0 kWh` para balance neto cero, o elige un objetivo positivo para importación neta y uno negativo para exportación neta.
4. Empieza con el valor predeterminado de **Hourly Balance Max Offset** de `1,000 W` y confirma que **Balance Neto** empieza a seguir la hora actual.

## Qué verás

**Balance Neto** muestra la energía neta de red de la hora actual. Un estado positivo significa exportación neta; uno negativo, importación neta. Su estado indica si la función está inactiva, fuera de una franja horaria, compensando hacia importación o exportación, limitada por su desplazamiento máximo o bloqueada para cargar.

La corrección cambia durante la hora. Por ejemplo, después de acumular importación neta, Omnibattery desplaza el objetivo de red hacia descarga o exportación durante el tiempo restante. No borra el historial medido; ajusta la potencia necesaria para aproximarse al objetivo al final de la hora.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| **Balance Neto** permanece en **Idle** | El interruptor está apagado, no ha llegado una muestra válida o el balance ya está dentro de la tolerancia | Confirma **Hourly net balance**, el sensor de red y **Hourly Balance Deadband** |
| El estado es **Out of slot** | Existen franjas horarias activadas y la hora actual queda fuera de todas | Comprueba los días y horas en [Franjas horarias](../configuration/time-slots.md) |
| El estado es **Blocked** o `compensation_stopped` | La carga está impedida por retraso de carga solar, una franja horaria, pausa de vehículo eléctrico, histéresis de carga o estado de carga máximo | Inspecciona `charge_block_reason` y el control correspondiente |
| El estado sigue en **Capped** y la hora no alcanza su objetivo | La corrección requerida supera **Hourly Balance Max Offset**, o la batería no tiene potencia o energía disponible | Comprueba primero los límites de batería; después aumenta gradualmente **Hourly Balance Max Offset** si la instalación puede soportarlo |
| El desplazamiento cambia demasiado a menudo | La histéresis del desplazamiento es demasiado pequeña para el ruido del medidor | Aumenta **Hourly Balance Hysteresis** |
| No hay corrección cerca del objetivo | La desviación está dentro de la tolerancia energética | Reduce **Hourly Balance Deadband** si una corrección más precisa compensa el ciclado adicional |
| La corrección desaparece mientras está activa la protección de capacidad | La protección de capacidad controla el objetivo de red | Es lo esperado; su objetivo absoluto de seguridad sustituye los objetivos horarios y otros objetivos aditivos mientras está activa |
| La fuente mostrada no está disponible | El sensor externo opcional de balance dejó de actualizarse | Restaura ese sensor; Omnibattery usa la integración de potencia de red cuando no detecta un sensor externo |

??? "Detalles avanzados"
    ### Ajustes

    | Control | Predeterminado | Rango | Efecto |
    |---|---:|---:|---|
    | **Hourly Balance Target** | `0.0 kWh` | `−2.0–2.0 kWh` | Energía neta de red deseada para cada hora civil; positiva significa importación y negativa, exportación |
    | **Hourly Balance Max Offset** | `1,000 W` | `100–5,000 W` | Limita cuánto puede desplazar la función el objetivo de potencia de red |
    | **Hourly Balance Deadband** | `0.0 kWh` | `0.0–0.5 kWh` | No aplica corrección mientras la desviación energética permanezca dentro de esta tolerancia |
    | **Hourly Balance Hysteresis** | `15 W` | `0–200 W` | Requiere este cambio de desplazamiento antes de publicar una nueva corrección |

    Un desplazamiento máximo mayor puede cerrar una brecha energética mayor en el tiempo disponible, pero también vuelve la respuesta de potencia más agresiva. Los límites de la batería y del sistema siguen aplicándose.

    ### Cálculo

    En cada ciclo de control, Omnibattery acumula importación y exportación para la hora civil local actual y calcula un desplazamiento aditivo del objetivo:

    ```text
    net_Wh = imported_Wh − exported_Wh
    deficit_Wh = target_net_Wh − net_Wh
    offset_W = deficit_Wh / remaining_hours
    ```

    Un desplazamiento positivo mueve el objetivo hacia importación de red; uno negativo, hacia descarga o exportación. El desplazamiento entra gradualmente durante los primeros `5 min` de la hora, se limita a **Hourly Balance Max Offset** y se detiene durante el último `1 min`. La histéresis del desplazamiento se omite durante los últimos `10 min` para que la corrección pueda seguir más de cerca el tiempo restante.

    La función elimina su desplazamiento en modo manual y fuera de las franjas horarias activas. Sin franjas activadas, sigue siendo elegible durante todo el día.

    ### Fuente de datos

    Omnibattery busca primero `sensor.balance_neto`. Supone que un valor positivo significa exportación y elige un método de lectura según la unidad y la clase de estado:

    | Tipo de fuente | Unidad | Clase de estado | Método de lectura |
    |---|---|---|---|
    | Energía acumulada | `kWh` o `Wh` | `total` o `total_increasing` | Diferencia con una instantánea tomada en el límite de la hora |
    | Energía neta instantánea | `kWh` o `Wh` | `measurement` u otra clase no total | Lectura directa |
    | Potencia de red | `W` o `kW` | Cualquiera | Integra la potencia en el tiempo con un cálculo trapezoidal |

    Cuando no se detecta un sensor externo compatible, el sensor configurado de consumo de red proporciona las muestras de potencia. **Balance Neto** expone el ID de entidad activo en `source`, o `trapezoidal` para la alternativa.

    ### Bloqueo y prioridad del objetivo

    La corrección de dirección de carga puede informar estos valores de `charge_block_reason`:

    | Motivo | Significado |
    |---|---|
    | `solar_charge_delay` | El retraso de carga solar impide cargar |
    | `time_slot` | Las reglas de la franja horaria activa impiden cargar |
    | `ev_pause` | La gestión de carga de vehículo eléctrico ha pausado la carga |
    | `hysteresis` | Está activa la histéresis de carga de batería |
    | `max_soc` | Todas las baterías con datos han alcanzado su estado de carga máximo |

    Mientras la carga está bloqueada, el desplazamiento positivo del objetivo permanece registrado. Esto evita que el controlador PD descargue la batería para cubrir la carga de casa mientras la red la suministra, y permite que la corrección guardada tenga efecto cuando desaparezca el bloqueo. Las correcciones en dirección de exportación no están bloqueadas por estas condiciones de carga.

    El balance horario es un objetivo aditivo: Omnibattery lo suma al objetivo de red del usuario y otras preferencias aditivas. Una anulación absoluta activa sustituye esa suma. La protección de capacidad usa esa anulación, de modo que tiene prioridad mientras controla un pico.

    ### Atributos de diagnóstico y persistencia

    **Balance Neto** puede exponer estos atributos:

    | Atributo | Significado |
    |---|---|
    | `status` | `idle`, `out_of_slot`, `capped`, `compensating_import`, `compensating_export` o `compensation_stopped` |
    | `offset_w` | Corrección de objetivo activa en vatios |
    | `imp_wh` | Importación de red acumulada en la hora actual |
    | `exp_wh` | Exportación de red acumulada en la hora actual |
    | `target_net_wh` | Objetivo horario configurado en vatios-hora |
    | `remaining_min` | Tiempo restante en la hora actual |
    | `source` | ID de entidad de fuente externa o `trapezoidal` |
    | `hour_iso` | Marca de tiempo local al inicio de la hora seguida |
    | `charge_block_reason` | Bloqueador de carga, presente solo mientras aplica uno |

    Los acumuladores y el último desplazamiento se guardan aproximadamente cada `5 min` y cuando se descarga la integración. Un reinicio solo los restaura si los datos guardados pertenecen a la hora civil local actual.
