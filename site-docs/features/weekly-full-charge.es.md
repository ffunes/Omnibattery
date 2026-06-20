# Carga semanal completa

Carga las baterías al **100 % una vez por semana** para que el pack llegue a la ventana superior de balanceo LFP y la integración pueda medir el desbalanceo de celdas en condiciones repetibles.

## Comportamiento

1. El día configurado de la semana, si el SOC máximo habitual es inferior al 100 %, la integración eleva temporalmente el límite de corte de carga de la batería al 100 %.
2. La batería carga hasta que entra la reducción por voltaje en la parte alta.
3. Desde `max_cell_voltage >= 3.48 V`, la carga se limita a 95 W (si la reducción por voltaje está activada).
4. Durante la carga semanal **no** se aplica la pausa de 3.58 V: la carga continúa a 95 W hasta el corte del BMS. La medición de 60 s del delta de celdas sigue ejecutándose como diagnóstico, pero ya no determina la finalización.
5. La carga se marca como completada solo cuando la batería está realmente llena: SOC reportado al **100 %**, o un **corte del BMS** confirmado (carga ≤10 W en Standby durante 5 ciclos ~10 s, reconocido en la zona de reducción ≥ 3.48 V aunque el SOC esté mal reportado).
6. Tras finalizar, el límite de SOC máximo (y el registro de corte hardware en v2) vuelve automáticamente al valor configurado por el usuario, y se reactiva la histéresis.

La carga semanal completa usa el mismo perfil de voltaje que una batería configurada normalmente con `max_soc = 100`. La función semanal solo eleva el objetivo a 100 %; no usa un algoritmo de balanceo distinto.

El sensor **Carga semanal completa** expone diagnósticos por batería en su atributo `batteries`: SOC en vivo y contador de ciclos de corte del BMS durante la carga, y una instantánea al completar (`soc_at_completion`, `max_cell_voltage_at_completion`, `completion_reason`, `bms_cutoff_cycles`).

!!! note "SOC desviado"
    Durante la carga semanal la pausa de 3.58 V **no** se aplica: la carga sigue a 95 W hasta que el BMS corta. Si el contador culombimétrico del BMS se ha desviado (celdas realmente llenas pero SOC reportado por debajo del 100 %), la finalización igual se detecta: la firma de corte del BMS (carga ≤10 W con el inversor en Standby durante 5 ciclos) se reconoce siempre que el pack esté en la zona de reducción (≥ 3.48 V), sin importar el SOC reportado. Así la carga semanal puede terminar aunque el pack nunca llegue a leer 100 %, e *intenta* recalibrar el SOC — depende del firmware del BMS. Ver [Recalibración de SOC con tensión alta atascada](cell-balance-monitor.md#recalibracion-de-soc-con-tension-alta-atascada).

## Monitor de equilibrio de celdas

El **monitor de equilibrio de celdas** está siempre activo. Registra la diferencia de tensión entre la celda más alta y la más baja tras cada medición en tensión alta, y mantiene actualizados los sensores, la tendencia y las alertas.

Consulta [Monitor de equilibrio de celdas](cell-balance-monitor.md) para más detalles.

## Interacción con el retraso de carga solar

Si el [retraso de carga solar](solar-charge-delay.md) está activo, la carga semanal puede aplazarse mientras la producción solar prevista sea suficiente para alcanzar el 100 %.

Cuando la carga semanal completa está activa, la integración puede omitir el retraso para que la batería alcance el punto de medición en tensión alta y la lectura de balance no se pierda.

## Registro Modbus implicado

La función manipula el registro **44000** (charging cutoff) de la batería para elevar temporalmente el límite.

!!! info
    Esta función está disponible para todas las versiones de batería compatibles (v2, v3, vA, vD).

![Configuración de carga semanal completa](../assets/screenshots/features/weekly-full-charge-config.png){ width="650"  style="display: block; margin: 0 auto;"}
