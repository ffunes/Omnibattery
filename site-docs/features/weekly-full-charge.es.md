# Carga completa semanal

Una carga completa semanal proporciona a una batería de litio-ferrofosfato (LFP) tiempo regular en la parte alta de su rango de carga y produce una lectura comparable del equilibrio de celdas. Resulta más útil cuando tu estado de carga (SOC) máximo habitual está por debajo del 100%.

## ¿Lo necesito?

**Úsalo si** tu batería rara vez llega al 100%, o quieres una comprobación de equilibrio regular sin iniciar tú una carga completa.

**No lo necesitas si** la batería ya llega al 100% regularmente. Una carga completa también puede comprar energía de la red, así que elige el día y el comportamiento de retraso solar adecuados para tu tarifa.

## Antes de empezar

- Omnibattery debe poder cargar la batería automáticamente.
- Mantén activada **Reducción de tensión de carga al 100%** para cada batería compatible si quieres una lectura de equilibrio de celdas estabilizada.
- Si está activado **Retraso de carga**, decide si el ciclo semanal puede comenzar inmediatamente o debe esperar a la solar prevista.
- Una batería en **Control manual de batería** queda excluida hasta que se reanude el control automático.

## Cómo activarlo

1. Abre el panel lateral de Omnibattery y selecciona **Control**.
2. En **Carga completa semanal**, activa **Carga completa semanal**.
3. Elige **Día de carga completa semanal**.
4. Si usas **Retraso de carga**, activa **Retrasar carga completa semanal** para esperar al sol; déjalo desactivado para iniciar el ciclo semanal sin ese retraso.

![Configura el día de carga completa semanal y el retraso solar](../assets/screenshots/configuration/advanced-weekly-full-charge-config.png){ width="650" style="display: block; margin: 0 auto;"}

## Qué verás

**Carga completa semanal** informa `idle`, `charging` o `complete`. El día elegido, Omnibattery eleva temporalmente el objetivo de carga al 100% para las baterías bajo control automático. Solo marca el ciclo como completo después de considerar llenas todas las baterías participantes; entonces restaura cada límite configurado.

Con [carga predictiva](../configuration/predictive-charging/index.md), la energía restante necesaria para el objetivo semanal entra en el plan. Omnibattery puede comprar esa energía durante el periodo de carga configurado cuando la solar prevista no la cubra. Sin carga predictiva, el ciclo depende de la solar disponible y puede no completarse en un día nublado.

El atributo `batteries` del sensor muestra el SOC en directo y la evidencia de finalización de cada batería. En baterías compatibles, **Delta de celdas**, **Estado de equilibrio** y **Última lectura de equilibrio** se actualizan tras la medición en la parte alta de carga. Consulta [¿Está sana mi batería?](cell-balance-monitor.md) para interpretar el resultado.

!!! important "Comportamiento del retraso solar"
    **Retrasar carga completa semanal** está desactivado de forma predeterminada. Por ello el ciclo semanal evita **Retraso de carga** y puede comenzar el día seleccionado. Activa el interruptor si prefieres que espere a que el retraso solar libere la carga.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El estado permanece `idle` | Hoy no es el día seleccionado o la función está desactivada | **Carga completa semanal** y **Día de carga completa semanal** |
| El ciclo espera en lugar de cargar | El ciclo semanal respeta el retraso solar | **Retrasar carga completa semanal** y **Retraso de carga** |
| Una batería no participa | El control manual por batería la controla, o sus datos no están disponibles | **Control manual de batería** y disponibilidad de la batería |
| El ciclo sigue `charging` cerca de lleno | El sistema de gestión de batería (BMS) no ha confirmado que todas las baterías participantes estén llenas | Los detalles por batería en **Carga completa semanal**; deja que termine el proceso de carga superior |
| El ciclo termina pero no aparece resultado de equilibrio | La batería no expone ambos extremos de tensión de celda, o no pudo terminar la medición diagnóstica en reposo | Si existe **Delta de celdas** y si ha cambiado **Última lectura de equilibrio** |

??? "Detalles avanzados"
    **Secuencia de carga y finalización**

    La función semanal eleva el SOC objetivo al 100%; no usa un algoritmo de equilibrado independiente. Con **Reducción de tensión de carga al 100%** activada, la carga se limita a 200 W después de que la tensión de la celda de control entre en la zona de reducción de 3.48 V. La medición de equilibrio se toma después de detener la carga y de que las celdas reposen 60 segundos.

    Omnibattery no toma una sola observación de 3.60 V como prueba de que una batería esté llena. Para baterías Venus E, la finalización puede proceder de un SOC informado del 100% o de un corte de BMS confirmado. Se confirma un corte cuando se ordenó cargar a la batería, la potencia entregada cae a 10 W o menos y el inversor permanece en Standby durante cinco ciclos de control consecutivos. La misma ruta de corte cubre un pack cuyo contador de SOC se ha desviado por debajo del 100%.

    Las baterías Venus A/D pueden contener packs acoplados. Su tensión máxima de celda informada puede representar solo un pack, por lo que Omnibattery mantiene la orden reducida de 200 W activa hasta confirmar el corte del BMS. Que un pack alcance el umbral de tensión no puede finalizar por sí solo el ciclo completo.

    Participa toda batería con datos actuales salvo las que estén en **Control manual de batería**. Los límites configurados se restauran solo después de completar todas las participantes. La medición de delta de celdas de 60 segundos es diagnóstica y no mantiene abierto el ciclo semanal.

    El atributo `batteries` informa de SOC en directo y recuento de ciclos de corte de BMS de cada batería mientras carga; tras terminar añade `soc_at_completion`, `max_cell_voltage_at_completion`, `completion_reason` y `bms_cutoff_cycles`.

    **Recalibración de SOC y comportamiento de reintento**

    Fuera del ciclo semanal, una batería Venus E que alcanza 3.60 V informando menos de 99% de SOC puede mantenerse a la reducción de 200 W hasta que corte su BMS. Si el primer corte ocurre por encima de 3.60 V y el SOC sigue por debajo del 100%, Omnibattery espera a que la celda se relaje a 3.57 V y permite un intento más a 200 W. Es una oportunidad de mejor esfuerzo para que el BMS recalibre su contador de SOC; el firmware decide si se recalibra.

    **Límites de hardware y software**

    Marstek Venus E v2 expone el registro de corte de carga `44000`, que el ciclo eleva temporalmente al 100%. Venus E v3 y Venus A/D no tienen registro de corte de SOC de hardware en Omnibattery y usan aplicación por software. Otros controladores usan su capacidad declarada de control por hardware o software. En todos los casos, el límite guardado se restaura cuando el ciclo termina o se detiene.

    Para recuperar un resultado de equilibrio rojo persistente, usa el [blueprint opcional de equilibrado activo Marstek](../automations/blueprints.md#balanceo-activo-de-una-bateria-marstek). Toma el control de una batería mediante **Control manual de batería** y es independiente de la función semanal.
