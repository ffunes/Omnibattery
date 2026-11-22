# Otras baterías a través de Home Assistant

Omnibattery puede tener en cuenta una batería que no puede controlar, siempre que otra integración ya la publique en Home Assistant. La lee, la incluye en el cálculo del consumo doméstico y nunca le envía una orden de potencia. Si esa integración tiene además un select o un interruptor que activa y desactiva la regulación propia de la batería, Omnibattery puede usarlo para [pararla cuando no debe actuar](#modo-de-operacion-nom-y-manual).

Úsalo para un inversor de otro fabricante con su propia integración de Home Assistant —por ejemplo una batería BYD detrás de un Kostal Plenticore— donde la batería aplica su propia lógica de autoconsumo.

## ¿Lo necesito?

**Úsalo si** una segunda batería comparte tu contador de red y Omnibattery no puede controlarla. Sin esto, la carga de esa batería se imputa a tu consumo doméstico y su descarga se resta de él, de modo que el perfil de consumo aprendido y todo lo que se predice a partir de él se alejan de la realidad.

**No lo necesitas si** la batería ya está soportada como marca controlable. Consulta antes [¿Es compatible mi batería?](../../compatibility.md): una batería controlable siempre es la mejor conexión.

**No hará que la batería sea controlable.** Omnibattery nunca le envía una orden de carga, descarga o reposo, nunca forma parte del reparto de carga ni del controlador proporcional-derivativo (PD), y su energía almacenada no entra en la carga predictiva desde la red.

## Antes de empezar

- Instala y configura la integración que lee la batería, y comprueba que publica **estado de carga en porcentaje** y **potencia de batería** (W o kW). Ambos son obligatorios.
- Fíjate en el signo del sensor de potencia. Compruébalo mientras la batería carga: varias integraciones, Kostal entre ellas, indican la carga con valores negativos.
- Averigua la capacidad útil de la batería en kWh. Nada la publica, así que la escribirás a mano.
- Revisa qué sensor configuraste como **sensor de producción solar** de Omnibattery. En un inversor híbrido debe medir el lado de **continua (DC)**. Consulta el aviso más abajo.

## Cómo añadirla

1. Abre **Ajustes → Dispositivos y servicios → Omnibattery → Configurar → Baterías** y aumenta en uno el número de baterías.
2. Elige **Other battery via Home Assistant entities** como marca.
3. Rellena el formulario:

| Campo | Qué introducir |
|---|---|
| **Nombre** | Un nombre descriptivo, por ejemplo `BYD` |
| **Sensor de estado de carga** | El sensor en porcentaje, p. ej. `sensor.scb_battery_soc` |
| **Sensor de potencia de la batería** | El sensor de potencia (W o kW), p. ej. `sensor.scb_battery_power` |
| **Convención de signo de la potencia** | Si el sensor cuenta como positiva la carga o la descarga |
| **Contadores de energía cargada** | Contadores totales (Wh, kWh o MWh), opcionales. Elige varios si la fuente los separa —Kostal publica por separado la carga desde solar y desde la red— y se suman |
| **Contador de energía descargada** | Contador total (Wh, kWh o MWh), opcional |
| **Estado de carga objetivo** | Número escribible, opcional, p. ej. `number.scb_battery_min_soc` |
| **Entidad de modo de operación** | Select o interruptor, opcional, que elige si la batería regula por su cuenta. Véase [más abajo](#modo-de-operacion-nom-y-manual) |
| **Considerar la telemetría perdida tras** | Cuánto puede callar la integración de origen. Auméntalo si consulta lentamente. El silencio con la batería en reposo (hasta 50 W) no cuenta: muchas fuentes solo publican cambios |

4. Si has elegido una entidad de modo, indica cuál de sus estados es **NOM** y cuál **Manual**.
5. En el paso siguiente, introduce la **capacidad nominal** en kWh. Es el único límite que pide el formulario.

## Qué verás

Un dispositivo con un sensor de estado de carga, un sensor de potencia y los contadores de energía que hayas configurado. Si has indicado un estado de carga objetivo escribible, aparece además un número **Target SOC**; lo fijas tú, Omnibattery nunca lo escribe por su cuenta.

No verás permitir carga, permitir descarga, control manual, modo forzado, límites de estado de carga por software ni topes de potencia. Todos ellos dan forma a órdenes que esta batería nunca recibe, así que se omiten deliberadamente.

El efecto visible está en **Consumo doméstico**: debería coincidir ahora con la cifra de consumo del propio inversor mientras la batería carga o descarga. Esa corrección es el motivo de esta conexión.

## Modo de operación: NOM y Manual {#modo-de-operacion-nom-y-manual}

Muchas baterías mantienen su propio contador a 0 W por sí solas. Eso es **NOM** (cero en el contador). **Manual** es lo contrario: la regulación está parada y la batería reposa. Si la integración que lee la batería tiene un select o un interruptor para ello, indícalo en **Entidad de modo de operación**.

La batería tiene entonces un selector **Modo de operación**:

- **Manual** (lo predeterminado en una batería recién añadida): la batería no hace nada. Omnibattery tampoco le envía órdenes de potencia.
- **NOM**: la batería regula primero y las que Omnibattery controla cubren lo que ella deja. Casa a 3000 W y NOM da 1200 W: las demás dan 1800 W. Con 2000 W de excedente solar y NOM tomando 800 W, las demás cargan con los 1200 W restantes. La carga de NOM nunca se convierte en descarga de otra batería.

El selector muestra **lo que has pedido**. Lo que hace la batería está en sus atributos: `effective_mode`, `status`, `suspended`, `suspension_causes`, `last_change`, `last_error`.

Con NOM pedido, Omnibattery mantiene la batería en Manual mientras:

| Causa | Por qué |
|---|---|
| Hay o va a haber una carga de red (predictiva, precio negativo, semanal, franja manual, carga manual de otra batería) | La batería vería la importación y descargaría hacia ella |
| Un dispositivo excluido está en marcha, entero o en parte | No puede dejar sin cubrir esa demanda. Excedente solar, Cubrir Hogar y control dinámico no cambian nada |
| Un dispositivo excluido aún no se ha podido leer, o se perdió estando en marcha | Nada indica que esté apagado |
| El **Modo Manual** global está activo | Mandas tú |
| Descargó mientras otra batería cargaba, en dos lecturas | La energía que pasa de una batería a otra se paga dos veces. Dura 5 minutos desde la última vez |
| Su carga llevó la importación por encima de la **Máximo de Importación de Red** | Mejor pararla que descargar otra batería para compensar. Dura 5 minutos |
| El controlador apunta a propósito a más de 100 W de cero (venta a precio alto, predescarga, balance horario, un objetivo de red fijo así de grande) | Mantiene el contador a 0 W: absorbería lo que se vende o descargaría hacia lo que se compra |
| No se puede leer su potencia | Nadie sabe qué está haciendo |

NOM vuelve solo 30 segundos después de que desaparezca la última causa.

**Una carga de red nunca se cancela por esto.** Espera como máximo 60 segundos, una sola vez, a que se confirme Manual, con el control normal en marcha mientras tanto. Si no se confirma, arranca igual, un aviso de reparación te lo dice, la batería aparece como degradada y Omnibattery lo sigue intentando; el aviso se borra solo. El peor caso es lo que ocurre sin entidad de modo: la batería descarga hacia la carga.

!!! warning "Lo que Omnibattery no puede limitar"
    Puede parar esta batería, pero no fijar su potencia. Los límites de potencia del sistema y por fase se aplican solo a las baterías que Omnibattery controla, así que el total físico puede superarlos.

    Si la batería o su integración están sin conexión y no aceptan Manual, nada puede pararla. Omnibattery lo dice y no afirma que esté parada. Si no se puede leer su potencia y lo último que se vio no fue Manual, las demás baterías no descargan hasta saber una de las dos cosas (el límite de importación sigue mandando).

Reiniciar o recargar conserva tu elección: la batería pasa a Manual al descargarse la integración o al parar Home Assistant y vuelve a NOM unos 30 segundos después de que todo se pueda leer otra vez.

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
| **Modo de operación** dice NOM pero la batería reposa | Una causa la mantiene en Manual | `status` y `suspension_causes` del selector, o la línea bajo él en el panel |
| NOM no vuelve nunca y la causa es un dispositivo excluido | Su sensor no se puede leer, o consume más de 10 W en reposo | La entidad que aparece en `suspension_causes` |
| Aviso «No se ha podido parar» | La entidad de modo no aceptó el cambio | Cámbiala a mano en **Herramientas para desarrolladores → Acciones**; el aviso se borra solo cuando la batería muestre Manual |

!!! warning "Sin entidad de modo"
    Una batería que Omnibattery no puede pausar puede descargarse *dentro* de
    una carga de red que él mismo está pagando. Si la batería observada aplica
    su propia lógica de autoconsumo, cubre la casa desde sus celdas mientras
    Omnibattery compra energía barata de la red para las baterías que sí
    gobierna: parte de lo comprado acaba sustituyendo energía que la otra
    batería habría aportado gratis unas horas más tarde. No se estropea nada ni
    se pierde nada dos veces, pero la carga nocturna puede resultar mayor de lo
    necesario.

    Si su integración tiene un select o un interruptor de modo, configúralo como
    **Entidad de modo de operación** y esto deja de ocurrir. Si no, sube el
    estado de carga mínimo de la otra batería durante las horas en que
    Omnibattery carga de la red, para que deje de descargarse mientras eso
    ocurre.

??? "Detalles avanzados"
    Omnibattery publica la potencia de esta batería solo bajo `battery_power`, nunca bajo `ac_power`. Los agregados usan entonces su respaldo documentado, `-battery_power`, que es lo que aporta al balance doméstico una batería sin puerto de alterna propio.

    La batería sí cuenta para el estado de carga del sistema, la capacidad total y la energía almacenada, porque esas cifras describen la instalación y no lo que Omnibattery pretende hacer con ella. La carga predictiva es el único lugar del que queda fuera.

    La batería queda excluida de `_get_available_batteries`, del reparto de carga, de la escalera de descarga y de la designación de batería primaria, y nunca llega a `apply_setpoint`. También queda fuera de la medida de escasez para baterías acopladas en continua que orienta el orden de carga: un hueco que esta integración no puede llenar haría parecer escaso cualquier día y dejaría paradas las baterías que sí podían haber absorbido la energía.

    Ningún camino de código puede darle una orden: además de filtrarla en el momento de la selección, tanto `_set_battery_power` como el propio `apply_power` del coordinador rechazan cualquier escritura sobre ella —incluido un 0 W en reposo—, de modo que una llamada que olvide el filtro sigue sin alcanzarla.

    Tampoco entra en el rendimiento de ida y vuelta medido que fija el margen de arbitraje. Sus propios contadores pueden ser del todo fiables, pero la energía que compra una decisión de arbitraje recorre el ciclo completo en las baterías que Omnibattery sí gobierna, y solo el rendimiento de estas puede decidir qué diferencial de precio debe superar esa compra.

    Su energía almacenada se mantiene por ahora completamente fuera de la carga predictiva. Contarla exigiría que entrara la mitad almacenada mientras la mitad del hueco libre se queda fuera —Omnibattery no puede ni cargar esta batería desde la red ni liberarla a demanda— y esa separación es un cambio aparte.

    Solo se guarda el modo pedido. El efectivo se lee en vivo de la entidad mapeada; un estado que no es ninguno de los dos cuenta como desconocido. Las escrituras van en segundo plano y se confirman en ciclos de control posteriores; una fallida se reintenta a los 5, 10, 20 y 40 segundos y después cada 60, y NOM se escribe como mucho cada 30 segundos. Tras 60 segundos fuera del modo buscado la batería está en `degraded`. El detector de flujo opuesto necesita las dos potencias por encima de 50 W en dos lecturas distintas separadas al menos 5 segundos; sin entidad de modo solo anota el evento en los diagnósticos.

    El número **Target SOC** escribe en la entidad configurada, redondeado al paso que esa entidad acepta y acotado a su rango.
