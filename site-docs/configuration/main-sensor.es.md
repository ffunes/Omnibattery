# Sensor principal

El primer paso configura las fuentes de datos globales de la integración.

![Configuración del sensor principal](../assets/screenshots/configuration/main-sensor.png){ width="600"  style="display: block; margin: 0 auto;"}

## Sensor de consumo de red

Sensor de Home Assistant que mide el intercambio de potencia con la red (en **W** o **kW**).

!!! tip "Sensores compatibles"
    Cualquier sensor que exponga la potencia de red funciona: Shelly EM, Shelly EM3, Neurio, integraciones de contador inteligente (e.g. `sensor.grid_power`).

!!! warning "Frecuencia de actualización"
    El sensor debe actualizarse lo más rápido posible. El controlador es **dirigido por eventos** —recalcula cada vez que este sensor publica un valor nuevo—, así que la frecuencia de actualización del sensor *es* la frecuencia de control: un sensor más rápido implica una respuesta más rápida y precisa. (Un watchdog de 2 segundos sigue ejecutando el ciclo si el sensor se queda en silencio.)

    El consumo del hogar puede variar varios kilovatios en fracciones de segundo (arranque de electrodomésticos, horno, lavadora…). Los sensores lentos son compatibles, pero su retraso puede hacer que el controlador reaccione a una situación que ya ha cambiado, reduciendo la calidad de la regulación.

    **Recomendado: actualización cada 1–2 segundos.** Los dispositivos Shelly no ofrecen esta cadencia MQTT de forma nativa. Es necesario ejecutar un script dentro del dispositivo; consulta la [referencia de scripts MQTT para Shelly Pro 3EM](../reference/shelly-pro-3em-mqtt-script.md) para ver ejemplos.

    Omnibattery sigue siempre el último valor publicado hasta que supera los **65 segundos de antigüedad**, independientemente del polling rate del sensor. Los sensores que actualizan repetidamente cada 10 segundos o más generan un único aviso de Repairs de Home Assistant por ejecución de la integración; no se emiten avisos recurrentes en el log. Si el sensor es rápido después del siguiente reinicio, el Repair persistente se elimina tras tres actualizaciones.

### Detección automática de kW

Si el atributo `unit_of_measurement` del sensor es `kW`, la integración multiplica el valor por 1000 automáticamente.

### Signo invertido

Activa **"Signo del medidor invertido"** si tu sensor usa la convención opuesta:

| Convención | Importación | Exportación |
|---|---|---|
| Estándar (por defecto) | Valor positivo | Valor negativo |
| Invertida | Valor negativo | Valor positivo |

Déjalo desactivado si no estás seguro.

## Sensor de potencia off-grid *(opcional)*

Puedes configurar un segundo sensor W/kW para el circuito respaldado. Debe ser
una entidad distinta del sensor de consumo de red normal. Al guardarlo aparece
el switch de sistema **Modo de medidor off-grid**, tanto como entidad de Home
Assistant como en la pestaña **Controles** del dashboard.

El ajuste **Signo del medidor off-grid invertido** es independiente del signo
del medidor principal. Actívalo si el sensor off-grid publica el consumo con
signo negativo; solo se aplica mientras este modo está activo.

Mientras el switch está activado, Omnibattery usa ese segundo sensor como fuente
del PD y de las estadísticas derivadas de consumo e importación/exportación. Al
desactivarlo vuelve al sensor principal. La conmutación corta el intervalo de
integración en curso para no contabilizar un salto artificial entre ambos
medidores. Las baterías que estén suministrando por su propio puerto off-grid
siguen excluidas del PD; el resto de baterías disponibles usa el sensor alternativo.

Este switch solo cambia la fuente de datos. No habilita el puerto off-grid/EPS,
no cambia el modo de la batería y no escribe ningún registro relacionado con la
salida de respaldo; el usuario debe habilitar ese puerto por separado.

---

## Potencia máxima contratada

La potencia contratada de tu conexión de red, en **W** (por defecto `7000`).

La integración limita la carga de las baterías para que la **importación de red proyectada nunca supere este límite**, evitando que salte el diferencial. Aplica en **todos los modos** — control normal de setpoint, un objetivo/offset positivo, balance neto horario y carga predictiva desde red — no solo al cargar desde la red de forma programada.

`max_contracted_power` protege la instalación de dos formas complementarias:

