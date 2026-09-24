# Evitar que cargas grandes y cargadores de VE usen la batería

La exclusión de cargas evita que un dispositivo grande agote la batería como si fuera consumo doméstico normal. Tú decides si el sol debe ir primero al dispositivo, si la batería puede cubrir el resto de la casa y si una wallbox autorregulada necesita margen para reaccionar.

## ¿Lo necesito?

**Úsala si** una carga grande supera la potencia de la batería, debe pagarse desde la red o ya tiene su propio controlador de excedente solar.

**No la necesitas si** quieres que la batería cubra el dispositivo exactamente como cualquier otra carga de casa.

Elige el comportamiento que coincide con tu objetivo:

| Objetivo | Comportamiento que debes usar |
|---|---|
| Mantener toda o parte de una carga grande fuera de la batería | Exclusión y **Exclusion percentage** |
| Dejar que un VE u otra carga use el sol disponible antes de que cargue la batería | **Solar Surplus** |
| Dejar que la batería cubra la demanda de casa mientras el sol y la red suministran la carga grande | **Cover Home**, junto con Solar Surplus |
| Coordinar con una wallbox que cambia su propia potencia según el contador de red | **Dynamic Power Control**, junto con Solar Surplus |
| Gestionar un cargador que informa del estado de carga pero no de vatios | **EV charger without power telemetry** |
| Evitar que la carga predictiva prometa el mismo sol a la batería y al VE | **Expected remaining demand** y, cuando sea necesario, su entidad de presencia |

## Antes de empezar

- Decide si el contador principal ya incluye el dispositivo.
- Identifica si el dispositivo expone telemetría de potencia, solo un estado de actividad o ambos.
- Para la prioridad solar y Cover Home, configura un sensor externo de producción solar en tiempo real cuando el comportamiento lo requiera.
- Completa la lista de sensores y campos de [Configurar una carga grande o cargador de VE](../configuration/excluded-devices.md).

## Cómo activarlo

1. Añade el dispositivo mediante **Ajustes → Dispositivos y servicios → Omnibattery → Configurar → Dispositivos excluidos**, usando la [lista de configuración](../configuration/excluded-devices.md#como-activarlo).
2. Abre el dispositivo del sistema Omnibattery o el panel y activa **Device – Enabled**.
3. Establece **Device – Exclusion %** en la parte que la batería debe ignorar.
4. Activa solo los controles de comportamiento en vivo que correspondan a tu objetivo: **Solar Surplus**, **Dynamic Power Control** o **Cover Home**.
5. Arranca el dispositivo y observa el flujo de red, la potencia de batería y la entidad de potencia o actividad del dispositivo.

![Controles de dispositivo excluido en Home Assistant](../assets/screenshots/features/load-exclusion-entities.png){ width="700" style="display: block; margin: 0 auto;"}

## Qué verás

- **Device – Enabled** activa o desactiva toda la corrección. Al desactivarlo, el control automático trata el dispositivo según la lectura de contador sin ajustar.
- **Device – Exclusion %** elige cuánta demanda queda fuera de la batería. Al 100%, la batería no cubre nada del dispositivo; al 0%, trata toda la demanda como carga normal. Los valores intermedios dividen la demanda.
- **Device – Solar Surplus** da al dispositivo activo prioridad sobre la carga de batería cuando hay sol disponible. La batería sigue sin descargar para la parte excluida.
- **Device – Cover Home** permite que la batería continúe cubriendo la demanda doméstica real mientras solo queda excluida la parte de red del dispositivo.
- **Device – Dynamic Power Control** da a una wallbox flexible tiempo para detectar y reclamar la exportación cambiante antes de que la batería cargue con el resto.

Un cargador de VE solo con estado pausa la carga y descarga de batería cuando aparece la carga. Tras la pausa, la batería puede cargar con excedente solar, pero permanece bloqueada para descargar hacia el VE hasta que termine la carga.

Si el dispositivo tiene configurada demanda restante prevista, la carga predictiva resta esa demanda del sol disponible para la batería. Una entidad de presencia libera la reserva cuando el VE o dispositivo está ausente. La [página de configuración](../configuration/excluded-devices.md) contiene las unidades aceptadas y las reglas de presencia.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| La batería sigue cubriendo todo el dispositivo | La exclusión está desactivada o su porcentaje es cero | Comprueba **Device – Enabled** y **Device – Exclusion %** |
| La batería ignora demasiada demanda de casa | **Cover Home** está desactivado mientras Solar Surplus da prioridad al dispositivo | Activa **Cover Home** si quieres que la batería cubra el resto de la casa |
| Batería y wallbox compiten por un sol cambiante | Dynamic Power Control está desactivado o su señal de actividad es incorrecta | Activa **Dynamic Power Control** y verifica que la entidad de actividad configurada cambie antes o junto con la demanda |
| La batería nunca carga mientras está activa la carga flexible | La wallbox sigue pidiendo prioridad o no queda exportación real | Comprueba potencia de dispositivo, estado de actividad, producción solar externa y exportación de red |
| Un cargador solo con estado no pausa la batería | Su entidad de actividad no informa de un estado activo reconocido | Comprueba **Device active / EV charging sensor** en la configuración del dispositivo |
| La carga predictiva omite energía barata y la batería queda baja | El sol prometido al dispositivo no está reservado | Configura **Expected remaining demand (kWh)** y una entidad de presencia si la demanda persiste tras desconectar |

??? "Detalles avanzados"
    **Corrección de carga**

    Para un dispositivo ya incluido en el contador principal, Omnibattery elimina la parte excluida antes de que el control proporcional–derivativo (PD) calcule su ajuste:

    ```text
    effective consumption = grid consumption - excluded device power
    control error = effective consumption - grid target
    ```

    Si el contador principal no ve el dispositivo, Omnibattery suma primero su potencia para reconstruir la demanda total y después aplica el tratamiento configurado. Esto evita eliminar la misma carga dos veces.

    **Temporización de Dynamic Power Control**

    Se considera que un dispositivo medido está consumiendo por encima de 100 W. En la primera demanda, la carga de batería cede durante 30 segundos. Un aumento de al menos 200 W en el margen disponible, calculado como producción solar menos potencia de dispositivo, inicia otra cesión de 20 segundos. Si no hay sensor de producción solar disponible, Omnibattery sondea durante 20 segundos cada 5 minutos en su lugar.

    Cuando baja la potencia del dispositivo, la descarga permanece bloqueada durante 5 minutos y la carga recibe una gracia de reinicio más corta. Un sensor de actividad activo también bloquea la carga antes de que aparezca la potencia medida, evitando un bloqueo de arranque en frío en el que la batería absorbe la exportación antes de que arranque la wallbox. Dynamic Power Control solo se aplica cuando el dispositivo está activado, incluido en el sensor principal de consumo, medido, no está en modo VE solo con estado y están activados tanto Solar Surplus como Dynamic Power Control.

    **Temporización de VE solo con estado**

    Cuando se detecta por primera vez la carga, Omnibattery ordena 0 W, bloquea ambas direcciones y congela el estado PD durante 5 minutos. La pausa da tiempo al cargador para negociar la corriente con el vehículo. Después, puede reanudarse la carga por excedente solar mientras la descarga sigue bloqueada hasta que el estado de actividad deje de informar carga.
