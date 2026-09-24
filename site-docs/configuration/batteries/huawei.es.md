# Huawei SUN2000 + LUNA2000

Omnibattery lee localmente un inversor Huawei SUN2000 y su batería LUNA2000, y controla la potencia de batería cuando hacerlo no reducirá tu producción solar. Así tienes control automático de la batería desde Home Assistant por la noche mientras el inversor mantiene el control cuando importa la producción solar.

## ¿Lo necesito?

**Úsalo si** tu SUN2000 comunica una batería LUNA2000 mediante Modbus TCP y quieres que Omnibattery la coordine con tu sensor de red, horarios u otras baterías compatibles.

**No lo uses para descarga forzada diurna.** Una orden forzada puede limitar la producción solar del inversor. Por ello, Omnibattery deja el control al inversor mientras sus cadenas solares producen; la descarga está disponible después de oscurecer y la carga puede enviarse de día solo cuando no pueda limitar la producción solar disponible.

| Equipo compatible | Conexión | Control automático | Límite importante |
|---|---|---|---|
| Inversor SUN2000 que comunica una LUNA2000 conectada | Modbus TCP, directamente o mediante un proxy Modbus | Sí, sujeto a la protección de producción solar | El límite efectivo de carga y descarga es el menor entre tu límite configurado y el límite en vivo comunicado por la batería. |

Omnibattery lee la capacidad de batería, contadores de energía, temperatura, tensión de batería, estado de carga (SOC), potencia de batería, potencia solar y estado del inversor. También puede mostrar potencia solar por cadena para hasta cuatro cadenas. Un gestor de energía EMMA conectado se detecta automáticamente cuando está disponible y puede proporcionar una lectura rápida de potencia de red.

## Antes de empezar

- Confirma que Home Assistant puede llegar al inversor o a un proxy Modbus en la red local.
- Si **Huawei Solar** ya está conectado al inversor, usa un proxy Modbus: el inversor acepta una conexión Modbus a la vez.
- Elige una vía de control:
    - Predeterminada: instala y configura **Huawei Solar**, y después identifica su dispositivo de batería LUNA2000 en el asistente.
    - Alternativa: usa **Direct Modbus writes**. No requiere Huawei Solar, pero está desactivada de forma predeterminada.
- Usa el ID de unidad Modbus del inversor, no el ID de unidad de EMMA o del cargador. Puedes dejarlo vacío para que Omnibattery busque.

!!! important "El control diurno se limita intencionadamente"
    El inversor y los paneles solares están acoplados en CC. Durante la producción solar, Omnibattery libera el control en lugar de enviar una orden que podría limitar el conjunto fotovoltaico. Este comportamiento es esperado, no una orden fallida.

## Cómo añadirlo

Esta vía se configura en el asistente de Omnibattery; no hay un interruptor independiente para activarla.

1. Abre **Ajustes → Dispositivos y servicios → Añadir integración** y selecciona **Omnibattery**. En una instalación existente, abre Omnibattery y selecciona **Configurar**.
2. En **Battery _n_ — Brand**, selecciona **Huawei SUN2000 + LUNA2000**.
3. En **Configure battery _n_ — Connection (Huawei)**, introduce **Name**, **IP Address** y **Modbus Port** para el inversor o proxy Modbus.
4. Deja en blanco **Modbus slave id (leave empty to search)** para encontrar el inversor automáticamente, o introduce su ID de unidad de inversor. Si responde más de un inversor con batería, elige el correcto en **Configure battery _n_ — Choose inverter**.
5. Deja **Direct Modbus writes** desactivado y selecciona **Huawei Solar battery device**, o activa **Direct Modbus writes** y deja vacío el campo de dispositivo. Termina los límites compartidos de potencia y SOC.

El asistente verifica que hay una LUNA2000 conectada. En la vía predeterminada, también rechaza un dispositivo de batería Huawei Solar que pertenezca a otro inversor.

## Qué verás

Verás control automático de batería, **Battery Manual Control** y los controles manuales de potencia compartidos. Una orden cero o de reposo libera la batería para que vuelva al modo de trabajo propio del inversor; no mantiene la batería a potencia cero.

