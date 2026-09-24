# Entender el plan de batería de hoy

Usa la cronología diaria de operación para ver cómo Omnibattery ha aprovechado el sol y protegido tu instalación hoy, y qué espera hacer después. Combina energía medida de casa y solar con acciones de batería y el plan de carga activo; es una vista de diagnóstico y no controla la batería.

## ¿Lo necesito?

**Úsala si** quieres entender por qué la batería cargó, descargó, esperó al sol o dejó sin usar un periodo de carga desde red.

**No la necesitas** para operar Omnibattery. El controlador sigue funcionando con el panel cerrado, y abrir la tarjeta no activa una decisión de control.

## Antes de empezar

- Instala el panel lateral de Omnibattery y deja que la integración recopile datos del día actual.
- Configura el [aprendizaje de consumo de casa](consumption-estimate.md) y una fuente solar para obtener las curvas más completas.
- Configura carga predictiva por Precio dinámico o Franja horaria si quieres ver decisiones futuras de carga desde red en la cronología. Precio en tiempo real registra las activaciones cuando suceden y, de forma intencionada, no tiene calendario futuro.

## Cómo activarlo

La cronología es automática; no hay interruptor que activar.

1. Abre el panel lateral de Omnibattery.
2. Selecciona **Overview** y busca **Daily Operation Timeline**.
3. Desplázate sobre una celda para abrir sus detalles. En dispositivos táctiles, toca el intervalo.
4. Usa la flecha de navegación derecha o el desplazamiento horizontal para ver la mañana local siguiente.

## Qué verás

El primer día local se divide en celdas de quince minutos. Las curvas continuas son medidas y las discontinuas, previstas. La energía solar y de consumo usan el eje de energía; el estado de carga total de la batería (SOC) usa el eje de porcentaje.

| Color o marcador | Significado | Cómo interpretarlo |
|---|---|---|
| Verde | El sol cargó la batería, o el plan futuro se lo asigna | Flujo pasado confirmado o flujo futuro planificado |
| Morado | Decisión de carga desde red | Una carga desde red planificada u observada |
| Azul | Descarga de batería | Energía suministrada desde la batería |
| Gris | Se consideró la carga desde red, pero no era necesaria | Una decisión explícita de no cargar, no datos ausentes |
| Amarillo suave | El excedente solar podría cargar la batería | Una oportunidad; permanece amarillo hasta que se observe la carga |
| Marcador de reloj | Está activo Solar Charge Delay | La información emergente incluye la hora estimada de liberación |

Los intervalos cerrados muestran datos observados. El intervalo abierto combina la energía medida hasta ahora con un resto proyectado, mientras que las celdas futuras son proyecciones informativas de los perfiles activos de consumo y solar, el estado de batería y el plan seleccionado. La información emergente etiqueta los valores observados y proyectados y muestra la energía que entró o salió de la batería.

En pantallas estrechas, desplázate horizontalmente por hora. La vista inicial se mantiene en el día local actual; usa la flecha derecha para revelar las `12 hours` adicionales hasta el mediodía local siguiente. Las flechas del teclado, el toque y el ratón muestran los mismos detalles de intervalo.

La cronología depende de la [estimación de consumo](consumption-estimate.md), [Solar Charge Delay](solar-charge-delay.md), [Precio dinámico](../configuration/predictive-charging/dynamic-pricing.md), [Franja horaria](../configuration/predictive-charging/time-slot.md) y [Precio en tiempo real](../configuration/predictive-charging/real-time-price.md).

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| Las celdas futuras están vacías | Una previsión no está disponible o está obsoleta, o el modo Precio en tiempo real no tiene programación futura por diseño | Fuentes de perfiles, estado del plan y modo predictivo seleccionado |
| Las celdas pasadas están vacías tras abrir el panel | Recorder no tiene historial utilizable o falta telemetría del día actual | Recorder, entidades de red/solar y disponibilidad de la integración |
| Una celda amarilla no se volvió verde | Se proyectó carga solar pero no entró energía en la batería | Límites de batería, SOC y la información emergente del intervalo |
| Una celda muestra más de una acción | Los flujos sucedieron simultáneamente | El patrón diagonal y el texto accesible de la información emergente |
| La celda actual difiere del resultado final | Su parte sin terminar sigue proyectada | Vuelve a comprobarlo cuando se cierre el cuarto de hora |
| Los datos ausentes aparecen como un hueco | Omnibattery conserva los valores no disponibles | Disponibilidad de las entidades de fuente; los valores ausentes no se convierten en cero |

??? "Detalles avanzados"
    La tarjeta contiene `96` celdas fijas de `15-minute` para el día local y hasta `48` celdas de ampliación para las siguientes `12 hours`, con un horizonte visible máximo de `144` celdas. Las curvas de energía usan `kWh/15 min`; el SOC usa un eje de `0–100%`.

    La decisión gris de no cargar se publica internamente como `grid_charge_not_needed`. Una celda puede contener hasta `3` acciones simultáneas. Los patrones diagonales y el texto accesible conservan cada acción en temas claros y oscuros. Las acciones solo se combinan cuando se solapan en el tiempo. Si la batería cambia de dirección dentro de un intervalo, se muestra la acción presente durante más tiempo. El intervalo actual nunca etiqueta como observada una acción proyectada para sus minutos restantes. `Charging to setpoint` es contexto y no un color de acción independiente. El Balance neto horario añade su propia leyenda y marcador de causa solo mientras esa función está activada.

    El SOC observado se conserva en el diario diario y se completa desde Home Assistant Recorder al abrir el panel. La información emergente nombra la forma solar aprendida o la alternativa sinusoidal y muestra la energía de carga/descarga observada o proyectada para el intervalo.

    La entidad de diagnóstico es `sensor.omnibattery_daily_operation_timeline`; su estado es la fecha local de la instantánea. Los atributos incluyen `schema_version`, zona horaria, frescura, fuentes de perfiles, series energéticas de `96` valores, SOC total observado y proyectado, máscaras de operación, decisiones de red y metadatos de retraso. La ampliación se publica por separado como `extended_horizon` y `extended_projection`, limitada a `48` intervalos y excluida de Recorder.

    La simulación de previsión usa entradas inmutables y desacopladas, y no tiene efectos secundarios de control. Los diagnósticos predictivos pertenecen al ciclo de precios y se restauran mediante él, no al renderizar la entidad. Los cuartos de hora completados permanecen inmutables; una reevaluación del plan solo puede reemplazar el intervalo abierto y los intervalos futuros. Los datos almacenados se restauran solo para la misma fecha local y huella temporal. Los datos corruptos se degradan a una cronología vacía y nunca bloquean el control de batería.

    La telemetría ausente permanece como `null` y no se convierte en cero. Si una previsión no está disponible u obsoleta, el pasado observado sigue visible y solo los valores futuros no compatibles pasan a no estar disponibles.