- Es un techo estricto para la carga de baterías en todos los modos.
- Mientras una franja de carga predictiva mantiene el control, también es el
  límite de importación de emergencia. Omnibattery detiene primero la carga y
  espera telemetría estabilizada; si la importación física continúa por encima,
  descarga únicamente el exceso confirmado.

Esta protección de emergencia **no** necesita que Protección de Capacidad/Peak
Shaving esté activado. Peak Shaving es una estrategia de reserva opcional e
independiente, con su propio límite configurable. Fuera de una franja de carga
predictiva, el PD normal sigue regulando hacia el objetivo de red configurado.
Consulta [Consumo del hogar durante la carga predictiva](predictive-charging/index.es.md#consumo-del-hogar-durante-una-franja-de-carga).

---

## Sensores de previsión solar *(opcional)*

Para configuraciones nuevas, selecciona el sensor que proporciona la producción
solar **restante de hoy** en **kWh** o **Wh**. Este valor se utiliza directamente
en las decisiones intradía, sin volver a restar la producción medida.

El campo de previsión del día completo se mantiene para entradas legadas que no
se han modificado. Al guardar **Restante de hoy**, sustituye y elimina ese campo
legado, resolviendo el Repair de transición. Las instalaciones existentes pueden
seguir funcionando hasta que cambien el sensor.

Configurarlo aquí lo pone a disposición de:

- **Carga predictiva** (modos Franja Horaria y Precio Dinámico)
- **Retraso de carga solar**

También puedes dejarlo en blanco y configurarlo más tarde desde la sección
**Sensores** de las opciones de la integración.

---

## Sensor de producción solar *(optional)*

Sensor de potencia de producción fotovoltaica (W o kW) en tiempo real de un inversor externo que no está conectado mediante las entradas MPPT de la batería. Se utiliza para mostrar el nodo Solar en el diagrama de flujo energético del panel. Déjalo vacío si tus paneles solares alimentan directamente el MPPT de la batería.

---

## Consumo del hogar *(derivado automáticamente)*

**No hay campo de sensor de consumo del hogar** en la configuración — la integración deriva el consumo total del hogar de sensores que ya tiene:

**Consumo del hogar = Potencia de red + Potencia AC de baterías + Producción solar**

Es el valor que muestra el diagrama de flujo de energía y el sensor `sensor.marstek_venus_system_home_consumption`, y alimenta el historial de 7 días que usan la carga predictiva y el retraso de carga. La acumulación cubre todo el día local, incluidas las franjas de carga predictiva; la potencia AC negativa de la batería cancela la energía de red usada para cargarla. El contador se reinicia a medianoche y sobrevive reinicios de HA.

La telemetría de red, solar y baterías es independiente y puede no representar
exactamente el mismo instante. Justo después de cambiar una orden de carga, su
combinación temporal puede producir un balance doméstico negativo imposible o
anormalmente pequeño. El sensor de Consumo del Hogar conserva el último valor
coherente durante un máximo de **15 segundos**; si las entradas siguen sin
cuadrar, muestra `unknown` en lugar de publicar un `0 W` falso. El acumulador
físico de energía diaria aplica su propia validación equivalente e interrumpe el
intervalo de integración en vez de sumar un cero inventado. Tampoco aplica las
exclusiones de cargas externas predictivas al total físico del panel.

Una telemetría rápida y coherente de red y batería acorta estas transiciones. Un
valor retenido o `unknown` breve durante un cambio de dirección del inversor es,
por tanto, una protección de calidad de datos, no una orden para descargar.

### Total de previsión frente al timeline solar

El sensor de previsión es el presupuesto energético. Un sensor de «restante de
hoy» ya representa energía futura y el acumulador de producción local no lo
reduce otra vez. Los sensores legados de día completo (`today`) se convierten
una sola vez a presupuesto restante mediante sus periodos futuros fechados, o
la parte restante de la curva solar; la producción ya medida nunca se desplaza
a intervalos futuros. El sensor opcional de producción real, y
los canales MPPT legibles de las baterías, solo aprenden la forma intradía; no
sustituyen el total de la previsión.

Cuando está disponible, la prioridad es periodos fechados del proveedor,
perfil solar local maduro y, finalmente, la curva sinusoidal existente. El
perfil no predice kWh, no corrige una previsión meteorológica incorrecta, no
controla el inversor ni garantiza producción cuando hay curtailment.
