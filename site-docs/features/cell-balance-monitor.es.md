# ¿Está sana mi batería?

El monitor de equilibrio de celdas compara la celda más alta y la más baja cerca del final de una carga completa. **Balance - Estado** ofrece una respuesta directa; **Balance - Delta de Celda al 100% (última carga completa)** y su historial te ayudan a decidir si un resultado inusual se mantiene en el tiempo.

## ¿Lo necesito?

**Úsalo si** tu batería muestra los extremos mínimo y máximo de tensión de celda y quieres comprobar si sus celdas se mantienen equilibradas con el tiempo. El monitor es automático en las baterías compatibles, así que no hay un interruptor independiente para activarlo.

**No lo necesitas si** tu batería no muestra ambos extremos de tensión de celda. En ese caso no aparecen las entidades de equilibrio, y su ausencia no indica por sí misma un fallo de la batería.

## Antes de empezar

- Busca **Balance - Estado**, **Balance - Delta de Celda al 100% (última carga completa)** y **Balance - Última Lectura** en el dispositivo de la batería.
- El equilibrio de celdas está disponible para Marstek Venus E v2/v3 y Venus A/D, baterías Zendure que publican los extremos de celda y dispositivos ESPHome/LilyGo cuando existen las entidades de origen.
- Los controladores de Anker, Hoymiles, Huawei y Sessy no proporcionan actualmente las dos lecturas que requiere este monitor.
- Activa **Activar reducción por voltaje al cargar al 100%** en la batería, o usa la [carga semanal completa](weekly-full-charge.md), para obtener una lectura comparable cerca del final de carga.

## Cómo activarlo

Esta función es automática; no hay un interruptor de monitorización ni un formulario de activación.

1. Abre el dispositivo de la batería en Home Assistant y confirma que existen **Balance - Delta de Celda al 100% (última carga completa)** y **Balance - Estado**.
2. En el panel de Omnibattery, deja activado **Activar reducción por voltaje al cargar al 100%** para esa batería.
3. Deja que la batería complete una carga completa y comprueba después que se ha actualizado **Balance - Última Lectura**.

## Qué verás

### ¿Está sana mi batería?

Lee primero **Balance - Estado**. Una lectura naranja o roja no demuestra que una celda esté degradada; compara lecturas de cargas completas terminadas antes de sacar una conclusión.

| Estado | Balance - Delta de Celda al 100% (última carga completa) | Qué significa | Qué hacer |
|---|---:|---|---|
| `green` | Por debajo de 200 mV | Dentro de la banda normal del monitor | No es necesario actuar |
| `yellow` | 200–229 mV | Por encima de la banda normal | Comprueba la siguiente lectura de carga completa y la tendencia |
| `orange` | 230–249 mV | Desequilibrio moderado | Repite una carga completa y confirma que el resultado persiste |
| `red` | 250 mV o más | Desequilibrio alto | Compara lecturas consecutivas; usa el blueprint de recuperación solo si el resultado persiste |
| `unknown` | No hay lectura comparable | El monitor no ha registrado un resultado válido cerca del final de carga | Comprueba **Balance - Última Lectura**, la disponibilidad de tensiones de celda y la reducción gradual de carga |

Comprueba también estas entidades:

- **Balance - Delta de Celda al 100% (última carga completa)**: la diferencia medida en milivoltios (mV).
- **Balance - Última Lectura**: cuándo terminó la última medición comparable.
- **Balance - Tendencia**: `rising`, `stable` o `falling` en las lecturas recientes.
- **Balance - Delta Promedio (4 lecturas)**: la media de las cuatro últimas lecturas comparables.

Sus atributos `measured_at` y `soc_at_measurement` indican cuándo se tomó esa instantánea y con qué SOC; no cambia entre cargas completas.

### Por qué el delta no coincide con la tensión máxima menos la mínima

**Balance - Delta de Celda al 100% (última carga completa)** no es un valor en vivo. Se registra cerca del final de una carga completa, que es donde las celdas LFP realmente se separan. **Tensión Máxima de Celda** y **Tensión Mínima de Celda** son valores en vivo, y entre aproximadamente el 20 % y el 90 % de SOC la tensión de una celda LFP es tan plana que incluso un pack desequilibrado solo muestra unos pocos milivoltios de diferencia. Un valor guardado de 243 mV junto a 3,331 V / 3,328 V en vivo al 80 % de SOC es por tanto normal y ambos valores son correctos.

**Balance - Delta de Celda (en vivo)** muestra esa diferencia en vivo (máx − mín, en mV). Úsalo para seguir una carga hasta el final; juzga el balance por el valor al 100%. En Venus A/D con datos por pack es el pack individual con mayor diferencia, con su número en el atributo `pack`.

