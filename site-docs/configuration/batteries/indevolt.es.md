# Indevolt SolidFlex / PowerFlex

Omnibattery controla una batería Indevolt a través de la API HTTP local de la batería. No necesita conexión a la nube.

## ¿Lo necesito?

**Úsalo si** tienes una batería Indevolt SolidFlex o PowerFlex y Home Assistant puede alcanzarla en tu red local. La SolidFlex 2000 está probada. La PowerFlex 2000, las SolidFlex y PowerFlex 3000 y la SolidFlex 1200 usan la misma API; si usas una de ellas, indica tu modelo y firmware.

**Usa Omnibattery en lugar de la integración oficial de Indevolt.** Ambas consultan la misma batería, y la batería atiende las peticiones de una en una. Omnibattery muestra las mismas medidas, incluidos los contadores de energía AC para el panel de Energía, así que desactiva o elimina la integración oficial para una batería que controle Omnibattery (consulta [Pasar desde la integración oficial de Indevolt](#pasar-desde-la-integracion-oficial-de-indevolt)).

## Antes de empezar

- En la app de Indevolt, activa la **API local** con el protocolo **HTTP** (no HTTP Digest) y da a la batería una dirección IP fija. Omnibattery no usa **Modbus TCP**, así que puedes dejarlo desactivado. En las pruebas, la batería respondió a las peticiones Modbus mucho más despacio que a las peticiones HTTP, y la conexión Modbus era menos estable y requería reconexiones frecuentes.
- En la app de Indevolt, pon el modo de trabajo en **Tiempo real** (Realtime). Omnibattery solo controla la batería en modo Tiempo real y nunca cambia el modo por sí mismo (ver [por qué](#por-que-omnibattery-solo-escribe-ajustes-cuando-los-cambias)).
- La **reserva** de la batería (SOC de respaldo), la **entrada AC máxima** y la **salida AC máxima** son los mismos ajustes que el SOC mínimo, la potencia máxima de carga y la potencia máxima de descarga de Omnibattery. Omnibattery los mantiene sincronizados, así que puedes cambiarlos en cualquiera de los dos sitios.
- Desactiva la integración oficial de Indevolt para esta batería.

## Cómo añadirla

1. En el asistente de Omnibattery, elige **Indevolt SolidFlex / PowerFlex**.
2. Introduce un **Nombre** descriptivo y la **dirección IP** de la batería.
3. Deja el **Puerto HTTP** en `8080` y la **Velocidad de solicitudes** en **Conservadora (5s)** (consulta [Elegir la velocidad de solicitudes](#elegir-la-velocidad-de-solicitudes)).
4. Omnibattery lee la identidad, el modo de trabajo y los límites de potencia de la batería. No cambia nada en la batería.
5. Si la batería no está en modo Tiempo real, un aviso lo indica. La configuración continúa; el control empieza cuando eliges Tiempo real en la app.
6. Elige los límites comunes de potencia y estado de carga. La primera vez que arranca, Omnibattery adopta la reserva y los límites de potencia de la batería en lugar de escribir los tuyos; después puedes cambiarlos en cualquiera de los dos sitios.

## Qué verás

- **El control empieza solo.** En cuanto la batería está en modo Tiempo real, Omnibattery envía una orden de parada, comprueba que la batería está en reposo y toma el control. Lo mismo ocurre tras cada reinicio de Home Assistant.
- **Control Status** muestra qué hace el driver: `active`, `starting`, `realtime_mode_required`, `handover_failed`, `telemetry_stale` o `unknown_model`.
- **Request Interval (Effective)**, **Read Latency (P95)**, **Write Latency (P95)** y **Request Failures** (diagnóstico) muestran cómo responde la batería. **Request Speed** (diagnóstico) muestra la velocidad de solicitudes elegida. El intervalo normalmente coincide con ella y aumenta cuando Omnibattery da más tiempo a una batería lenta; cada latencia es el 5% más lento (P95, es decir, el 95% de las respuestas son más rápidas) de las últimas 200 lecturas o escrituras, y muestra lo rápido que la batería responde a las peticiones de Omnibattery, no lo rápido que cambia su potencia, que tarda unos segundos (las lecturas suelen tardar decenas de milisegundos, las escrituras unos cientos); los fallos cuentan los tiempos de espera agotados y los errores desde que se inició Home Assistant. **Flash Writes** (diagnóstico) cuenta las escrituras de Omnibattery de la reserva y los límites de potencia en la memoria flash de la batería (consulta «Por qué Omnibattery solo escribe ajustes cuando los cambias», más abajo).
- **Cambiar de modo en la app es seguro.** Si eliges otro modo de trabajo, Omnibattery se pausa, muestra una notificación y deja la batería en paz. Vuelve a Tiempo real y retoma el control automáticamente.
- **Tiempo de respuesta.** Omnibattery lee la batería una vez por intervalo de la velocidad de solicitudes (5s por defecto, 2s en Máxima). Una nueva consigna sale en cuanto han pasado 2s desde la petición anterior (1s en Normal y Rápida; en Máxima 0.2s después de la respuesta anterior, como mucho una consigna por segundo), y la batería la sigue en pocos segundos.
- **Ajustes:** **Discharge Cutoff** (la reserva de la batería, que es el SOC mínimo de Omnibattery), **Max Charge Power** y **Max Discharge Power**. Un cambio aquí se escribe en la batería; un cambio en la app de Indevolt aparece aquí en pocos minutos. Los controles de potencia llegan hasta el máximo de hardware de la batería. **Charge Cutoff** (SOC máximo) no tiene ajuste en la batería y lo aplica solo Omnibattery.
- **Sensores:** estado de carga, potencia de batería, potencia AC, modo de trabajo y versión de firmware, y además:
    - **AC Input Power** y **AC Output Power** en la conexión AC de la batería (leídas cada 10s), y **Bypass Power** a través de la toma bypass.
    - Contadores de energía en dos lados. **Total AC Input Energy** y **Total AC Output Energy** cuentan en la conexión AC: usa estos para la batería en el panel de Energía. **Total Charging Energy**, **Total Discharging Energy** y los contadores diarios cuentan en el lado de la batería, después de las pérdidas de conversión. **Off-Grid Output Energy** y **Bypass Input Energy** cuentan lo que pasó por las tomas de respaldo y bypass.
    - Para una batería con paneles solares conectados: **Solar Output Power**, **Daily Solar Energy** y **Total Solar Energy**. La potencia, la tensión y la corriente por entrada PV están disponibles, pero desactivadas por defecto.
    - Temperaturas: **Inverter Battery Temperature** para la batería integrada en la unidad del inversor y **Expansion Battery N Temperature** para cada batería de expansión presente; **Internal Temperature**, que el límite de carga por temperatura opcional de Omnibattery usa para reducir la carga cuando la batería está caliente, es la más alta de ellas. Hay un sensor de temperatura del transformador, desactivado por defecto.
- **Diagnóstico:** **Realtime Command** muestra qué hace el modo Realtime de la batería (en espera, cargando o descargando). **Feed-in Limit** y **Grid Charging** muestran esos ajustes de la batería; cámbialos en la app de Indevolt. **MAC Address** muestra la dirección MAC que indica la batería; el panel también la muestra en Información del dispositivo. Desactivados por defecto: tensión y frecuencia de red, tiempo restante de carga y de descarga, modo del dispositivo (independiente o clúster) y, para cada batería presente (la de la unidad del inversor y cada batería de expansión), su SOC, ciclos de carga, temperatura MOS, tensión, corriente y número de serie. Omnibattery solo lee esos detalles de las baterías presentes.
- **Indevolt Meter Power** (desactivado por defecto) es la potencia de red que indica el medidor emparejado con la batería en la app de Indevolt. Va 10–20s por detrás de tu medidor de red, así que no lo uses como sensor de red de Omnibattery.

## Pasar desde la integración oficial de Indevolt

Omnibattery muestra las medidas de la integración oficial de Indevolt con sus propios nombres, leídas en las peticiones que ya hace. Apunta tus paneles, ayudantes y el panel de Energía a los sensores de Omnibattery y después elimina la integración oficial.

| Integración oficial | Omnibattery | Nota |
|---|---|---|
| Battery SOC | Battery SOC | |
| Battery power | Battery Power | Signo opuesto: positivo mientras carga |
| AC input power, AC output power | AC Input Power, AC Output Power | |
| | AC Power | Salida menos entrada: positivo mientras descarga, como espera el panel de Energía para la potencia de la batería |
| Bypass power | Bypass Power | |
| Total AC input energy, Total AC output energy | Total AC Input Energy, Total AC Output Energy | La energía que entra y sale de la batería en el panel de Energía |
| Battery daily and total charging and discharging energy | Total Daily Charging Energy, Total Daily Discharging Energy, Total Charging Energy, Total Discharging Energy | |
| Off-grid output energy, Bypass input energy | Off-Grid Output Energy, Bypass Input Energy | Bypass input energy en kWh en lugar de Wh |
| DC output power, Daily production, Cumulative production | Solar Output Power, Daily Solar Energy, Total Solar Energy | Total solar energy en kWh en lugar de Wh |
| Meter power, Meter connected | Indevolt Meter Power, Indevolt Meter Connected | Desactivados por defecto |
| Battery charge/discharge state | Inverter State | Standby, Charging o Discharging |
| Energy mode | Working Mode | Solo lectura |
| Real-time mode, Real-time target SOC, Real-time power limit | Realtime Command, Realtime Target SOC, Realtime Command Power | |
| Rated capacity | Battery Total Energy | |
| Discharge limit, Max AC output power, Inverter input limit | Discharge Cutoff, Max Discharge Power, Max Charge Power | Ajustes, sincronizados con la batería |
| Feed-in power limit, Allow grid charging | Feed-in Limit, Grid Charging | Solo lectura |
| Tensión y frecuencia de red, tiempo restante de carga y de descarga, modo del dispositivo, potencia, tensión y corriente por entrada PV y, por batería, SOC, ciclos, temperaturas, tensión, corriente y número de serie | Los mismos nombres | Desactivados por defecto, como en la integración oficial |

Omnibattery no cambia desde Home Assistant el modo de trabajo, el indicador LED, la toma bypass, la carga desde la red ni el límite de inyección: cada uno es un ajuste permanente de la batería, así que cámbialo en la app de Indevolt (consulta [por qué](#por-que-omnibattery-solo-escribe-ajustes-cuando-los-cambias)). El botón de parada y las acciones de carga y descarga de la integración oficial competirían con el control de Omnibattery; usa el modo manual de Omnibattery. **Equivalent full cycles** de la integración oficial muestra el intervalo de carga completa de la batería en días, no un número de ciclos; los sensores **Cycles** por batería son los contadores de ciclos de la propia batería.

## Elegir la velocidad de solicitudes

La **Velocidad de solicitudes** fija con qué frecuencia Omnibattery habla con la batería. La eliges por batería al añadirla y puedes cambiarla después con **Reconfigurar** en Omnibattery, en **Ajustes → Dispositivos y servicios**.

| Velocidad | Intervalo | Cuándo usarla |
|---|---|---|
| Máxima | 1s | Por debajo del mínimo documentado por Indevolt, bajo tu responsabilidad. Solo sirve con un contador de red que se actualiza cada segundo: puede salir una consigna cada segundo, y una lectura sigue a una respuesta a los 0.2s en lugar de esperar 1s. Las lecturas siguen cada 2s; como mucho 1.5 peticiones por segundo. Pon también **PD Min Cycle Interval** a 0 (consulta el consejo más abajo). |
| Rápida | 2s | Una conexión fuerte y estable, y quieres la reacción más rápida. Envía un 50% más de peticiones que Normal. |
| Normal | 3s | Un contador de red que se actualiza cada 3s: reacciona antes que Conservadora con poca carga adicional. |
| Conservadora (por defecto) | 5s | Recomendada por Indevolt, y suficiente para un contador de red que se actualiza cada 5s, el ritmo habitual en Home Assistant. También es la opción con Wi-Fi débil, si otras apps también consultan la batería o si Omnibattery avisa de que la batería va lenta. |

Todo lo que sea más rápido que los 5s de Indevolt va bajo tu responsabilidad, y te corresponde comprobar que tu batería, Home Assistant y tu red pueden dar abasto. En la página del dispositivo de la batería, **Request Interval (Effective)** debe seguir coincidiendo con tu elección, y **Read Latency (P95)** y **Write Latency (P95)** deben quedarse muy por debajo de 1s (las lecturas suelen tardar decenas de milisegundos, las escrituras unos cientos). Si el intervalo sigue por encima de tu elección o aparece una notificación de batería lenta, elige una velocidad más lenta. Puedes probar una velocidad mayor: si la batería responde despacio o no responde, el driver de Indevolt de Omnibattery vuelve automáticamente a un ritmo más tranquilo hasta que el tiempo de respuesta se recupera.

Una velocidad mayor hace llegar antes cada nueva consigna a la batería, así que reacciona antes a los cambios del contador, lo que puede mejorar el balance neto cero. No hace que la propia batería suba o baje su potencia más rápido, y la batería solo actualiza su potencia AC cada 6s aproximadamente. Una velocidad más rápida que tu contador de red no gana nada: con un contador que se actualiza cada 2s, elige Rápida en lugar de Máxima. Salvo en Máxima, Omnibattery nunca envía dos peticiones con menos de 1s de separación, el mínimo que documenta Indevolt (2s en Conservadora). En Máxima espera 0.2s después de cada respuesta, siempre con una sola petición cada vez, y vuelve a Rápida con la separación de 1s cuando la batería responde despacio. Elijas lo que elijas, reduce el ritmo por sí mismo cuando la batería responde despacio o no responde.

!!! tip "¿Solo baterías Indevolt? Pon PD Min Cycle Interval a 0"
    El **PD Min Cycle Interval** de Omnibattery (1.0s por defecto) descarta una actualización del contador que llega demasiado pronto tras la ejecución de control anterior, para que un contador rápido no sature de órdenes un puente Modbus lento, como un Elfin EW11. El driver de Indevolt ya marca su propio ritmo de peticiones y solo envía la consigna más reciente, así que para una Indevolt este intervalo no protege nada. Solo añade retraso: una actualización descartada espera a la siguiente ejecución de control, hasta 2s más tarde.

    Si todas las baterías de Omnibattery son Indevolt, ponlo a `0`: abre el panel lateral de Omnibattery, selecciona **Control** y, en **Controlador PD (avanzado)**, pon **Intervalo mín. de ciclo** a 0. Se aplica al momento. La batería no recibe más peticiones, porque la velocidad de solicitudes sigue marcando el ritmo. Mantén el valor por defecto si Omnibattery también controla una batería detrás de un puente Modbus lento. Los demás ajustes PD se explican en [seguir el consumo de casa](../../features/pd-controller.md).

## Por qué Omnibattery solo escribe ajustes cuando los cambias

!!! info "Órdenes en tiempo real en memoria de trabajo, ajustes solo a petición"
    Una batería guarda dos tipos de datos. Sus ajustes, como el modo de trabajo, la reserva y los límites de potencia, se guardan en memoria flash para que sobrevivan a un corte de corriente. La memoria flash se desgasta tras un número limitado de escrituras. Las órdenes en tiempo real se guardan en memoria de trabajo (RAM), que se puede escribir tantas veces como haga falta.

    Lo que desgasta la flash es la frecuencia de escritura. Un controlador como Omnibattery puede enviar una orden nueva cada pocos segundos; si esas órdenes fueran a flash, podrían desgastar el controlador de la batería mucho antes del final de su vida normal. Un ajuste que cambias unas pocas veces al año no cuesta nada.

    Lo comprobamos con los desarrolladores de Indevolt. Confirmaron que solo la orden en tiempo real (cargar, descargar o parar, con una potencia y un SOC objetivo) se guarda en memoria de trabajo y que el resto de ajustes se guarda en flash. Por eso:

    - El control solo escribe la orden en tiempo real, por muy a menudo que se ejecute.
    - La reserva y los dos límites de potencia se escriben solo cuando los cambias, en los ajustes de Omnibattery o en la página del dispositivo. Omnibattery espera 3s a que termines y no escribe si la batería ya tiene ese valor. No se escribe nada al arrancar.
    - Una red de seguridad detecta una automatización o un fallo que los escribe una y otra vez sin que nadie lo note; está para proteger tu batería, no para limitarte. Tras 100 escrituras en un año (los tres ajustes juntos), Omnibattery pausa estas escrituras hasta que vuelvas a permitirlas en **Ajustes → Reparaciones**, con un solo clic. Si cambias los ajustes tú mismo te quedas muy por debajo; una automatización que cambia la reserva cada día llega al límite tras unos 100 días. Los cambios hechos en la app de Indevolt se siguen adoptando. **Flash Writes** (diagnóstico), en la página del dispositivo, cuenta todas las escrituras hasta ahora.
    - El modo de trabajo y los demás ajustes nunca se escriben. Cámbialos en la app de Indevolt.

    Las protecciones del código y las pruebas automáticas lo garantizan, para que un cambio futuro no pueda añadir una escritura por accidente.

Omnibattery aplica su propio SOC mínimo y máximo por software. Cada orden lleva además un SOC objetivo unos pocos puntos más allá de la carga actual en la dirección pedida, que Omnibattery va moviendo mientras la batería carga o descarga. Si Home Assistant se detiene en mitad de una orden, la batería se para sola en unos 5% de estado de carga.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El asistente no conecta | La API local está desactivada, o la dirección o el puerto no son correctos | Activa la API local en la app de Indevolt y revisa la dirección IP y el puerto `8080` |
| El asistente dice que la batería pide una contraseña | La API local está en HTTP Digest | Elige **HTTP** para la API local en la app de Indevolt |
| Control Status es `realtime_mode_required` | La batería está en otro modo de trabajo | Elige **Tiempo real** en la app de Indevolt; el control se reanuda solo |
| Una notificación dice que la batería va lenta, o Request Interval (Effective) se mantiene por encima de la velocidad elegida | Otro cliente también la consulta, o la señal Wi-Fi es débil | Desactiva la integración oficial de Indevolt para esta batería, revisa la señal o elige la velocidad de solicitudes **Conservadora (5s)** |
| Una notificación dice que un ajuste no se aplicó | La batería no aceptó el valor | La batería conserva su valor; inténtalo más tarde o cámbialo en la app de Indevolt |
| Reparaciones dice que la escritura de ajustes de Indevolt está en pausa | 100 escrituras de la reserva y los límites de potencia en un año, casi siempre por una automatización o un script | Revisa las automatizaciones y scripts que fijan estos valores y vuelve a permitir la escritura en **Ajustes → Reparaciones** (también si los cambios eran intencionados) |
| Control Status es `unknown_model` | La batería indica un tipo de dispositivo que Omnibattery aún no conoce | Abre un issue en GitHub con el tipo de dispositivo de la notificación |
| Control Status es `handover_failed` | La batería no confirmó el reposo tras la orden de parada | Revisa la batería en la app de Indevolt; Omnibattery lo reintenta solo |

??? "Detalles avanzados"
    Todas las peticiones a una batería comparten un único presupuesto: una petición cada vez, con al menos 1s entre el inicio de una y el de la siguiente (2s en Conservadora y mientras va más despacio, a 5s o 10s). Máxima cuenta desde la respuesta anterior: al menos 0.2s después, con órdenes separadas al menos 1s y lecturas cada 2s. Las lecturas llegan una vez por intervalo de la velocidad de solicitudes. Una orden pendiente va antes que una lectura pendiente, al menos esa separación después de la petición anterior y al menos un intervalo después de la orden anterior. El sondeo propio de Omnibattery solo lee una caché, así que nunca añade peticiones.

    Cuando la batería tiene problemas, el intervalo recorre una escala fija: 2s, 3s, 5s, 10s (desde Máxima, el primer escalón es Rápida con la separación de 1s). Dos respuestas malas en 30s (un tiempo de espera agotado, un error, una lectura de más de 1s o una escritura de más de 2s) lo suben un escalón, como mucho un escalón cada 10s; una sola respuesta mala se trata como un fallo puntual. Tras 30s sin respuestas malas baja un escalón, nunca por debajo de la velocidad elegida. 10s es el último escalón: más lento solo haría que el control trabajara con datos más antiguos durante más tiempo. Un solo tiempo de espera agotado no pausa el control.

    La telemetría se lee por lotes: potencia, potencia AC, modo de trabajo y SOC en cada hueco, el estado en tiempo real cada unos 30s, los límites cada pocos minutos y los contadores de energía cada minuto. Si la batería devuelve alguna vez un lote incompleto, Omnibattery lee lotes más pequeños hasta que las respuestas vuelvan a estar completas.

    La potencia AC entregada es la potencia AC total de la batería menos la del puerto bypass, para que una carga o un microinversor en ese puerto no cuente como salida de la batería.

    El firmware mínimo de Indevolt para la API local es CMS V1406.07.002E para los modelos 2000, V1409.08.3034 para la familia 3000 y V1407.07.202E para la SolidFlex 1200. Omnibattery muestra una notificación si la batería indica una versión anterior.

    Para los controles comunes y los límites del sistema, consulta [Elegir la conexión de la batería](index.md).
