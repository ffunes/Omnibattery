# Plan de implementación: Smart Pre-discharge desde el día anterior (#325)

## Decisión

La [discusión #325](https://github.com/ffunes/Omnibattery/discussions/325)
es una **feature request válida e interesante**, con prioridad recomendada
**media**.

La propuesta identifica una limitación real: Smart Pre-discharge solo construye
un plan para el día local de la evaluación. El plan normal nace a las `00:05`,
por lo que no puede seleccionar una franja cara de la tarde o noche anterior
para crear el hueco que necesitará la producción solar del día siguiente. En
mercados como Nord Pool esto puede desplazar la predescarga desde el pico
vespertino hacia una franja mucho menos valiosa después de medianoche.

No es un duplicado de las discusiones #270 o #273:

- #270 propone forzar exportación ante picos de precio;
- #273 propone una descarga predictiva general;
- #325 conserva el propósito anti-vertido existente: descargar solo lo
  necesario para reservar hueco para una ventana solar concreta.

El interés afecta a un subconjunto concreto —Precio Dinámico, Smart
Pre-discharge, precios del día siguiente y previsión solar—, no a «la mayoría de
usuarios» como afirma el formulario. El hilo tiene por ahora un solo voto y no
aporta más casos independientes, así que el impacto comunitario aún no está
demostrado. Aun así, la mejora encaja con la arquitectura y corrige una frontera
temporal artificial sin añadir un nuevo modo de control.

La condición técnica imprescindible es disponer de una **previsión solar
fechada para mañana**. La previsión de hoy o «restante hoy» no se puede reutilizar
como aproximación: podría ordenar una exportación innecesaria y degradar tanto
el SOC como el coste del usuario.

## Objetivo

Cuando estén disponibles los precios y la previsión solar del día `D`, permitir
que Smart Pre-discharge prepare durante `D-1` un plan cuyo primer plazo sea la
primera ventana de riesgo solar de `D`.

El planner debe poder elegir franjas elegibles entre el momento de evaluación y
esa primera ventana, incluso si pertenecen al día anterior, manteniendo la misma
prioridad actual:

1. seguridad, ownership y suelos de SOC;
2. protección de ventanas solares con precio de inyección desfavorable;
3. creación de solo el hueco necesario;
4. selección de las franjas de predescarga de mayor valor;
5. uso del hueco sobrante para carga oportunista, sin consumir la reserva solar.

Ejemplo esperado:

```text
D-1 16:00  llegan los precios y la previsión de D
D-1 21:00  pico de precio: se crea el hueco necesario
D  00:05   se recalcula el mismo objetivo con SOC y previsión actualizados
D  11:00   ventana solar de inyección negativa: se protege el hueco
```

La evaluación de `D-1` es provisional. La evaluación diaria de `D` a las
`00:05` sigue siendo autoritativa y ajusta o cancela el plan según el SOC, los
precios y la previsión vigentes.

## Diagnóstico del comportamiento actual

### Horizonte limitado al día local

`PricingManager._curtailment_plan_slots()` conserva únicamente slots futuros
cuyo `slot.start.date()` coincide con `now.date()`. Aunque algunos proveedores
ya expongan `raw_tomorrow` o un calendario ampliado, esos slots se eliminan antes
de llamar al planner.

`plan_curtailment()` sí sabe ordenar candidatos por precio y exige que terminen
antes de la primera ventana de riesgo. La carencia principal no está en el
selector puro, sino en los datos que recibe y en la ausencia de una fecha
objetivo explícita.

### La reevaluación vespertina tiene otro propósito

`_evaluate_evening_recharge()` calcula exclusivamente el déficit doméstico
restante hasta la medianoche actual. Puede añadir carga barata para evitar que
la batería se agote esa noche, pero:

- no busca riesgo solar de mañana;
- no se ejecuta si la batería ya está suficientemente llena;
- no construye un plan de predescarga;
- trunca expresamente los slots en la medianoche, porque una carga posterior no
  puede cubrir consumo anterior.

La nueva evaluación anti-vertido debe tener un ciclo propio. No debe alterar el
horizonte correcto de la recarga vespertina.

### La previsión solar no representa mañana

El contrato actual distingue `today` y `remaining today`. El valor escalar
seleccionado se usa para el día en curso. Puede conservar periodos fechados en
`SolarForecastInput`, pero no existe una entrada de configuración ni un lector
que garantice el total solar del día siguiente.

El perfil solar aprendido o la curva sinusoidal aportan la **forma temporal**,
pero no pueden inventar el total de kWh de mañana. Por eso no son un sustituto de
una previsión energética fechada.

### No todos los proveedores cargan precios de mañana

- HACS Nord Pool puede aportar `raw_today` y `raw_tomorrow`.
- Tibber ya solicita un horizonte que incluye hoy y mañana.
- ENTSO-e y otros sensores pueden incluir ambas fechas si su entidad las
  publica.
- El camino de Nord Pool oficial solo solicita actualmente la fecha local
  actual y su caché no contiene mañana.

La implementación debe tratar la disponibilidad como una capacidad del
proveedor, no asumirla por el tipo seleccionado.

### El reset de medianoche elimina el plan

El reset diario limpia `_curtailment_plan` junto al calendario de Precio
Dinámico. Un plan creado en `D-1` debe conservar sus ventanas de riesgo de `D`
hasta que la evaluación de las `00:05` lo reemplace. Los overrides ya finalizados
sí deben limpiarse durante el cambio de fecha.

### La adaptación solar en vivo usa el día actual

La reserva se ajusta con el acumulador solar diario. Antes de la fecha objetivo,
ese acumulador pertenece a `D-1` y no se puede comparar con la previsión de `D`.
El plan adelantado debe mantener su reserva prevista intacta hasta que
`now.date() == target_date`.

## Alcance

### Incluido

- previsión solar explícita y fechada para el día siguiente;
- lectura de precios hasta el final del día objetivo;
- plan anti-vertido con `target_date` y horizonte que cruza medianoche;
- candidatos de predescarga en `D-1` y `D`, siempre antes del primer riesgo;
- preparación/reintento automático cuando se publiquen los datos de mañana;
- convivencia segura con el plan todavía activo de `D-1`;
- continuidad controlada durante la medianoche;
- reconstrucción autoritativa a las `00:05` de `D`;
- soporte de reinicio, reevaluación manual y cambios de opciones;
- diagnóstico, traducciones, documentación y pruebas.

### Fuera de alcance

- descarga especulativa general o venta forzada por picos de precio (#270 y
  #273);
- previsión multi-día más allá de mañana;
- estimar los kWh solares de mañana a partir de los de hoy;
- persistir planes de control en disco;
- añadir una hora configurable de evaluación;
- controlar directamente un inversor fotovoltaico;
- relajar SOC, potencia, ownership, franjas del usuario o protecciones actuales.

## Contrato temporal y de previsión

### Fecha objetivo explícita

Ampliar `CurtailmentPlan` con metadatos estables:

```text
target_date
planning_horizon_start
planning_horizon_end
forecast_source
forecast_date
```

`plan_curtailment()` recibirá `target_date` y mapas de solar/consumo por slot.
Solo un slot de `target_date` podrá convertirse en `risk_slot`; los slots
anteriores seguirán siendo candidatos de predescarga si terminan antes del
primer riesgo.

Esto evita depender de la fecha implícita de `now` y permite probar el planner
sin Home Assistant.

### Previsión solar de mañana

Añadir la clave opcional `solar_forecast_tomorrow_sensor`, validada en `kWh` o
`Wh` igual que las entradas actuales. No sustituye ni elimina `today` o
`remaining today`.

Crear un lector orientado a fecha, conceptualmente:

```text
read_solar_forecast_for_date(hass, controller, target_date)
    -> TargetDaySolarForecast | None
```

Prioridad de fuentes para `target_date == today + 1`:

1. periodos explícitos, fechados y completos para el día objetivo;
2. sensor escalar «forecast tomorrow», con sus periodos si los ofrece;
3. sin previsión válida: no se ejecuta predescarga adelantada.

No se mezclan periodos incompletos con otra fuente. Si solo existe un total
escalar, la forma se obtiene mediante la prioridad ya usada por el timeline
solar: perfil aprendido maduro y después curva sinusoidal. El total del sensor
sigue siendo autoritativo.

El contrato debe publicar tanto la fecha como la procedencia para que un sensor
que no haya avanzado de día no pueda reutilizar silenciosamente datos
obsoletos.

### Consumo del horizonte

Construir `consumption_by_slot` con el perfil de 96 intervalos para cada fecha
local comprendida entre `now` y el final de `target_date`. Si el perfil no es
maduro, repetir la curva provisional diaria existente; no repartir un único
total diario uniformemente sobre un horizonte de más de 24 horas.

El consumo del día objetivo se usa para calcular excedente solar dentro de las
ventanas de riesgo. El consumo de `D-1` solo determina cuánta predescarga es
físicamente posible en modo «Solo autoconsumo».

### Solar por slot

Reutilizar `pricing/solar_timeline.py` para construir la curva del día objetivo
y proyectarla sobre los slots de precio mediante solape temporal. Todos los
slots de `D-1` deben recibir `0 kWh` de la previsión de `D`.

La construcción debe usar datetimes locales fechados y duraciones absolutas,
incluidos días DST de 23 o 25 horas. No se debe aplicar una función acumulativa
de hora del día a un bloque multi-día sin separar primero las fechas.

## Horizonte de precios

Solicitar y parsear desde `now` hasta la medianoche posterior a `target_date`.
Después validar que exista cobertura de precio para las zonas relevantes:

- al menos un candidato futuro antes del primer riesgo, y
- slots del día objetivo capaces de identificar la ventana de riesgo.

La ausencia de precios de mañana produce `waiting_tomorrow_prices`; no borra un
plan válido del día actual ni activa ningún override.

### Nord Pool oficial

Extender la caché del servicio para solicitar por separado hoy y mañana cuando
mañana ya pueda estar publicado. La clave de caché debe incluir las fechas
solicitadas. Las respuestas se normalizan y fusionan sin duplicar slots.

Un fallo al pedir mañana no debe destruir los slots válidos de hoy. El estado de
salud distinguirá entre:

- precios actuales utilizables;
- precios de mañana aún no publicados;
- error real o datos mal formados.

### Otros proveedores

Mantener los parsers existentes. El nuevo flujo inspecciona los timestamps
normalizados y continúa únicamente si cubren `target_date`. Tibber, HACS Nord
Pool y sensores con atributos de mañana no necesitan un tratamiento de control
distinto.

## Ciclo de vida del plan

### 1. Preparación automática

Iniciar intentos cuando se cumpla todo lo siguiente:

- Smart Pre-discharge y Precio Dinámico están activos;
- `target_date == today + 1`;
- el proveedor ya expone precios de `target_date`;
- existe una previsión solar válida para `target_date`;
- aún queda una franja elegible antes del primer riesgo.

No hace falta una hora configurable. El control puede empezar a comprobar la
disponibilidad a partir de las `13:00`, con un cooldown de 30 minutos hasta las
`23:00`. Un intento sin datos no consume definitivamente el pase del día.

Una vez construido, guardar un fingerprint de:

- fecha objetivo;
- timestamps y precios relevantes;
- total, periodos y fuente solar;
- huella del perfil de consumo;
- umbral, margen, reserva y modo de exportación.

No reconstruir si el fingerprint y el headroom material no han cambiado.

### 2. Plan preparado y plan activo

Mantener como máximo dos referencias en memoria:

- `_curtailment_plan`: plan que gobierna el runtime actual;
- `_curtailment_next_day_plan`: plan preparado para mañana.

El plan actual conserva prioridad mientras tenga una ventana de riesgo vigente
o futura en `D-1`. Los slots de esa ventana se pasan como reservados al planner
de mañana, por lo que el nuevo plan nunca programa predescarga sobre una
protección activa.

Cuando finaliza el último riesgo del plan actual:

1. reconstruir el plan preparado con SOC/headroom en vivo;
2. promoverlo a `_curtailment_plan`;
3. limpiar solo los overrides del plan anterior;
4. permitir que sus candidatos de `D-1` entren en ejecución.

Si el plan actual está en fail-safe y no se conoce con seguridad el final de la
solar de `D-1`, no se permite la predescarga adelantada hasta el final solar
estimado. La seguridad del día en curso prevalece sobre el valor económico.

### 3. Ejecución

La ruta runtime no cambia de autoridad:

- `_current_predischarge_slot()` encuentra candidatos de cualquier fecha;
- el objetivo se recalcula con headroom y SOC actuales;
- se detiene en cuanto el hueco requerido es suficiente;
- se mantienen reserva de SOC, potencia, bloqueos y los tres modos de
  exportación;
- ninguna predescarga puede solaparse con un `risk_slot`, un slot reservado de
  carga o una franja del usuario que prohíba descargar.

El ajuste por producción solar real solo se habilita en `target_date`. Antes de
esa fecha, la reserva sigue la previsión; después, conserva el comportamiento
adaptativo actual.

### 4. Medianoche

Separar el reset del calendario de carga del ciclo de vida anti-vertido:

- limpiar el slot activo, targets y overrides que hayan terminado;
- conservar un plan cuyo `target_date == today`;
- promover el plan preparado si aún no se había promovido;
- eliminar planes con `target_date < today`;
- mantener la reserva prevista hasta la reevaluación de las `00:05`.

No persistir el plan en disco. Tras un reinicio, la misma ruta de preparación lo
reconstruirá desde precios, previsión, configuración y SOC actuales.

### 5. Reevaluación de las `00:05`

La evaluación diaria vuelve a construir el plan para el día corriente con:

- previsión de hoy/restante hoy;
- SOC y headroom en vivo;
- precios actuales;
- consumo del horizonte actual.

El resultado reemplaza atómicamente el plan provisional. Si la previsión cambió
o el consumo nocturno ya creó el hueco necesario, reduce o cancela las franjas
pendientes. Si falla, conserva una postura segura y no reactiva overrides
caducados.

### 6. Otras entradas

- **Reinicio después de las 13:00:** reconstruir primero el calendario normal y
  luego intentar el plan de mañana.
- **Botón Reevaluar Precio Dinámico:** reevaluar el día actual y, si hay datos,
  preparar también mañana.
- **Activar Smart Pre-discharge:** ejecutar inmediatamente ambos intentos
  aplicables.
- **Cambios de umbral, reserva, margen, exportación o sensores:** invalidar los
  dos fingerprints y reconstruir.
- **Desactivar la función o salir de Precio Dinámico:** limpiar ambos planes,
  overrides y bloqueos.

## Estados y diagnóstico

Ampliar `curtailment_status` y el diagnóstico descargable con:

- `plan_target_date`;
- `plan_horizon_start` / `plan_horizon_end`;
- `forecast_date` y `forecast_source`;
- `prepared_next_day_plan`;
- `prepared_target_date`;
- `prepared_first_discharge_slot`;
- `tomorrow_price_slots_available`;
- `tomorrow_forecast_available`;
- `next_day_evaluation_status`;
- `next_day_retry_at`.

Estados/razones nuevas y estables:

```text
waiting_tomorrow_prices
waiting_tomorrow_forecast
waiting_current_day_risk_end
next_day_plan_prepared
next_day_plan_promoted
target_date_expired
```

La falta del sensor de mañana no debe crear una Repair: es una capacidad
opcional y el comportamiento de las `00:05` sigue disponible. Sí debe quedar
visible en el sensor y en la documentación para explicar por qué no hubo
predescarga vespertina.

Los atributos con calendarios completos seguirán en `_unrecorded_attributes`
para no inflar Recorder.

## Configuración y compatibilidad

- Añadir el sensor opcional de previsión de mañana a los pasos de configuración
  solar y a las opciones de Precio Dinámico/Smart Pre-discharge.
- Validar existencia y unidad mediante el mismo helper de los sensores actuales.
- Conservar de forma independiente `today`/`remaining today` y `tomorrow`.
- No es necesaria una migración destructiva: las entradas existentes no tienen
  la nueva clave y mantienen exactamente el comportamiento de medianoche.
- Si el sensor actual ya aporta periodos fechados completos para mañana, no
  exigir un segundo sensor.
- Mantener Smart Pre-discharge desactivado por defecto.
- No añadir otro switch ni una hora configurable en esta fase.

## Cambios por archivo

### Núcleo

- `custom_components/omnibattery/pricing/curtailment.py`
  - fecha objetivo en `CurtailmentPlan`;
  - filtro de riesgos por fecha;
  - candidatos cross-midnight;
  - mapas explícitos por slot y nuevas invariantes.
- `custom_components/omnibattery/pricing/engine.py`
  - adquisición del horizonte de mañana;
  - preparación, reintento, promoción y rollover;
  - reconstrucción con consumo/solar fechados;
  - adaptación solar real solo en la fecha objetivo;
  - convivencia con calendario de carga y plan actual.
- `custom_components/omnibattery/solar_forecast.py`
  - lector por fecha;
  - validación del sensor de mañana;
  - selección atómica de periodos o total escalar.
- `custom_components/omnibattery/pricing/solar_timeline.py`
  - reutilización o pequeño adapter para mapear un día objetivo a slots de
    precio fechados.
- `custom_components/omnibattery/__init__.py`
  - estado runtime, carga de opciones y reset/promoción en medianoche.

### Proveedores y configuración

- `custom_components/omnibattery/pricing/engine.py` y
  `pricing/nordpool.py`: caché de Nord Pool oficial para hoy/mañana.
- `custom_components/omnibattery/const/integration_const.py`: nueva clave y
  constantes de retry.
- `custom_components/omnibattery/config_flow.py`: campo, defaults y validación.
- `custom_components/omnibattery/strings.json` y todas las traducciones:
  nombre, descripción y estados.

### Observabilidad y documentación

- `custom_components/omnibattery/binary_sensor.py`: atributos del plan activo y
  preparado.
- `custom_components/omnibattery/diagnostics.py`: fecha, fuentes y disponibilidad
  sin exponer datos sensibles.
- `site-docs/configuration/predictive-charging/dynamic-pricing.md` y `.es.md`:
  requisitos, fallback y secuencia vespertina.
- `site-docs/reference/entities.md` y `.es.md`: nuevos atributos/configuración.
- `README.md` y `CHANGELOG.md`: resumen de la capacidad al implementarla.

## Plan de pruebas

### Planner puro

1. Evaluación en `D-1` con riesgo solar en `D`: selecciona el pico de las
   `21:00` de `D-1` por delante de slots más baratos de madrugada.
2. Solo slots de `target_date` pueden convertirse en riesgo.
3. Los candidatos pueden cruzar medianoche, pero siempre terminan antes del
   primer riesgo.
4. Una ventana de riesgo de `D-1`, una carga reservada o una franja que bloquea
   descarga nunca se seleccionan.
5. Los mapas solares de `D` asignan cero a `D-1`; el consumo se distribuye por
   fecha sin duplicar el total.
6. Se mantienen los resultados actuales de Solo autoconsumo, Automático y
   Límite personalizado.
7. Cobertura horaria y de 15 minutos, precios empatados y bloques parciales.
8. Días DST de 23 y 25 horas con timestamps locales conscientes de zona.

### Motor y ciclo de vida

1. La preparación de mañana se ejecuta aunque la recarga vespertina salga antes
   por batería llena o ausencia de déficit nocturno.
2. La falta de precios o forecast programa retry y no consume el pase diario.
3. Nunca se usa `today` o `remaining today` como forecast de mañana.
4. Un plan de `D-1` con riesgo pendiente conserva prioridad; el plan preparado
   se promueve después de esa ventana.
5. Al promover, se reconstruye con el SOC actual y se evita sobre-descarga si el
   consumo natural ya creó hueco.
6. El acumulador solar de `D-1` no reduce la reserva de `D`.
7. El reset de medianoche conserva el plan de `D` y elimina overrides acabados.
8. La evaluación de las `00:05` reemplaza atómicamente el plan provisional.
9. Reinicio, botón manual, activación, desactivación y cambio de opciones
   gestionan ambos planes.
10. Una excepción deja el runtime sin exportación ni bloqueos huérfanos.
11. La carga oportunista respeta `free space - solar reserve` en un plan creado
    el día anterior.
12. Los slots de SOC mínimo garantizado mantienen su prioridad de seguridad.

### Precios y forecast

1. Nord Pool oficial fusiona hoy y mañana sin duplicados y tolera que mañana aún
   no esté publicado.
2. HACS Nord Pool usa `raw_tomorrow` sin cambiar el parser existente.
3. Tibber conserva hoy/mañana y el filtro respeta el final del día objetivo.
4. Un proveedor sin timestamps de mañana queda en `waiting_tomorrow_prices`.
5. El sensor de mañana acepta `Wh`/`kWh`, rechaza estados no finitos y conserva
   periodos válidos.
6. Periodos incompletos caen de forma atómica al total escalar; sin total válido
   no hay plan adelantado.

### Configuración, entidades y regresión

1. Alta y opciones guardan/limpian el sensor de mañana sin eliminar los sensores
   actuales.
2. Entradas antiguas mantienen el plan de las `00:05` sin migración manual.
3. El sensor y los diagnósticos publican fechas/fuentes serializables.
4. Los calendarios grandes no se registran en Recorder.
5. Ejecutar las suites focalizadas de pricing, curtailment, solar forecast,
   Nord Pool, config flow, binary sensor y diagnostics.
6. Ejecutar finalmente la suite completa, validación JSON, compilación Python y
   `git diff --check`.

## Criterios de aceptación

- Con datos de mañana disponibles, un riesgo solar de `D` puede generar una
  predescarga en una franja cara de `D-1`.
- La cantidad sigue limitada al hueco necesario y se detiene cuando ese hueco ya
  existe.
- Sin forecast solar fechado de `D`, nunca se inicia una predescarga adelantada.
- Una ventana protegida del día en curso nunca pierde prioridad.
- El plan sobrevive la medianoche sin dejar un override anterior activo.
- A las `00:05` se recalcula con información actual y no se duplica la reserva.
- Todos los suelos de SOC, límites de potencia, modos de exportación, ownership,
  bloqueos, carga oportunista y fail-safe actuales conservan su autoridad.
- Los usuarios existentes sin sensor de mañana no cambian de comportamiento.
- Diagnóstico y documentación explican por qué el plan se creó, esperó, se
  promovió o no pudo ejecutarse.

## Secuencia recomendada de implementación

1. **Contrato puro:** fecha objetivo, mapas por slot y pruebas cross-midnight en
   `curtailment.py`.
2. **Datos fechados:** lector solar de mañana, consumo multi-día y cobertura de
   precios, incluido Nord Pool oficial.
3. **Lifecycle:** preparación, retry, plan preparado, promoción, rollover y
   reevaluación de las `00:05`.
4. **Runtime e interacciones:** SOC vivo, acumulador solar, carga oportunista,
   slots reservados y limpieza fail-safe.
5. **Configuración y observabilidad:** flujo, entidades, diagnósticos y
   traducciones.
6. **Documentación y validación completa.**

Cada fase debe dejar pruebas focalizadas verdes antes de pasar a la siguiente.
La primera entrega no debe incorporar arbitraje predictivo general ni más de un
día futuro.