En baterías con datos por pack, **Balance - Delta de Celda al 100% (última carga completa)** representa la peor diferencia interna entre los packs. Sus atributos `packs_mV` y `worst_pack` identifican el pack responsable del resultado; Omnibattery no resta la celda más baja de un pack de la celda más alta de otro.

Si el naranja o el rojo persisten tras varias cargas completas, usa el [blueprint de equilibrio activo para Marstek](../automations/blueprints.md#balanceo-activo-de-una-bateria-marstek) con una batería Marstek compatible. Ejecútalo con una sola batería cada vez y sigue sus notificaciones de limpieza antes de devolver esa batería al control automático.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| Faltan las entidades de equilibrio | El controlador no publica ambos extremos de tensión de celda | Si existen **Maximum Cell Voltage** y **Minimum Cell Voltage** para esa batería |
| **Balance - Estado** permanece en `unknown` | No ha terminado una medición comparable de carga completa | **Activar reducción por voltaje al cargar al 100%**, el objetivo de carga y **Balance - Última Lectura** |
| El último valor parece mucho mayor o menor de lo habitual | La carga terminó de otra manera, o la medición no procedía de la misma condición cerca del final de carga | Compara la marca de tiempo y varias lecturas de carga completa terminadas |
| Un valor de Venus A/D parece incoherente con la tensión a nivel de batería | Los registros a nivel de batería pueden representar el pack 1 mientras que el diagnóstico usa los datos por pack disponibles | `packs_mV` y `worst_pack` en **Balance - Delta de Celda al 100% (última carga completa)** |
| El naranja o el rojo reaparecen tras otra carga completa | El desequilibrio podría ser persistente | **Balance - Tendencia**, el historial reciente y el blueprint de equilibrio activo |

??? "Detalles avanzados: interpretar mV y desequilibrio"
    **Por qué las mediciones se toman cerca de la carga completa**

    La tensión de una celda de fosfato de hierro y litio (LFP) se mantiene relativamente plana durante buena parte del rango de carga utilizable. En esa región, las diferencias de tensión son una prueba poco fiable de una diferencia de estado de carga. Cerca de la rodilla superior, las tensiones de celda se separan con más claridad y el sistema de gestión de batería (BMS) puede identificar y descargar ligeramente las celdas adelantadas. Por ello, Omnibattery compara lecturas estabilizadas cerca del final de carga en lugar de tratar una diferencia en directo a mitad de carga como un resultado de salud.

    **La curva de carga LFP en detalle**

    Una celda LFP típica de 3,2 V nominales sigue una curva en la que la tensión se mantiene casi plana durante la mayor parte del rango utilizable y solo se separa cerca del extremo superior:

    | Rango de SOC | Rango de tensión de celda | Pendiente |
    |---|---|---|
    | 0–10% | 2,50 V → 3,20 V | Rodilla de entrada pronunciada |
    | 10–90% | 3,20 V → 3,30 V | Casi plana — aproximadamente 1 mV por % de SOC |
    | 90–97% | 3,30 V → 3,45 V | Comienza una subida suave |
    | 97–99% | 3,45 V → 3,55 V | Rodilla: la tensión empieza a subir con fuerza |
    | 99–100% | 3,55 V → 3,65 V | Rodilla superior pronunciada: el acantilado de la carga completa |

    En la meseta, dos celdas que muestran tensiones casi iguales pueden diferir varios puntos porcentuales de SOC, por lo que una diferencia de tensión a mitad de carga no es una prueba útil de desequilibrio. También significa que el equilibrio pasivo no puede actuar ahí: el BMS descarga ligeramente la celda más alta mediante una resistencia y, para identificar qué celda es la más alta, necesita que la diferencia entre celdas supere el ruido de medida. Solo por encima de la rodilla las tensiones de celda se separan lo suficiente para que el BMS encuentre y descargue la celda adelantada; por eso los umbrales siguientes se sitúan en esa estrecha ventana cerca del final de carga, no en la zona plana intermedia.

    **Cómo crea Omnibattery una lectura comparable**

    Con **Activar reducción por voltaje al cargar al 100%** activado, la ruta de control entra en la zona de reducción gradual a 3,48 V y limita esa batería a 200 W. Una batería Venus E se detiene normalmente a 3,60 V, o antes cuando el BMS rechaza una orden de carga. La carga permanece entonces desactivada durante 60 segundos antes de que Omnibattery registre:

    ```text
    cell_delta_mV = (maximum_cell_voltage - minimum_cell_voltage) × 1000
    ```

    El enclavamiento de reducción gradual se libera después de que la celda de control baje de 3,44 V. Empezar y liberar a tensiones diferentes evita transiciones repetidas mientras la celda se relaja.

    Las baterías Venus A/D pueden contener packs acoplados, y sus registros máximo/mínimo a nivel de batería describen el pack 1 en lugar de todos los packs. Por tanto, alcanzar 3,60 V no detiene la carga ni inicia por sí solo la medición. Omnibattery mantiene la orden de 200 W hasta confirmar un corte del BMS, espera 60 segundos y luego registra la peor diferencia interna entre los packs que proporcionan datos válidos. Se excluyen las diferencias de tensión entre packs porque cada pack tiene su propio BMS.

    Un corte del BMS requiere una solicitud de carga real, una potencia entregada de 10 W o menos y Standby durante cinco ciclos de control consecutivos. Así se evita clasificar una batería inactiva como completa. El mismo corte puede activar una lectura estabilizada por debajo de 3,60 V cuando la batería permanece en la zona de reducción gradual.

    **Por qué se usan estos umbrales de tensión**

    | Umbral | Dónde se usa | Por qué este valor |
    |---|---|---|
    | 3,45 V | Referencia para el inicio de la rodilla superior | Aproximadamente donde la curva LFP abandona la meseta; por debajo, las tensiones de celda están demasiado próximas para distinguir un desequilibrio real |
    | 3,48 V | Activador para reducir gradualmente la carga a 200 W (`NORMAL_BALANCE_TAPER_CELL_VOLTAGE`) | Un pequeño margen por encima de la rodilla confirma que el pack está entrando realmente en la ventana de equilibrio, y no solo oscilando tras un escalón de carga, antes de reducir la potencia |
    | 3,44 V | Punto de liberación de la reducción gradual (`NORMAL_BALANCE_TAPER_EXIT_CELL_VOLTAGE`) | Empezar y liberar la reducción gradual a tensiones diferentes evita transiciones repetidas mientras la celda se relaja |
    | 3,60 V | Punto de medición superior; la carga se detiene y la integración espera 60 s antes de leer la diferencia (`NORMAL_BALANCE_PAUSE_CELL_VOLTAGE`) | Lo bastante alto para que el firmware de BMS compatible alcance su comportamiento nativo de final de carga, manteniendo margen respecto al límite de LFP; el BMS de la batería aún puede cortar antes |
    | 3,57 V | Tensión de reintento de recalibración del SOC | La celda debe relajarse de nuevo dentro de la ventana de equilibrio antes de iniciar el único reintento a 200 W |
    | 0,20 V (200 mV) | Límite de estado verde/amarillo (`BALANCE_THRESHOLD_YELLOW`) | Se fija por encima de la diferencia normal de fábrica cerca del final de carga, para que una pequeña discrepancia esperable no se interprete como un fallo |

    El blueprint opcional de equilibrio activo usa sus propios valores predeterminados configurables con mayor precisión: 3,49 V como punto de cambio a carga regulada y suelo de descarga entre reintentos, 3,40 V como su tensión de reintento más baja y 0,03 V (30 mV) como objetivo de finalización, ya que funciona fuera del bucle de control automático de la integración.

    **Estados y alertas**

    Las bandas de estado usan la diferencia bruta registrada: verde por debajo de 200 mV, amarillo desde 200 mV hasta menos de 230 mV, naranja desde 230 mV hasta menos de 250 mV y rojo desde 250 mV. Las lecturas naranja y roja crean una notificación persistente. Un resultado rojo en dos cargas completas consecutivas añade la advertencia de celda degradada.

    **Lógica de tendencia y notificaciones**

    Omnibattery conserva hasta 52 lecturas comparables y calcula la media mostrada y la tendencia a partir de las cuatro últimas. Un cambio de más de 2 mV por lectura es `rising`; de menos de −2 mV por lectura es `falling`; los valores entre esos límites son `stable`.

    La alerta de tendencia tiene en cuenta una referencia de fábrica de 180 mV. Se activa cuando la tendencia es ascendente y la media bruta de cuatro lecturas supera 220 mV. Las notificaciones de equilibrio de celdas tienen un periodo de espera de siete días por batería, por lo que una condición continua no crea una nueva notificación persistente en cada ciclo.

    **Por qué tarda tanto**

    El equilibrio activo de celdas es lento por dos motivos. La corriente de equilibrio pasivo es baja: un BMS LFP típico descarga ligeramente la celda más alta a través de una resistencia de equilibrio con una corriente de entre aproximadamente 30 mA y 150 mA, y los packs Marstek Venus se observan normalmente en el extremo bajo de ese intervalo, alrededor de 50 mA para una celda de 100 Ah, lo que elimina solo cerca del 0,05% de SOC por hora de la celda alta. Son estimaciones observadas en campo, no especificaciones fijas. La ventana de equilibrio también es estrecha: el BMS solo puede descargar mientras el pack está por encima de aproximadamente 3,45 V y la celda más alta es detectable frente al resto, de modo que una carga que alcanza el extremo superior y vuelve enseguida a descargar pasa allí solo unos minutos.

    Las observaciones de campo en packs reales son coherentes con esa aritmética: reducir la diferencia de celdas cerca del final de carga aproximadamente 5 mV suele requerir unas 24 horas acumuladas en el extremo superior de la ventana de equilibrio. Los desequilibrios mayores (50 mV o más) pueden necesitar varios días de sesiones repetidas de equilibrio en el extremo superior, y un pack que ha permanecido desequilibrado durante meses puede tardar una semana o más en recuperarse. Si ejecutas el blueprint de equilibrio activo para recuperar un pack visiblemente desequilibrado, déjalo funcionando durante la noche (o más tiempo) antes de comprobar el resultado: observar la diferencia en tiempo real no mostrará cambios en minutos.

    **Recalibración de SOC en Venus E**

    Una batería Venus E puede alcanzar 3,60 V mientras su estado de carga (SOC) comunicado permanece por debajo del 99%. Cuando sucede fuera del ciclo semanal, Omnibattery puede continuar a 200 W hasta que el BMS corte. Si el SOC sigue por debajo del 100%, espera a que la celda se relaje hasta 3,57 V y permite un intento más a 200 W. Esto solo crea las condiciones para recalibrar; el firmware del BMS decide si cambia el SOC mostrado.

    **Referencia de sensores y diagnóstico**

    Se crean seis entidades de diagnóstico solo cuando el controlador declara ambas lecturas de tensión de celda:

    | Patrón de entidad | Finalidad |
    |---|---|
    | `sensor.*_cell_delta` | Última diferencia comparable al final de carga en mV, `measured_at`, `soc_at_measurement`, historial reciente y desglose opcional por pack |
    | `sensor.*_cell_delta_live` | Tensión de celda máxima − mínima en vivo, en mV; no disponible mientras falte alguna de las dos lecturas |
    | `sensor.*_balance_status` | `green`, `yellow`, `orange`, `red` o `unknown` |
    | `sensor.*_delta_trend` | Dirección a lo largo de las lecturas comparables recientes |
    | `sensor.*_last_balance_read` | Marca de tiempo de la última lectura |
    | `sensor.*_delta_avg_4w` | Media de las cuatro últimas lecturas |

    Los valores se restauran después de reiniciar Home Assistant. **Estado de la Integración** muestra `normal_balance_protection` para un diagnóstico más profundo:

    | Atributo | Significado |
    |---|---|
    | `enabled` | Si está activada la reducción gradual de tensión de la batería |
    | `in_zone` | Si la tensión de su celda de control está en la zona de carga superior |
    | `max_cell_voltage` / `min_cell_voltage` | Extremos de tensión a nivel de batería en directo |
    | `delta_V` | Diferencia en directo en voltios |
    | `voltage_taper_latched` | Si está enclavada la reducción gradual cerca del final de carga |
    | `bms_cutoff_charge_active` | Si una batería con packs acoplados sigue pudiendo cargarse hasta el corte del BMS |
    | `bms_cutoff_measurement` | Si una medición posterior al corte está `pending` o `done` |
    | `soc_recal_active` | Si se ofrece a un SOC comunicado bajo un corte propiedad del BMS |
    | `soc_recal_bms_cutoff` | Si se ha alcanzado ese corte |
    | `soc_recal_retry_pending` / `soc_recal_retry_active` | Estado del reintento único |
    | `soc_recal_first_cutoff_voltage` | Tensión más alta observada durante el primer corte |
    | `charge_limit_w` | Límite de carga efectivo por batería antes del reparto |

    Estos atributos explican la ruta de control actual; **Balance - Estado** sigue siendo el resultado de salud mostrado al usuario.

    El blueprint de equilibrio activo se ejecuta fuera del bucle de control normal de Omnibattery mediante **Manual Battery Control**. Su propia página es la referencia canónica de su secuencia de carga, reposo, reintentos y limpieza. Las mediciones estabilizadas publicadas por el blueprint entran en el mismo historial de **Balance - Delta de Celda al 100% (última carga completa)** con `source: blueprint`.
