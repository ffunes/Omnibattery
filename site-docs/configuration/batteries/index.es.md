# Configuración de baterías

Omnibattery puede coordinar hasta diez baterías en una misma instalación.
Selecciona la marca de cada unidad en el asistente de Home Assistant y consulta
después la página correspondiente para sus campos de conexión y límites
específicos. El bucle de control, el dashboard, la carga predictiva y la mayor
parte de los controles en tiempo de ejecución son comunes.

## Elige una marca

| Marca | Conexión | Documentación |
|---|---|---|
| **Marstek** | Modbus TCP, Modbus RTU o puente LilyGo/ESPHome | [Marstek](marstek.md) |
| **Zendure** | API HTTP local | [Zendure](zendure.md) |
| **Anker SOLIX** | Modbus TCP | [Anker SOLIX](anker.md) |
| **Sessy** | API HTTP local mediante el dongle de Sessy | [Sessy](sessy.md) |
| **Hoymiles MS-A2 / HiBattery** | MQTT mediante Home Assistant | [Hoymiles MQTT](hoymiles.md) |
| **Fronius GEN24 / BYD** | Modbus TCP y Solar API local | [Fronius GEN24 / BYD](fronius-gen24.md) |

![Selector de marca de batería](../../assets/screenshots/configuration/battery-brand-form.png){ width="650"  style="display: block; margin: 0 auto;"}

ESPHome es un método de conexión para una batería Marstek, no una marca
independiente. Selecciona **Marstek mediante LilyGo RS485 (ESPHome)** cuando la
batería se exponga a Home Assistant mediante un puente LilyGo.

## Número de baterías

Selecciona cuántas unidades tienes (1–10). La integración te pedirá configurar
cada unidad por separado, por lo que una instalación mixta puede combinar
marcas compatibles.

![Control de número de baterías](../../assets/screenshots/configuration/battery-slider.png){ width="650"  style="display: block; margin: 0 auto;"}

## Ajustes comunes por batería

Todas las baterías tienen nombre, límites de carga/descarga, límites de SOC,
histéresis de carga y umbral de backup offgrid. La página de conexión y algunos
campos específicos cambian según la marca; consulta las páginas anteriores para
esos detalles.

| Ajuste | Función |
|---|---|
| **Nombre** | Identifica la batería en Home Assistant y en el dashboard de Omnibattery. |
| **Potencia máx. de carga/descarga** | Limita la potencia que Omnibattery puede solicitar. Algunas marcas comunican estos límites automáticamente. |
| **SOC máximo** | Detiene la carga en el límite superior configurado. |
| **SOC mínimo** | Detiene la descarga en el límite inferior configurado. |
| **Histéresis de carga** | Evita ciclos rápidos después de alcanzar el SOC superior. El mínimo es 2 %. |
| **Umbral offgrid backup** | Excluye la batería del control PD cuando la carga offgrid indica un evento de backup activo. |
| **Capacidad nominal** | Se usa para calcular la energía almacenada y la eficiencia cuando la marca no ofrece un contador de capacidad. |

![Formulario de configuración de batería](../../assets/screenshots/configuration/battery-config-form.png){ width="650"  style="display: block; margin: 0 auto;"}

## SOC y límites de potencia en tiempo de ejecución

Los valores de SOC máximo/mínimo y potencia máxima de carga/descarga se pueden
ajustar en cualquier momento desde los sliders de la integración sin
reconfigurar. Los cambios se persisten y se restauran en cada reinicio de Home
Assistant.

El switch `Control Manual de Batería` también está disponible en tiempo de
ejecución. Entrega la batería al usuario después de verificar `0 W`, la
mantiene fuera del grupo automático y persiste esa propiedad entre reinicios.
Consulta la [guía multi-batería](../../features/multi-battery.es.md#control-manual-por-batería)
para conocer el comportamiento de la transición y el efecto sobre las demás
baterías.

![Sliders de SOC y potencia](../../assets/screenshots/configuration/battery-runtime-sliders.png){ width="650"  style="display: block; margin: 0 auto;"}

## Límites de potencia del sistema

Configura desde el dashboard de Omnibattery los límites combinados opcionales de
carga y descarga. El límite individual de cada batería se sigue aplicando y
establecer cualquiera de los límites del sistema en `0 W` lo desactiva.

![Límites de potencia del sistema](../../assets/screenshots/configuration/battery-system-power-limits-config.png){ width="650"  style="display: block; margin: 0 auto;"}

## Umbral offgrid backup

La entidad numérica **Umbral Offgrid Backup** está disponible en la tarjeta de
dispositivo de cada batería. Auméntala cuando el puerto offgrid tenga una carga
permanente, como un router, switch PoE o cámaras IP; de lo contrario esa carga
puede mantener la batería excluida del control PD.

| Escenario | Umbral recomendado |
|---|---|
| Sin cargas permanentes en offgrid | `0 W` |
| Cargas pequeñas (~20–40 W) | `50 W` (por defecto) |
| Cargas permanentes más pesadas (~80–120 W) | `150 W` |

Cuando **Función Backup** está activada y la carga offgrid medida supera el
umbral, la batería se gestiona de forma autónoma. Se aplica un período de
enfriamiento de cinco minutos después de que la carga baje del umbral.

## Configuración relacionada

- Las [franjas horarias](../time-slots.md) controlan cuándo pueden cargar o descargar las baterías.
- La [carga predictiva](../predictive-charging/index.md) programa la carga opcional desde la red.
- La [gestión multi-batería](../../features/multi-battery.md) explica cómo se reparte la potencia entre unidades.
