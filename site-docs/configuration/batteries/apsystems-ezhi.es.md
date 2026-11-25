# APsystems EZHI

Omnibattery controla un microinversor híbrido APsystems EZHI y su batería mediante la interfaz HTTP local del EZHI. No se necesita conexión a la nube ni credenciales.

## ¿Lo necesito?

**Úsalo si** tienes un EZHI, Home Assistant puede acceder a él en tu red y puedes ponerlo en **Local Mode** desde la app AP EasyPower.

**Cuenta con un límite de 1.200 W** en ambos sentidos. El soporte del EZHI es nuevo: incluye la versión de firmware al informar de un comportamiento que difiera de esta página.

## Antes de empezar

- Activa **Local Mode** en la app AP EasyPower. Sin él, el EZHI acepta las órdenes pero no las ejecuta.
- Asigna al EZHI una dirección IP local estable y confirma que Home Assistant puede acceder a él.
- No necesitas nada más: el EZHI comunica la capacidad de su batería. El asistente solo la pide si el dispositivo no la da.

## Cómo añadirla

1. En el asistente de configuración de Omnibattery, elige **APsystems EZHI**.
2. Introduce un **Name** descriptivo y la **IP address** del EZHI.
3. Mantén **Port** en `80` salvo que lo hayas cambiado y espera la prueba de conexión.
4. Elige los límites comunes de potencia y estado de carga.

## Qué verás

Omnibattery proporciona control automático y controles por software **Force Mode** y de potencia. Activa **Battery Manual Control** antes de enviar un objetivo manual de carga, descarga o reposo. La integración envía un único objetivo de potencia neta al EZHI y comprueba que el dispositivo lo ha aceptado.

Las entidades incluyen el estado de carga (SOC) de la batería, **Battery Power**, **Solar Power**, **AC Delivered Power**, la temperatura de la batería, el estado de salud y la energía cargada y descargada acumulada. **Solar Power** cuenta como solar del sistema en los cálculos de Omnibattery. **Inverter Temperature** y **Power Setpoint** se crean desactivadas.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El asistente no puede conectar | Local Mode está desactivado, o la dirección o el puerto son incorrectos | Activa **Local Mode** en AP EasyPower y comprueba que la IP responde |
| La batería ignora las órdenes | El EZHI ha salido de Local Mode | Revisa el modo del sistema en AP EasyPower |
| El asistente pide la capacidad | El EZHI no la ha comunicado | Introduce la capacidad nominal de la batería en kWh |
| Los valores manuales no tienen efecto | La batería sigue en el grupo automático | Activa **Battery Manual Control** |

??? "Detalles avanzados"
    El EZHI acepta de `-1.200` a `1.200 W`; Omnibattery usa ese rango fijo para carga y descarga. No hay reducción por voltaje de celda ni corte de SOC por hardware: los límites de SOC se aplican por software.

    El driver lee `/getDeviceInfo` (número de serie, firmware, capacidad), `/getOutputData` (SOC, estado de salud, potencia de batería, solar y del puerto AC, temperaturas, kWh acumulados) y `/getPower`, y escribe `/setPower?p=<W>`. `/setPower` responde con éxito en cualquier modo del sistema pero solo actúa en Local Mode, así que cada orden se confirma contra la consigna que devuelve `/getPower`.

    La solar entra directamente al puerto AC del EZHI, así que la potencia de batería y la del puerto AC difieren mientras hay sol. Las comprobaciones de entrega usan el puerto AC (**AC Delivered Power**), que es lo que controla la consigna.

    Para los controles compartidos y los límites del sistema, consulta [Elige la conexión de tu batería](index.md).
