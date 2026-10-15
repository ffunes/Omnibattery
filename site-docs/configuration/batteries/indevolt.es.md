# Indevolt SolidFlex / PowerFlex

Omnibattery controla una batería Indevolt a través de la API HTTP local de la batería. No necesita conexión a la nube.

## ¿Lo necesito?

**Úsalo si** tienes una batería Indevolt SolidFlex o PowerFlex y Home Assistant puede alcanzarla en tu red local. La SolidFlex 2000 está probada. La PowerFlex 2000, las SolidFlex y PowerFlex 3000 y la SolidFlex 1200 usan la misma API; si usas una de ellas, indica tu modelo y firmware.

**Usa Omnibattery en lugar de la integración oficial de Indevolt.** Ambas consultan la misma batería, y la batería atiende las peticiones de una en una. Omnibattery muestra las mismas medidas, así que desactiva la integración oficial para una batería que controle Omnibattery.

## Antes de empezar

- En la app de Indevolt, activa la **API local** con el protocolo **HTTP** (no HTTP Digest) y da a la batería una dirección IP fija.
- En la app de Indevolt, pon el modo de trabajo en **Tiempo real** (Realtime). Omnibattery solo controla la batería en modo Tiempo real y nunca cambia el modo por sí mismo (ver [por qué](#por-que-omnibattery-solo-escribe-ajustes-cuando-los-cambias)).
- La **reserva** de la batería (SOC de respaldo), la **entrada AC máxima** y la **salida AC máxima** son los mismos ajustes que el SOC mínimo, la potencia máxima de carga y la potencia máxima de descarga de Omnibattery. Omnibattery los mantiene sincronizados, así que puedes cambiarlos en cualquiera de los dos sitios.
- Desactiva la integración oficial de Indevolt para esta batería.

## Cómo añadirla

1. En el asistente de Omnibattery, elige **Indevolt SolidFlex / PowerFlex**.
2. Introduce un **Nombre** descriptivo y la **dirección IP** de la batería.
3. Deja el **Puerto HTTP** en `8080`.
4. Omnibattery lee la identidad, el modo de trabajo y los límites de potencia de la batería. No cambia nada en la batería.
5. Si la batería no está en modo Tiempo real, un aviso lo indica. La configuración continúa; el control empieza cuando eliges Tiempo real en la app.
6. Elige los límites comunes de potencia y estado de carga. La primera vez que arranca, Omnibattery adopta la reserva y los límites de potencia de la batería en lugar de escribir los tuyos; después puedes cambiarlos en cualquiera de los dos sitios.

## Qué verás

- **El control empieza solo.** En cuanto la batería está en modo Tiempo real, Omnibattery envía una orden de parada, comprueba que la batería está en reposo y toma el control. Lo mismo ocurre tras cada reinicio de Home Assistant.
- **Control Status** muestra qué hace el driver: `active`, `starting`, `realtime_mode_required`, `handover_failed`, `telemetry_stale` o `unknown_model`.
- **Request Interval**, **Read Latency (P95)**, **Write Latency (P95)** y **Request Failures** (diagnóstico) muestran cómo responde la batería. El intervalo es normalmente de 5s y aumenta cuando Omnibattery da más tiempo a una batería lenta; cada latencia es el 5% más lento de las últimas 200 lecturas o escrituras (las lecturas suelen tardar decenas de milisegundos, las escrituras unos cientos); los fallos cuentan los tiempos de espera agotados y los errores desde que se inició Home Assistant.
- **Cambiar de modo en la app es seguro.** Si eliges otro modo de trabajo, Omnibattery se pausa, muestra una notificación y deja la batería en paz. Vuelve a Tiempo real y retoma el control automáticamente.
- **Tiempo de respuesta.** Omnibattery envía como mucho una petición cada 5s, como recomienda Indevolt. Una nueva consigna sale en el siguiente hueco y la batería la sigue en pocos segundos.
- **Ajustes:** **Discharge Cutoff** (la reserva de la batería, que es el SOC mínimo de Omnibattery), **Max Charge Power** y **Max Discharge Power**. Un cambio aquí se escribe en la batería; un cambio en la app de Indevolt aparece aquí en pocos minutos. Los controles de potencia llegan hasta el máximo de hardware de la batería. **Charge Cutoff** (SOC máximo) no tiene ajuste en la batería y lo aplica solo Omnibattery.
- **Sensores:** estado de carga, potencia de batería, potencia AC, contadores de energía, modo de trabajo y versión de firmware. Temperaturas: **Inverter Battery Temperature** para la batería integrada en la unidad del inversor y **Expansion Battery N Temperature** para cada batería de expansión presente; **Internal Temperature**, que el límite de carga por temperatura opcional de Omnibattery usa para reducir la carga cuando la batería está caliente, es la más alta de ellas. Hay un sensor de temperatura del transformador, desactivado por defecto.

## Por qué Omnibattery solo escribe ajustes cuando los cambias

!!! info "Órdenes en tiempo real en memoria de trabajo, ajustes solo a petición"
    Una batería guarda dos tipos de datos. Sus ajustes, como el modo de trabajo, la reserva y los límites de potencia, se guardan en memoria flash para que sobrevivan a un corte de corriente. La memoria flash se desgasta tras un número limitado de escrituras. Las órdenes en tiempo real se guardan en memoria de trabajo (RAM), que se puede escribir tantas veces como haga falta.

    Lo que desgasta la flash es la frecuencia de escritura. Un controlador como Omnibattery puede enviar una orden nueva cada pocos segundos; si esas órdenes fueran a flash, podrían desgastar el controlador de la batería mucho antes del final de su vida normal. Un ajuste que cambias unas pocas veces al año no cuesta nada.

    Lo comprobamos con los desarrolladores de Indevolt. Confirmaron que solo la orden en tiempo real (cargar, descargar o parar, con una potencia y un SOC objetivo) se guarda en memoria de trabajo y que el resto de ajustes se guarda en flash. Por eso:

    - El control solo escribe la orden en tiempo real, por muy a menudo que se ejecute.
    - La reserva y los dos límites de potencia se escriben solo cuando los cambias, en los ajustes de Omnibattery o en la página del dispositivo. Omnibattery espera 3s a que termines, no escribe si la batería ya tiene ese valor y acepta como mucho 10 cambios por ajuste cada 24h. No se escribe nada al arrancar.
    - El modo de trabajo y los demás ajustes nunca se escriben. Cámbialos en la app de Indevolt.

    Las protecciones del código y las pruebas automáticas lo garantizan, para que un cambio futuro no pueda añadir una escritura por accidente.

Omnibattery aplica su propio SOC mínimo y máximo por software. Cada orden lleva además un SOC objetivo unos pocos puntos más allá de la carga actual en la dirección pedida, que Omnibattery va moviendo mientras la batería carga o descarga. Si Home Assistant se detiene en mitad de una orden, la batería se para sola en unos 5% de estado de carga.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El asistente no conecta | La API local está desactivada, o la dirección o el puerto no son correctos | Activa la API local en la app de Indevolt y revisa la dirección IP y el puerto `8080` |
| El asistente dice que la batería pide una contraseña | La API local está en HTTP Digest | Elige **HTTP** para la API local en la app de Indevolt |
| Control Status es `realtime_mode_required` | La batería está en otro modo de trabajo | Elige **Tiempo real** en la app de Indevolt; el control se reanuda solo |
| Una notificación dice que la batería va lenta, o Request Interval supera 5s | Otro cliente también la consulta, o la señal Wi-Fi es débil | Desactiva la integración oficial de Indevolt para esta batería y revisa la señal |
| Una notificación dice que un ajuste no se aplicó | La batería no aceptó el valor | La batería conserva su valor; inténtalo más tarde o cámbialo en la app de Indevolt |
| Una notificación dice que un ajuste se cambió demasiadas veces | Más de 10 cambios de un ajuste en 24h, a menudo por una automatización | Espera un día y revisa las automatizaciones que fijan estos valores |
| Control Status es `unknown_model` | La batería indica un tipo de dispositivo que Omnibattery aún no conoce | Abre un issue en GitHub con el tipo de dispositivo de la notificación |
| Control Status es `handover_failed` | La batería no confirmó el reposo tras la orden de parada | Revisa la batería en la app de Indevolt; Omnibattery lo reintenta solo |

??? "Detalles avanzados"
    Todas las peticiones a una batería comparten un único presupuesto: una petición cada vez, con al menos 5s entre el inicio de una y el de la siguiente. El sondeo propio de Omnibattery solo lee una caché, así que nunca añade peticiones. Una orden pendiente ocupa el siguiente hueco. Si la batería responde despacio o no responde, el intervalo crece hasta 20s y vuelve a 5s cuando las respuestas son normales; un solo tiempo de espera agotado no pausa el control.

    La telemetría se lee por lotes: potencia, potencia AC, modo de trabajo y SOC en cada hueco, el estado en tiempo real cada unos 30s, los límites cada pocos minutos y los contadores de energía cada minuto. Si la batería devuelve alguna vez un lote incompleto, Omnibattery lee lotes más pequeños hasta que las respuestas vuelvan a estar completas.

    La potencia AC entregada es la potencia AC total de la batería menos la del puerto bypass, para que una carga o un microinversor en ese puerto no cuente como salida de la batería.

    El firmware mínimo de Indevolt para la API local es CMS V1406.07.002E para los modelos 2000, V1409.08.3034 para la familia 3000 y V1407.07.202E para la SolidFlex 1200. Omnibattery muestra una notificación si la batería indica una versión anterior.

    Para los controles comunes y los límites del sistema, consulta [Elegir la conexión de la batería](index.md).
