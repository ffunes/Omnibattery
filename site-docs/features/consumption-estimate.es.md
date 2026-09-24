# Aprende el consumo de tu hogar

Aprovecha al máximo la energía solar dejando que Omnibattery aprenda cuánta energía usa tu hogar y cuándo la usa. La estimación es automática; ayuda a la carga predictiva y al [Retraso de carga solar](solar-charge-delay.md) a decidir si la energía almacenada y la solar prevista pueden cubrir la demanda.

## ¿Lo necesito?

**Úsalo si** empleas la carga predictiva o el Retraso de carga solar y quieres que estas funciones tengan en cuenta la rutina habitual de tu hogar.

**No necesitas gestionarlo** cuando tu consumo reciente es representativo. Intervén solo ante vacaciones, fallos del medidor o días inusuales que no deban influir en previsiones futuras.

## Antes de empezar

- Configura un sensor de red y una batería que funcionen. Omnibattery deriva la demanda del hogar de las mismas mediciones que usa el diagrama de flujo de energía.
- Mantén activado el Recorder de Home Assistant si quieres recuperar días recientes que falten tras la instalación o un reinicio.
- Los [dispositivos excluidos y adicionales](../configuration/excluded-devices.md) son opcionales. Sus ajustes de potencia configurados se incluyen en el aprendizaje.

## Cómo activarlo

Esta función es automática; no hay ningún interruptor para activar el aprendizaje.

1. Abre el panel lateral de Omnibattery y comprueba que **Consumo de la casa** sigue la carga de tu hogar.
2. Deja que la integración recopile consumo diario representativo. **Perfil de consumo esperado de la casa** muestra cuándo está listo el perfil aprendido.
3. Activa **Modo vacaciones** en el dispositivo Sistema Omnibattery mientras se interrumpa tu rutina habitual. Desactívalo cuando el hogar vuelva a la normalidad.
4. Para eliminar un día inusual pasado, abre **Herramientas para desarrolladores → Acciones**, ejecuta **Omnibattery: Excluir días de consumo** y elige la primera y la última fecha local que ignorar.

![Atributos del aprendizaje de consumo](../assets/screenshots/features/consumption-estimate-attributes.png){ width="700" style="display: block; margin: 0 auto;"}

## Qué verás

- **Consumo de la casa** muestra la potencia instantánea del hogar utilizada para aprender.
- **Consumo diario de la casa** acumula la energía de hoy del hogar y se reinicia a la medianoche local.
- **Perfil de consumo esperado de la casa** muestra la previsión de hoy y si su fuente es el perfil aprendido maduro o una estimación alternativa.
- **Captura actual del perfil de consumo** muestra cuánta demanda de hoy se ha capturado hasta ahora.
- **Modo vacaciones** informa de la línea base fija de vacaciones mientras el aprendizaje está en pausa.

La integración aprende tanto un total diario como un patrón por hora del día. Un patrón maduro permite que Precio dinámico reserve energía antes de un pico de demanda temprano, manteniendo flexible por precio la demanda posterior hasta el siguiente amanecer. El Retraso de carga solar usa la misma previsión de demanda restante, de modo que la demanda ya observada hoy no se cuenta dos veces.

Consulta la [cronología diaria de funcionamiento](daily-operation-timeline.md) para comparar el consumo aprendido con las acciones de batería previstas para hoy.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| La previsión aún usa una alternativa | El perfil por hora no ha reunido cobertura reciente suficiente | **Perfil de consumo esperado de la casa** y sus atributos `source`, `mature` y `coverage_ratio` |
| El Consumo de la casa no es plausible | Una medición de red, solar, batería o dispositivo excluido tiene el signo incorrecto o no está disponible | El diagrama de flujo de energía y las entidades de origen |
| Un día inusual sigue afectando al aprendizaje | Se registró antes de activar Modo vacaciones | Ejecuta **Omnibattery: Excluir días de consumo** para esa fecha local |
| No aparece historial reciente tras un reinicio | Recorder no tiene historial utilizable de Consumo de la casa | Retención de Recorder, disponibilidad de entidades y diagnósticos de la integración |
| Modo vacaciones informa de una línea base genérica | Aún no ha observado suficientes datos nocturnos válidos | Mantén el modo activo durante noches representativas e inspecciona la fuente de su línea base |

