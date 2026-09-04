# Huawei SUN2000 + LUNA2000

Omnibattery se conecta a una Huawei LUNA2000 a través de su inversor híbrido
SUN2000 mediante Modbus TCP. La telemetría siempre se lee directamente por
Modbus. Las órdenes de potencia pueden usar los servicios de la integración
**Huawei Solar** (opción predeterminada) o escrituras Modbus directas opcionales.

!!! warning "Hardware probado"
    La compatibilidad se ha validado en una única instalación trifásica europea
    con un SUN2000-8K-MAP0, un módulo de potencia LUNA2000-10KW-C1 y dos packs
    LUNA2000-7-E1. Otros modelos SUN2000, configuraciones de hardware y versiones
    de firmware aún no se han probado.

## Elegir el método de control

Los inversores Huawei solo aceptan una conexión Modbus simultánea. Elige el
esquema de conexión antes de añadir la batería:

| Método de control | Requisitos | Endpoint Modbus en Omnibattery |
|---|---|---|
| **Servicios de Huawei Solar** (predeterminado) | Instala y configura la [integración Huawei Solar](https://github.com/wlcrs/huawei_solar), y comprueba que su dispositivo de batería LUNA2000 aparece en Home Assistant. | Un [proxy Modbus](https://github.com/Akulatraxas/ha-modbusproxy) compartido por Huawei Solar y Omnibattery. |
| **Escrituras Modbus directas** | Huawei Solar no es necesaria. Esta vía solo se ha validado en una instalación, por lo que conviene empezar con la opción predeterminada cuando sea posible. | El propio inversor si Omnibattery es el único cliente Modbus; en caso contrario, un proxy Modbus compartido. |

!!! danger "No abras dos conexiones directas"
    No conectes Huawei Solar, Omnibattery, evcc u otro cliente directamente al
    inversor al mismo tiempo. Coloca un proxy Modbus delante y dirige todos los
    clientes al proxy.

Para la vía predeterminada, configura primero Huawei Solar y verifica que su
dispositivo de batería esté disponible. Configura el inversor como dispositivo
de destino del proxy y usa la dirección y el puerto del proxy en ambas
integraciones. Omnibattery no necesita autenticación Modbus.

## Añadir la batería

En el asistente de configuración de Omnibattery, selecciona **Huawei SUN2000 +
LUNA2000** y completa el formulario de conexión.

| Campo | Descripción | Por defecto |
|---|---|---|
| **Nombre** | Nombre usado para el dispositivo de batería | `Huawei LUNA2000 1` |
| **Dirección IP** | Dirección del proxy Modbus, o del inversor cuando Omnibattery sea su único cliente | — |
| **Puerto Modbus** | Puerto TCP expuesto por el proxy o el inversor | `502` |
| **ID de esclavo Modbus** | ID de unidad del inversor SUN2000, no el del gestor de energía EMMA ni el de un cargador. Déjalo vacío para buscarlo automáticamente. | Búsqueda automática |
| **Escrituras Modbus directas** | Envía las órdenes de potencia directamente en lugar de usar los servicios de Huawei Solar | Desactivadas |
| **Dispositivo de batería Huawei Solar** | Dispositivo LUNA2000 creado por Huawei Solar. Es obligatorio si las escrituras directas están desactivadas; déjalo vacío si están activadas. | — |

La búsqueda automática del ID de esclavo tarda unos 15 segundos. Si encuentra
un único inversor con batería, Omnibattery lo selecciona. Si encuentra varios
en una instalación en cascada, elige el inversor al que pertenece esta
LUNA2000. Añade otra batería con el otro ID de esclavo por cada sistema de
almacenamiento adicional.

El asistente comprueba que el dispositivo de batería Huawei Solar seleccionado
pertenezca al mismo inversor que responde en el ID de esclavo Modbus. Así evita
leer la telemetría de un inversor y enviar las órdenes a otro en una cascada.

## Límites de potencia y SOC

Durante la prueba de conexión, Omnibattery lee los límites actuales de carga y
descarga de la batería. Esos valores sirven como punto de partida del siguiente
formulario. Puedes reducirlos para tu instalación; el rango superior está
limitado por la potencia activa máxima del inversor porque el límite que
comunica la batería puede cambiar al añadir packs. Cada orden se sigue
restringiendo al límite de hardware disponible en ese momento. La capacidad
nominal se lee automáticamente de la batería y no requiere ningún dato manual.

La página de límites comunes también incluye:

- SOC máximo: 80–100 % (por defecto `100 %`);
- SOC mínimo: 0–30 % (por defecto `10 %`);
- histéresis de carga obligatoria (mínimo 2 %);
- umbral de backup offgrid.

Huawei solo admite un rango más estrecho en sus registros persistentes de corte
por SOC. Por ello, Omnibattery aplica por software todo el rango configurado y
solo usa los cortes de hardware cuando el valor se puede representar.

LUNA2000 ofrece datos por pack, pero no las tensiones de cada celda. Por tanto,
el monitor de equilibrio de celdas y la reducción de carga al 100 % basada en
tensión no están disponibles. Para los controles comunes en tiempo de ejecución
y los límites del sistema, consulta la [configuración de baterías](index.md).

## Comportamiento específico de Huawei

Como la batería y los strings fotovoltaicos comparten el inversor SUN2000, la
potencia de descarga disponible disminuye cuando la producción solar se acerca
al límite del inversor. Omnibattery lo tiene en cuenta automáticamente.

Una orden de `0 W` devuelve el control de la batería al modo de trabajo propio
del inversor; no mantiene la LUNA2000 en reposo. Por tanto, el inversor puede
reanudar su estrategia de autoconsumo. Al activar **Control Manual de Batería**,
la batería también queda en manos del inversor después de detenerse el
controlador automático.

## Solución de problemas

| Problema | Comprobación |
|---|---|
| **No se puede conectar** | Verifica la dirección y el puerto, comprueba que el proxy puede alcanzar el inversor y asegúrate de que ningún cliente evita el proxy. |
| **Se alcanza el inversor, pero no se encuentra la batería** | Deja vacío el ID de esclavo para repetir la búsqueda o comprueba que el ID elegido pertenezca al inversor que tiene conectada la LUNA2000. |
| **No aparece el dispositivo de batería Huawei Solar** | Instala y configura Huawei Solar, o activa las escrituras Modbus directas y deja vacío el campo del dispositivo. |
| **El dispositivo de batería no coincide con el inversor** | En una cascada, selecciona el dispositivo Huawei Solar y el ID de esclavo que pertenezcan al mismo inversor. |
| **El formulario parece detenerse tras enviarlo** | Una búsqueda automática del ID de esclavo suele tardar unos 15 segundos. |

Para consultar el firmware verificado, el mapa de registros y las limitaciones
de la implementación, consulta la [evaluación técnica del driver de
Huawei](../../reference/driver-assessment-huawei.md).
