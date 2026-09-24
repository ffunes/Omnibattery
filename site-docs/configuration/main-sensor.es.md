# Conecte su medidor de red y límites eléctricos

Omnibattery utiliza su sensor de energía de la red para decidir cuánto deben cargarse o descargarse las baterías. Esta configuración también define el techo eléctrico y los datos del circuito solar o de respaldo opcionales utilizados por otras funciones.

## ¿Lo necesito?

**Úsalo si** estás configurando Omnibattery: cada instalación necesita un sensor de consumo de red y una potencia máxima contratada.

**No necesita** los campos solares, de salida de respaldo o trifásicos opcionales a menos que su instalación utilice la función relacionada.

## Antes de empezar

- Encuentre un Home Assistant `sensor` que informe el intercambio de red en vivo en `W` o `kW`, como Shelly EM/EM3, Neurio o integración de medidores inteligentes.
- Compruebe si los valores positivos significan importación y los valores negativos significan exportación.
- Encuentra tu límite de potencia contratada en vatios.
- Opcional: prepare un pronóstico solar del día restante, un sensor de producción de inversor externo o un medidor de circuito de respaldo separado.

## Cómo activarlo

1. Abra **Configuración → Dispositivos y servicios → Omnibattery → Configurar → Sensores** y seleccione **Sensor de consumo de red**.
2. El poder de importación debería ser positivo. Si su medidor informa que la importación es negativa, habilite **Signo de medidor invertido**.
3. Ingrese **Potencia máxima contratada (W)** para que la carga de la batería no pueda impulsar la importación de red proyectada por encima de ese límite.
4. Agregue solo las fuentes opcionales que necesita: **Sensor de pronóstico solar restante para hoy (recomendado)** para planificación solar, **Sensor de producción solar (opcional)** para un inversor externo o **Sensor de energía fuera de la red (opcional)** para un circuito de respaldo medido por separado.
5. Habilite la **Protección de corriente trifásica** solo cuando pueda proporcionar límites y sensores de corriente firmados para las fases físicas que desea proteger.

![Seleccione los principales sensores Omnibattery](../assets/screenshots/configuration/main-sensor.png){ width="600" style="display: block; margin: 0 auto;"}

!!! warning "La captura de pantalla necesita actualizarse"
    El formulario actual también incluye campos separados de pronóstico del día restante, medidor fuera de la red y protección trifásica que no son visibles en esta captura de pantalla.

## lo que veras

El diagrama de flujo de energía del tablero utiliza el sensor de red y los datos solares y de batería disponibles. Omnibattery también crea **Consumo doméstico** al combinar esas fuentes; no selecciona un sensor de consumo doméstico independiente.

La configuración de un medidor de circuito de respaldo crea un **Modo de medidor fuera de la red**. Actívelo para usar ese medidor para control y estadísticas de la red, y apáguelo para regresar al medidor principal. Este interruptor de software no habilita la salida de respaldo física (EPS) de la batería.

Un pronóstico solar del resto del día estará disponible para predecir la carga y el retraso de la carga solar. La producción solar externa añade el nodo solar al diagrama de flujo de energía cuando los paneles están conectados a un inversor independiente.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| La batería aleja la energía de la red del objetivo | El signo del metro está invertido | Importe una pequeña cantidad de la red y confirme que el sensor sea positivo; de lo contrario habilite **Signo de medidor invertido** |
| El control reacciona tarde o sobrepasa las cargas cambiantes | El sensor de red publica demasiado lento | Verificar el historial de la entidad y acortar su intervalo de actualización cuando el medidor lo admita |
| **Falta el modo de medidor fuera de la red** | No se guardó ningún medidor de circuito de respaldo separado | Configure el **Sensor de energía fuera de la red (opcional)** y asegúrese de que sea diferente del sensor principal |
| Falta el nodo solar | Omnibattery no tiene fuente externa de producción solar | Configurar **Sensor de producción solar (opcional)** solo para energía solar que no se mide a través de entradas de seguimiento del punto de máxima potencia (MPPT) de la batería |
| La carga predictiva ignora el pronóstico | Se seleccionó un valor de todo el día como energía restante o la unidad del sensor está equivocada | Prefiera el pronóstico restante de hoy y confírmelo en los informes `Wh` o `kWh` |
| **Consumo en el hogar** se mantiene brevemente o se convierte en `unknown` durante un cambio de dirección | Las lecturas de red, batería y energía solar describen diferentes instantes | Comprobar que las fuentes se actualizan puntualmente y luego esperar lecturas coherentes |

