# Otras baterías, solo lectura a través de Home Assistant

Omnibattery puede tener en cuenta una batería que no puede controlar, siempre que otra integración ya la publique en Home Assistant. La lee, la incluye en el cálculo del consumo doméstico y nunca le envía una orden.

Úsalo para un inversor de otro fabricante con su propia integración de Home Assistant —por ejemplo una batería BYD detrás de un Kostal Plenticore— donde la batería aplica su propia lógica de autoconsumo.

## ¿Lo necesito?

**Úsalo si** una segunda batería comparte tu contador de red y Omnibattery no puede controlarla. Sin esto, la carga de esa batería se imputa a tu consumo doméstico y su descarga se resta de él, de modo que el perfil de consumo aprendido y todo lo que se predice a partir de él se alejan de la realidad.

**No lo necesitas si** la batería ya está soportada como marca controlable. Consulta antes [¿Es compatible mi batería?](../../compatibility.md): una batería controlable siempre es la mejor conexión.

**No hará que la batería sea controlable.** Omnibattery nunca le envía una orden de carga, descarga o reposo, nunca forma parte del reparto de carga ni del controlador proporcional-derivativo (PD), y su energía almacenada no entra en la carga predictiva desde la red.

## Antes de empezar

- Instala y configura la integración que lee la batería, y comprueba que publica **estado de carga en porcentaje** y **potencia de batería en vatios**. Ambos son obligatorios.
- Fíjate en el signo del sensor de potencia. Compruébalo mientras la batería carga: varias integraciones, Kostal entre ellas, indican la carga con valores negativos.
- Averigua la capacidad útil de la batería en kWh. Nada la publica, así que la escribirás a mano.
- Revisa qué sensor configuraste como **sensor de producción solar** de Omnibattery. En un inversor híbrido debe medir el lado de **continua (DC)**. Consulta el aviso más abajo.

## Cómo añadirla

1. Abre **Ajustes → Dispositivos y servicios → Omnibattery → Configurar → Baterías** y aumenta en uno el número de baterías.
2. Elige **Other battery, read-only via Home Assistant entities** como marca.
3. Rellena el formulario:

| Campo | Qué introducir |
|---|---|
| **Nombre** | Un nombre descriptivo, por ejemplo `BYD` |
| **Sensor de estado de carga** | El sensor en porcentaje, p. ej. `sensor.scb_battery_soc` |
| **Sensor de potencia de la batería** | El sensor en vatios, p. ej. `sensor.scb_battery_power` |
| **Convención de signo de la potencia** | Si el sensor cuenta como positiva la carga o la descarga |
| **Contadores de energía cargada** | Contadores totales en kWh, opcionales. Elige varios si la fuente los separa —Kostal publica por separado la carga desde solar y desde la red— y se suman |
| **Contador de energía descargada** | Contador total en kWh, opcional |
| **Estado de carga objetivo** | Número escribible, opcional, p. ej. `number.scb_battery_min_soc` |
| **La batería comparte inversor con paneles solares** | Actívalo en un inversor híbrido |
| **Considerar la telemetría perdida tras** | Cuánto puede callar la integración de origen. Auméntalo si consulta lentamente |

4. En el paso siguiente, introduce la **capacidad nominal** en kWh. Es el único límite que pide el formulario.

## Qué verás

Un dispositivo con un sensor de estado de carga, un sensor de potencia y los contadores de energía que hayas configurado. Si has indicado un estado de carga objetivo escribible, aparece además un número **Target SOC**; Omnibattery se limita a leerlo mientras no actives el objetivo automático.

No verás permitir carga, permitir descarga, control manual, modo forzado, límites de estado de carga por software ni topes de potencia. Todos ellos dan forma a órdenes que esta batería nunca recibe, así que se omiten deliberadamente.

El efecto visible está en **Consumo doméstico**: debería coincidir ahora con la cifra de consumo del propio inversor mientras la batería carga o descarga. Esa corrección es el motivo de esta conexión.

!!! warning "Revisa tu sensor de producción solar en un inversor híbrido"
    Omnibattery deriva la carga doméstica como `red + potencia de batería + producción solar`. En un inversor híbrido, elige el sensor que mide la producción de **continua** (la suma de las entradas de string). La salida de alterna del inversor ya lleva restada la carga de la batería, así que usarla restaría la batería dos veces en cuanto configures esta conexión.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El asistente dice que no pudo leer algunas entidades | La entidad no existe, no está disponible o publica texto | El error nombra el valor que falta. Abre **Herramientas para desarrolladores → Estados** y confirma que la entidad muestra un número |
| El consumo doméstico ahora es demasiado bajo | La convención de signo está invertida | Observa el sensor de potencia mientras la batería carga y ajusta el campo |
| El consumo doméstico sigue siendo demasiado alto mientras la batería carga | El sensor de producción solar mide el lado de alterna | Consulta el aviso anterior |
| La batería pasa a no disponible constantemente | La integración de origen consulta más despacio que el límite configurado | Aumenta **Considerar la telemetría perdida tras** |
| El estado de carga del sistema incluye esta batería | Funciona según lo previsto | El estado de carga del sistema, la capacidad total y la energía almacenada describen la instalación, y esta batería forma parte física de ella. Solo la *planificación* la deja fuera — véase la fila siguiente |
| La carga predictiva compra energía de red aunque esta batería esté llena | Funciona según lo previsto, por ahora | Sus kilovatios hora se mantienen deliberadamente fuera del balance energético. Consulta la nota siguiente |

??? "Detalles avanzados"
    Omnibattery publica la potencia de esta batería solo bajo `battery_power`, nunca bajo `ac_power`. Los agregados usan entonces su respaldo documentado, `-battery_power`, que es lo que aporta al balance doméstico una batería sin puerto de alterna propio.

    La batería sí cuenta para el estado de carga del sistema, la capacidad total y la energía almacenada, porque esas cifras describen la instalación y no lo que Omnibattery pretende hacer con ella. La carga predictiva es el único lugar del que queda fuera.

    La batería queda excluida de `_get_available_batteries`, del reparto de carga, de la escalera de descarga y de la designación de batería primaria, y nunca llega a `apply_setpoint`. También queda fuera de la medida de escasez para baterías acopladas en continua que orienta el orden de carga: un hueco que esta integración no puede llenar haría parecer escaso cualquier día y dejaría paradas las baterías que sí podían haber absorbido la energía.

    Ningún camino de código puede darle una orden: además de filtrarla en el momento de la selección, tanto `_set_battery_power` como el propio `apply_power` del coordinador rechazan cualquier escritura sobre ella —incluido un 0 W en reposo—, de modo que una llamada que olvide el filtro sigue sin alcanzarla.

    Tampoco entra en el rendimiento de ida y vuelta medido que fija el margen de arbitraje. Sus propios contadores pueden ser del todo fiables, pero la energía que compra una decisión de arbitraje recorre el ciclo completo en las baterías que Omnibattery sí gobierna, y solo el rendimiento de estas puede decidir qué diferencial de precio debe superar esa compra.

    Su energía almacenada se mantiene por ahora completamente fuera de la carga predictiva. Contarla exigiría que entrara la mitad almacenada mientras la mitad del hueco libre se queda fuera —Omnibattery no puede ni cargar esta batería desde la red ni liberarla a demanda— y esa separación es un cambio aparte.

    El número **Target SOC** escribe en la entidad configurada, redondeado al paso que esa entidad acepta y acotado a su rango.
