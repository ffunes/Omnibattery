# Carga predictiva según el precio actual

El modo Precio en tiempo real comprueba el precio vigente y carga desde la red cuando es suficientemente barato y la vivienda tiene un déficit energético previsto. No promete nada sobre futuros periodos porque no tiene un calendario de precios futuros.

## ¿Lo necesito?

**Úsalo si…** tu fuente expone solo el precio actual de electricidad o quieres una regla directa de «cargar por debajo de este precio».

**No lo necesitas si…** tu proveedor publica precios futuros y quieres seleccionar los periodos más baratos por adelantado; usa [Precio dinámico](dynamic-pricing.md). Para periodos baratos semanales fijos, usa [Franja horaria](time-slot.md).

## Antes de empezar

- Prepara un sensor de Home Assistant para el precio actual de electricidad.
- Elige un **Umbral máximo de precio** fijo o un sensor opcional de precio medio diario. El sensor medio tiene prioridad cuando tiene un valor válido.
- La previsión solar es opcional. Sin una, Omnibattery evalúa de forma conservadora sin solar futura.
- Configura los requisitos comunes descritos en [¿Qué modo debo elegir?](index.md).

## Cómo activarlo

1. Abre **Ajustes → Dispositivos y servicios → Omnibattery → Configurar** y elige **Precio en tiempo real** como modo de carga predictiva.
2. Selecciona **Sensor de precio de electricidad** y, si está disponible, **Sensor de precio medio diario**.
3. Selecciona un sensor opcional de previsión solar y termina el formulario.
4. En la pestaña **Control** de Omnibattery, establece **Umbral máximo de precio** si no proporcionaste un sensor medio y confirma que **Carga predictiva** está activada.

![Configura la fuente de precio actual](../../assets/screenshots/configuration/predictive-charging/real-time-price-form.png){ width="650" style="display: block; margin: 0 auto;" }

## Qué verás

Cuando el precio actual está en o por debajo del umbral activo, Omnibattery comprueba el balance energético restante. Inicia la carga desde red solo cuando la energía de la batería y la solar esperada no cubren la demanda esperada de la vivienda. La carga se detiene cuando el precio sube por encima del umbral, se alcanza el objetivo calculado o una regla de permiso de carga o seguridad la bloquea.

Este modo no reserva un periodo futuro más barato, no asigna cuotas futuras de precio ni expone **Reevaluar carga predictiva**. Comprueba de nuevo en cada ciclo de control. Las [franjas horarias de funcionamiento](../time-slots.md) configuradas pueden seguir restringiendo cuándo se permite cargar.

Activa **Descarga basada en precio** si también quieres conservar la batería mientras la electricidad es barata. La descarga se bloquea en o por debajo del mismo umbral activo y se reanuda por encima de él, sujeta a las reglas de horario de funcionamiento y seguridad. La carga por excedente solar sigue disponible.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| La carga nunca empieza con un precio barato | No existe un umbral válido o ya no queda déficit energético | Sensor de precio actual, sensor medio, **Umbral máximo de precio** y **Carga predictiva activa** |
| La carga se detiene inmediatamente | El precio actual ha subido, ha terminado una franja de funcionamiento o ha intervenido una regla de seguridad/control | Precio en vivo, permisos de carga, objetivo de SOC y **Estado de integración** |
| Se ignora un periodo más barato posterior | Este modo no tiene un calendario de precios futuros | Cambia a [Precio dinámico](dynamic-pricing.md) si hay precios futuros disponibles |
| La batería no descarga | **Descarga basada en precio** o una franja de funcionamiento bloquea la descarga | Umbral activo, precio en vivo y [franjas horarias de funcionamiento](../time-slots.md) |
| Parece que se ignora el sensor medio | Su estado no está disponible o no es numérico | Estado y unidad del sensor; se usa el umbral fijo como método alternativo |
| La energía prevista cambia pero no empieza a cargar pronto | El precio actual está por encima del umbral | Espera a que el precio cumpla; este modo no puede reservar un periodo futuro |

??? "Detalles avanzados"
    ### Regla de carga y prioridad del umbral

    Cada ciclo de control aplica esta regla:

    ```text
    if current_price ≤ active_threshold:
        if usable_battery + expected_solar < expected_consumption:
            start or continue grid charging
    else:
        stop grid charging
    ```

    El umbral activo se resuelve en este orden:

    1. Un **Sensor de precio medio diario** válido, cuando está configurado.
    2. **Umbral máximo de precio**, la entidad numérica en vivo.

    Si ninguno proporciona un valor, Precio en tiempo real no realiza ninguna acción de carga. Si el precio en vivo deja de estar disponible durante una carga, la carga se detiene y la energía no entregada se registra como déficit no cubierto.

    El objetivo energético sigue siendo predictivo: Omnibattery calcula el déficit del horizonte actual y un objetivo por batería. Lo que permanece reactivo es la selección de periodo. Un sensor de precio actual no puede demostrar que existirá un periodo futuro ni que cumplirá un plazo energético, así que este modo no crea reservas futuras.

    **Margen de seguridad de previsión solar** se resta de la solar esperada. Las instalaciones nuevas empiezan con aproximadamente el 5% de la capacidad total de batería configurada; si la capacidad se desconoce durante la configuración, el método alternativo es no aplicar margen. Es una entidad de control en vivo, no un campo del formulario de configuración.

    ### Descarga basada en precio

    La regla de descarga opcional es la inversa de la carga:

    ```text
    if current_price > active_threshold:
        discharge allowed
    else:
        discharge blocked
    ```

    Cuando se bloquea, la potencia de batería vuelve a 0 W y el controlador proporcional–derivativo (PD) congela su estado para poder reanudarse sin un salto derivativo. Si las franjas horarias de funcionamiento también restringen la descarga, deben ser verdaderos tanto el permiso temporal como el permiso de precio.

    Esta regla limita la descarga económica ordinaria. SOC mínimo, protección de fase, disponibilidad de batería, control manual, restricciones de respaldo y las demás reglas de seguridad siguen siendo autoritativas.

    ### Comparación con Precio dinámico

    | Capacidad | Precio dinámico | Precio en tiempo real |
    |---|---|---|
    | Entrada de precio | Periodos futuros de precios fechados | Estado de precio actual |
    | Elección de periodo | Periodos futuros elegibles más baratos antes de cada plazo | El periodo vigente ahora |
    | Gestión del déficit no cubierto | Puede mover la cuota a futuros periodos elegibles | Registra energía no entregada; no existe calendario futuro |
    | Botón de reevaluación | Reconstruye el calendario restante | No está presente; cada ciclo decide de nuevo |
    | Umbral de descarga basada en precio | Umbral máximo fijo o media diaria calculada | Sensor medio diario y después umbral máximo fijo |
    | Políticas solo dinámicas | Programación con precio negativo, anti-limitación de producción, retención de excedente, reserva de descarga, margen de arbitraje, exportación con precio alto | No disponibles |