??? "Detalles avanzados"
    **Cadencia del medidor y datos obsoletos**

    El controlador vuelve a calcular cada vez que el sensor de red publica. También se ejecuta un ciclo de seguridad cada 2 segundos. Se recomienda un intervalo de actualización de 1 a 2 segundos; Los intervalos de 10 segundos o más activan una reparación de Home Assistant después de 3 informes lentos consecutivos. La reparación se borra después de 20 intervalos consecutivos más rápidos. La última lectura sigue teniendo autoridad hasta que tenga más de 65 segundos.

    La demanda del hogar puede cambiar en varios kilovatios entre lecturas lentas, por lo que el controlador puede responder a una carga que ya ha cambiado.

    Si utiliza un medidor Shelly, consulte los [scripts MQTT de Shelly Pro 3EM](../hardware/shelly-pro-3em-mqtt-script.md) para obtener una cadencia de publicación más rápida.

    Los sensores con `unit_of_measurement: kW` se convierten a vatios automáticamente.

    **Protección de potencia contratada**

    El valor de configuración predeterminado es 7000 W y acepta entre 1000 y 20 000 W. Limita la carga de la batería en control normal, objetivos positivos, saldo neto por hora y carga de red predictiva. Durante un intervalo de carga predictiva, Omnibattery primero deja de cargar si la importación alcanza el límite; una vez que la telemetría se estabilice, puede descargar el exceso confirmado. [Protección de capacidad (reducción de picos)](../features/peak-shaving.md) es una estrategia de reserva separada.

    **Fuentes solares**

    Un pronóstico restante de hoy ya es energía futura, por lo que Omnibattery no le resta la producción medida. El campo heredado de todo el día permanece disponible para las entradas existentes; guardar un sensor restante de hoy lo reemplaza. El sensor de producción externo en tiempo real y los canales MPPT de batería legibles ayudan a conocer la forma de producción, pero no reemplazan el pronóstico total.

    Cuando hay períodos de proveedores con fecha disponibles, Omnibattery los utiliza para la línea de tiempo solar. De lo contrario, utiliza un perfil local maduro y luego un retroceso sinusoidal. El perfil no predice la energía total ni corrige el proveedor meteorológico.

    **Consumo derivado de la vivienda**

    ```text
    home consumption = grid power + battery alternating current (AC) power + solar power
    ```

    El valor alimenta un historial de consumo de 7 días utilizado por la carga predictiva y el retraso de la carga solar. Se acumula durante el día local, se reinicia a medianoche y sobrevive a los reinicios de Home Assistant. La energía de carga de la batería anula la energía de la red utilizada para cargarla.

    Debido a que las fuentes se actualizan de forma independiente, un cambio de dirección puede crear brevemente un equilibrio imposible. **Consumo doméstico** mantiene su último valor coherente durante hasta 15 segundos y luego informa `unknown` si las entradas aún no están de acuerdo. Su acumulador físico de energía diaria rompe ese intervalo en lugar de añadir un falso cero. Las exclusiones de carga externa utilizadas para el control no alteran el total de este panel físico.

    Al seleccionar **Modo de medidor fuera de la red** se interrumpe el intervalo de integración de energía actual, por lo que un salto entre medidores no se cuenta como energía. La configuración del letrero fuera de la red se aplica solo a esa fuente. Las baterías que alimentan su propia salida de respaldo permanecen fuera del control proporcional-derivado (PD), mientras que las otras baterías disponibles usan el medidor seleccionado.