??? "Detalles avanzados"
    ### Qué cuenta como consumo del hogar

    Omnibattery deriva la potencia instantánea del hogar de las mediciones que ya tiene:

    ```text
    home = grid + Σ(battery AC power) + solar
    ```

    La energía solar de corriente continua (CC) conectada mediante una entrada de seguimiento del punto de máxima potencia (MPPT) ya está neteada en la potencia de corriente alterna (CA) de la batería y no se añade dos veces. Durante la carga desde la red, la potencia de CA negativa de la batería cancela la importación de red correspondiente. Por ejemplo, importar `2.8 kW` mientras la batería carga a `2.5 kW` produce `0.3 kW` de demanda del hogar.

    Una instalación antigua puede conservar un `household_consumption_sensor`. Omnibattery lo lee directamente solo si no se ha configurado ningún sensor de producción solar; con un sensor solar, se prefiere el valor derivado. Este campo ya no se ofrece durante la configuración. El valor derivado también se expone como `sensor.marstek_venus_system_home_consumption`.

    Antes de acumular, se resta un dispositivo excluido con `included_in_consumption = true` porque ya aparece en la lectura de red/hogar. Se añade un dispositivo adicional con `included_in_consumption = false` porque esa carga no aparece en la lectura. Consulta los [dispositivos excluidos](../configuration/excluded-devices.md).

    ### Total diario y estimación heredada

    Cada muestra de control aporta energía usando el tiempo real transcurrido:

    ```text
    increment (kWh) = home_power (W) × elapsed_time (s) / 3,600,000
    ```

    Las franjas de carga no pausan el aprendizaje. A las `23:55` hora local, Omnibattery guarda el acumulador de todo el día si es de al menos `1.5 kWh`; después el acumulador se reinicia a medianoche. Conserva las `7` entradas diarias más recientes y calcula su media:

    ```text
    expected_consumption = Σ(daily_consumption) / number_of_days
    ```

    Las entradas que faltan al inicio usan `5.0 kWh` hasta que el relleno de Recorder o una captura real las reemplaza. El relleno integra **Consumo de la casa** de cada día local que falta, incluidos los ajustes de dispositivos excluidos/adicionales. Los historiales de versiones antiguas que solo cubrían franjas de carga se descartan y reconstruyen para que nunca se mezclen totales de días parciales y completos.

    Ejemplo:

    ```text
    Daily totals: 5.0, 5.1, 5.3, 4.8, 4.9, 6.3, 6.0 kWh
    Expected consumption = 37.4 / 7 = 5.34 kWh
    ```

    El valor en curso de todo el día también se expone como `household_consumption_full_day_kwh` en `binary_sensor.marstek_venus_system_predictive_charging_active` y persiste tras reinicios del mismo día. Sus atributos incluyen el historial diario y los recuentos de entradas reales y alternativas. En modo Precio dinámico, `energy_horizon_end` identifica el límite del amanecer local y `overnight_consumption_kwh` informa de la demanda prevista entre medianoche y ese límite.

    **Red en SOC mínimo** (`sensor.marstek_venus_system_daily_grid_at_min_soc_energy`) se reinicia a medianoche local e informa de la energía de red importada mientras todas las baterías estaban en su estado de carga (SOC) mínimo durante una franja de descarga. Es solo diagnóstico y no se añade a la estimación porque el consumo derivado del hogar ya incluye esa demanda.

    ### Perfil aprendido por hora del día

    Omnibattery conserva hasta `28` días locales en `96` intervalos de quince minutos. Integra muestras con una regla trapezoidal entre límites de intervalo, medianoche y cambios de horario de verano. Un hueco de muestra superior a `5 minutes` rompe la continuidad; un intervalo necesita al menos `675 seconds` (`75%`) de cobertura para ser utilizable.

    El perfil prefiere el día de la semana coincidente, después el tipo laborable/fin de semana coincidente y después todos los días utilizables. Las muestras se ponderan por antigüedad con `1.0`, `0.75`, `0.5` y `0.25`. Un rango solicitado solo es maduro si tiene al menos `7` días válidos; al menos `2` muestras coincidentes para el `75%` de sus intervalos; al menos `80%` de cobertura total; y una muestra más reciente de no más de `7` días.

    Hasta entonces, Omnibattery distribuye la estimación diaria sobre una curva temporal provisional con forma de hogar: la demanda más baja de `00:00–06:00`, un aumento de desayuno, mayor demanda diurna y el pico de cena más intenso. La curva se normaliza para conservar el total diario exacto, incluso en días de cambio de horario de verano. Las previsiones ajustan el tramo restante de hoy después de las primeras `3 hours`, alcanzan el ajuste completo a las `12:00` y limitan esa corrección al `30%` de la previsión restante de hoy para que un único pico no elimine la demanda posterior esperada. El tramo posterior a medianoche usa el perfil del día siguiente o la tasa horaria histórica cuando el perfil no está disponible.

    Durante Modo vacaciones, los días de calendario afectados se omiten del historial diario y solo los cuartos de hora afectados se omiten del perfil. Las previsiones usan la carga mediana de las últimas `3` noches válidas de `01:00–05:00`, donde una noche necesita `3 hours` de cobertura. Antes de disponer de ello, el orden alternativo es el perfil nocturno aprendido, el historial diario distribuido a lo largo del día y, por último, la estimación predeterminada. Conmutar el interruptor rompe la continuidad de las muestras para que un intervalo nunca se atribuya a ambos lados del cambio de modo.

    **Excluir días de consumo** acepta fechas de los últimos `35` días, las elimina del historial diario y las enmascara del perfil sin modificar contadores físicos ni Recorder. La misma acción se puede llamar como:

    ```yaml
    action: omnibattery.exclude_consumption_days
    data:
      start_date: "2026-09-07"
      end_date: "2026-09-07"  # optional; defaults to start_date
    ```

    Eliminar manualmente un día de `.storage` no lo excluye: el relleno lo trata como ausente y lo restaura.

    Un perfil inmaduro recurre a la estimación diaria heredada o a una estimación de tasa actual, según la función solicitante. El relleno de Recorder se ejecuta en segundo plano con una consulta por cada origen configurado. Los datos de perfil sin procesar están aislados en `omnibattery.<entry_id>.consumption_profile`. Cambiar un origen o ajuste de carga rompe la continuidad de las muestras y vuelve a llenar los días ausentes. Un cambio de zona horaria de Home Assistant vuelve a agrupar los días almacenados; las dos fechas de los extremos conservan sus horas parciales y se recuperan de nuevo. Solo una zona horaria almacenada que ya no existe fuerza un perfil nuevo.

    `sensor.omnibattery_expected_home_consumption_profile` expone la previsión por intervalos y horas, el origen, la madurez, la cobertura y los metadatos alternativos. `sensor.omnibattery_consumption_profile_capture` expone la captura sin procesar de hoy mediante `hourly_capture_kwh`, `interval_capture_kwh` e `interval_coverage_s`; se reinicia el siguiente día local. Los diagnósticos de la integración incluyen un resumen de aprendizaje acotado por día.

    Este perfil de hogar es independiente del perfil temporal solar. El aprendizaje doméstico estima demanda absoluta por hora local; el perfil solar aprende una forma normalizada de luz a partir de potencia solar directa. Ninguno cambia el presupuesto energético de la previsión.