El dispositivo de batería proporciona lecturas como **Battery SOC**, **Battery Power**, **Battery Voltage**, **Solar Power**, **Storage Status**, **Working Mode**, contadores de energía y **Battery Temperature**. **Grid Power** aparece solo cuando se detecta un EMMA. **MPPT1 Power** a **MPPT4 Power** aparecen solo para las cadenas que comunica el inversor.

Omnibattery aplica por software tu SOC mínimo y máximo general. Los propios registros de corte del inversor se usan solo como respaldo más restrictivo. El monitor de equilibrio de celdas, la reducción gradual por tensión y el soporte de registros de alarma no están disponibles en esta vía porque no proporciona las lecturas de celdas necesarias ni datos de alarma validados.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El asistente no puede conectarse | Dirección, puerto o extremo Modbus inaccesible incorrectos | Confirma que Home Assistant puede llegar al inversor o proxy y que Modbus TCP está disponible. |
| El asistente no encuentra batería | La unidad seleccionada no es el inversor o no hay una LUNA2000 conectada | Deja **Modbus slave id (leave empty to search)** vacío y comprueba después la conexión del inversor y la instalación LUNA2000. |
| El asistente pregunta qué inversor usar | Respondió más de un inversor con batería en el bus | Selecciona el inversor que posee esta LUNA2000; añade otra entrada de batería para otro inversor. |
| No se puede completar la vía de control predeterminada | Falta Huawei Solar o no se seleccionó su dispositivo de batería | Configura **Huawei Solar** y selecciona **Huawei Solar battery device**, o activa **Direct Modbus writes**. |
| Las órdenes no tienen efecto de día | Omnibattery detectó cadenas solares produciendo | Es la protección de producción solar. Comprueba el control de batería después de oscurecer. |
| La potencia solicitada es menor de la configurada | La batería comunica actualmente una capacidad de carga o descarga menor | Comprueba **Max Charge Power** o **Max Discharge Power** y la configuración de paquetes de la batería. |
| Grid Power no está disponible | No se detectó ningún EMMA en el bus Modbus | Configura tu sensor habitual de potencia de red de Home Assistant; la batería Huawei sigue siendo utilizable sin la lectura EMMA. |

??? "Detalles avanzados"
    La telemetría usa lecturas de registros de retención Modbus TCP. La vía de control predeterminada envía órdenes de carga forzada, descarga y liberación mediante servicios Huawei Solar; **Direct Modbus writes** escribe la misma secuencia de control forzado en el inversor.

    El asistente busca IDs de unidad de inversor de `0` a `247` cuando el ID de esclavo está vacío. Distingue las direcciones de inversor, gestor de energía y cargador sondeando el modelo de inversor y el almacenamiento conectado. Si un proxy Modbus sirve inversores en cascada, el asistente te pide seleccionar el que tiene esta batería.

    El formulario de límites acepta `100–15,000 W` para cada sentido. Omnibattery usa inicialmente los límites de carga y descarga en vivo comunicados por la batería, y cada orden se limita otra vez al menor entre ese valor en vivo y tu límite configurado. El registro propio de corte de carga del inversor acepta `90–100%`; su registro de corte de descarga acepta `0–20%`. Los límites fuera de esos rangos de hardware siguen aplicándose por software.

    Una orden forzada dura `10 min`, pero Omnibattery actualiza o cambia las órdenes según sea necesario y libera el inversor con un objetivo de reposo. La vía de órdenes tiene una banda muerta de escritura de `250 W`, espera al menos `20 s` entre escrituras normales y actualiza una orden mantenida después de `240 s`. La latencia declarada del actuador es `25 s`, por lo que una lectura de potencia inmediata no se considera una comprobación exacta de entrega.

    El driver lee hasta cuatro pares de tensión/corriente de cadena y calcula cada valor **MPPT _n_ Power**. Esas lecturas describen el lado solar de CC del inversor; no se exponen como puerto de batería de CA. La LUNA2000 comunica información de paquetes en lugar de las tensiones por celda necesarias para el monitor de equilibrio de celdas y la reducción gradual por tensión.
