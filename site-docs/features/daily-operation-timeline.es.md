# Línea temporal de operación diaria

La pestaña Resumen incluye una línea temporal de 24 horas con 96 celdas fijas
de quince minutos. La previsión continúa además hasta las 12:00 del día local
siguiente (48 celdas adicionales), inicialmente fuera de la vista. Las celdas
describen acciones de las baterías y las curvas superpuestas muestran energía
solar y del hogar en `kWh/15 min`.

## Cómo leer la tarjeta

- Los intervalos cerrados son datos observados. El intervalo actual contiene la
  energía real acumulada y una proyección del resto hasta cerrar el cuarto de
  hora.
- Los intervalos futuros son una proyección informativa construida con los
  perfiles activos de consumo y solar, el estado actual de las baterías y el
  plan ya seleccionado de Precio Dinámico o Franja horaria.
- Precio en Tiempo Real no inventa un calendario futuro: conserva solo sus
  activaciones reales del pasado y del intervalo actual.
- Las curvas continuas son medidas y las discontinuas son previstas. Solar y
  consumo usan el eje izquierdo en `kWh/15 min`; el SOC total usa el eje derecho
  de `0–100 %`. El tooltip muestra el SOC observado o proyectado del intervalo e
  identifica la forma solar aprendida o el fallback sinusoidal activo. El diario
  conserva el SOC observado y el panel completa el día desde Recorder al abrirse.

Los colores de acción representan flujos, no permisos: verde significa energía
solar que ha cargado la batería o que el plan futuro le asigna, morado una
decisión de carga desde la red, azul una descarga observada o proyectada y gris
una decisión explícita de `grid_charge_not_needed`. Un sombreado amarillo tenue
indica una ventana con excedente solar disponible en la que la batería podría
cargar, pero sin mostrar esa posibilidad como una carga efectiva. En el cuarto
de hora actual, una carga solar prevista permanece amarilla hasta que se observa
energía entrando en la batería. Una celda puede contener hasta tres acciones; los
patrones diagonales y el texto accesible mantienen la diferencia con cualquier
tema. Solo se combinan visualmente cuando fueron simultáneas; si la batería
cambió de sentido dentro del cuarto de hora, se muestra la acción observada
durante más tiempo. El intervalo actual nunca presenta como observada una acción
que solo está proyectada para sus minutos restantes. «Carga hasta el setpoint»
es contexto, no otro color. El Retraso de
Carga usa un reloj y una hora estimada de desbloqueo. El tooltip muestra también
la energía que ha entrado o salido realmente de la batería durante una franja
observada. Para una franja futura muestra la carga o descarga proyectada.
Los patrones diagonales indican acciones realmente simultáneas y son
independientes del balance neto horario; su leyenda y marca de causa solo se
muestran cuando la función está activada.

## Contrato de la entidad

La entidad de diagnóstico es
`sensor.omnibattery_daily_operation_timeline`. Su estado es la fecha local del
snapshot. Los atributos contienen `schema_version`, zona horaria, frescura,
fuentes de perfiles, series energéticas de 96 valores, máscaras de operación,
SOC total observado y proyectado, decisiones de red y metadatos del retraso.
Las series diarias y las listas de operación mantienen 96 valores para
conservar el contrato existente. La ampliación se publica aparte en
`extended_horizon` y `extended_projection`, limitada a 48 intervalos y
excluida de Recorder; consultar la entidad desde el dashboard no provoca
reevaluaciones del control. Su previsión se genera con entradas inmutables y
desacopladas mediante un simulador sin efectos laterales. Los diagnósticos
predictivos pertenecen al ciclo de precios y nunca se restauran al renderizar
esta entidad.

El backend deja inmutables los cuartos cerrados. Una reevaluación solo puede
sustituir el intervalo actual abierto y el futuro. La restauración del Store
solo se acepta para la misma fecha local y huella temporal; un Store corrupto
produce una línea vacía y nunca bloquea el control de las baterías.

## Móvil y datos ausentes

En pantallas estrechas las 144 celdas conservan un ancho mínimo legible y se
desplazan horizontalmente por hora. La vista inicial se queda en las primeras
24 horas; pulsa la flecha de desplazamiento hacia la derecha para revelar las
12 horas adicionales. Las flechas del teclado, el toque y el ratón muestran los mismos detalles. La telemetría ausente permanece como
`null`, no se convierte silenciosamente en cero. Si una previsión no está
disponible u obsoleta, la tarjeta conserva el pasado observado y marca solo el
futuro que ya no puede justificarse.

Consulta [estimación de consumo](consumption-estimate.es.md),
[retraso de carga solar](solar-charge-delay.es.md), [Precio Dinámico](../configuration/predictive-charging/dynamic-pricing.es.md),
[Franja horaria](../configuration/predictive-charging/time-slot.es.md) y
[Precio en Tiempo Real](../configuration/predictive-charging/real-time-price.es.md)
para conocer las fuentes de la proyección.
