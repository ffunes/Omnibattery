# Blueprints

Los blueprints son automatizaciones opcionales de Home Assistant que complementan Omnibattery. No forman parte de la configuración de la integración ni cambian su código.

## Antes de empezar

- Importa un blueprint desde **Ajustes → Automatizaciones y escenas → Blueprints** mediante el enlace siguiente; después crea una automatización a partir de él.
- Para instalarlo manualmente, copia el archivo YAML en `/config/blueprints/automation/omnibattery/` y recarga los blueprints. Consulta [Instalación](../installation.md#blueprint-installation) para los pasos generales.

## Balanceo activo de una batería Marstek {#balanceo-activo-de-una-batería-marstek}

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/marstek_active_balance_blueprint.yaml)

- **Finalidad:** ejecuta un perfil de balanceo activo de celdas que carga y descarga una batería en pequeños pasos para reducir la diferencia entre sus celdas.
- **Requiere:** un `input_boolean` persistente por batería, usado como solicitud de ejecutar/cancelar; el blueprint detecta automáticamente el resto de entidades de telemetría y control en el dispositivo Omnibattery seleccionado.
- **Controla:** el interruptor por batería **Battery Manual Mode**, el modo forzado y las entidades de consigna solo del dispositivo seleccionado, mientras la ejecución esté activa.
- **Compatible con:** exactamente una batería Marstek por instancia de automatización. Solo usa entidades de Home Assistant; no accede directamente a Modbus.
- **No hace:** ejecutar más de una batería por automatización ni mantener el control al salir; cada salida intenta escribir 0 W en ambas direcciones, restaurar el máximo de SOC normal y liberar Battery Manual Mode.

Crea una automatización por batería. **Battery Manual Mode** es el límite de control: mientras una ejecución está activa, el controlador automático de Omnibattery y otras automatizaciones manuales no pueden escribir consignas que compitan en esa batería. Activa el `input_boolean` de solicitud para iniciar o reanudar tras un reinicio, y desactívalo para cancelar.

??? "Detalles avanzados"
    El blueprint valida la telemetría, las opciones de modo forzado, el orden de tensiones y los límites de los números antes de tomar el control, incluido el número `charging_cutoff_capacity` usado como límite máximo de SOC. Las sustituciones opcionales avanzadas de ID de entidad siguen disponibles para instalaciones donde se haya cambiado el nombre de una entidad. Las opciones de `force_mode` se llaman **None**, **Charge** y **Discharge**; las entidades ESPHome antiguas en minúsculas siguen siendo compatibles durante la migración.

    Sus valores predeterminados son 3,49 V → 3,60 V, 95 W de carga superior, 200 W de descarga, 60 s de reposo, un objetivo de 30 mV y un suelo de reintento adaptativo de 3,40 V. Si el BMS rechaza la carga antes de 3,60 V pero todavía dentro de la ventana superior, el blueprint toma la misma medición estabilizada de 60 segundos antes de continuar con la descarga adaptativa; los rechazos por debajo de esa ventana no se añaden al historial formal.

    Si falla alguna confirmación de seguridad, el `input_boolean` de solicitud se deja deliberadamente activado para que se pueda inspeccionar la batería antes de que otra automatización pueda controlarla.

    Su línea base de notificación procede del valor persistente `Cell Delta` de la integración, que representa la última lectura formal al 100%/OCV en lugar de la telemetría instantánea de celdas. Tras cada medición estabilizada de 60 segundos, el blueprint lanza el evento público `omnibattery_balance_measurement_ready` con el dispositivo seleccionado y un ID de medición. Omnibattery resuelve el dispositivo, lee las tensiones de celda de su propio coordinador y registra el resultado en el historial existente de `Cell Delta` con `source: blueprint`. El evento es de solo lectura y no concede a la integración el control de la batería.

## Informador de estado central mediante webhook

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/central_status_webhook_reporter_blueprint.yaml)

- **Finalidad:** informa sensores seleccionados de Omnibattery y Home Assistant desde cada instalación a un punto final HTTP central, para un único panel que cubra varias casas o baterías.
- **Requiere:** un `rest_command` definido una vez en `configuration.yaml` (abajo) y su URL guardada en `secrets.yaml`.
- **Controla:** nada en Omnibattery; solo lee estados de sensores y los envía.
- **Compatible con:** cualquier instalación; eliges qué sensores informar (por ejemplo, SOC, potencia de batería, potencia de red y estado de integración).
- **No hace:** crear por sí mismo la orden HTTP saliente; Home Assistant debe definirla porque un blueprint no puede hacerlo.

Elige un ID de sitio único, los sensores que informar y un intervalo de notificación. También se envía un informe cuando se inicia Home Assistant. Cada informe contiene el ID de sitio, marca de tiempo y el estado, nombre, unidad y clase de dispositivo de cada entidad seleccionada.

```yaml
rest_command:
  omnibattery_status_report:
    url: !secret omnibattery_status_webhook_url
    method: POST
    content_type: application/json
    payload: "{{ report }}"
```

Reinicia Home Assistant después de añadir el `rest_command` y conserva el servicio de orden REST predeterminado en el blueprint salvo que hayas elegido otro nombre. Usa HTTPS y trata la URL del punto final como un secreto.

## Objetivo de red distinto para carga y descarga

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/different_grid_target_blueprint.yaml)

- **Finalidad:** establece **PD Target Grid Power** según la dirección activa de la batería, para evitar oscilaciones alrededor de un objetivo de red cero o aplicar un sesgo deliberado de importación/exportación.
- **Requiere:** los sensores de potencia de carga y descarga del sistema, además del número **PD Target Grid Power**.
- **Controla:** solo el número **PD Target Grid Power**.
- **Compatible con:** cualquier instalación que use control de seguimiento de red proporcional–derivativo (PD).
- **No hace:** cambiar el objetivo en reposo; es opcional y, si no se establece, el objetivo existente no cambia mientras el sistema está en reposo.

De forma predeterminada establece `-50 W` mientras carga (una pequeña exportación a red) y `+50 W` mientras descarga (una pequeña importación de red). El umbral de potencia activa ignora el ruido cerca de cero. La automatización se ejecuta cuando la potencia cruza el umbral y cuando se inicia Home Assistant.

## Sincronización del límite de peak shaving

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/peak_shaving_limit_sync_blueprint.yaml)

- **Finalidad:** sincroniza **Capacity Protection Limit** de Omnibattery con un sensor de pico mensual, para tarifas o configuraciones de gestión de demanda donde el pico permitido sigue un valor mensual medido.
- **Requiere:** el sensor de pico mensual y el número **Capacity Protection Limit**.
- **Controla:** solo el número **Capacity Protection Limit**.
- **Compatible con:** un sensor de pico mensual que informa en `kW` o `W`; el blueprint convierte automáticamente `kW` a vatios.
- **No hace:** escribir el número cuando su valor ya coincide con el pico medido.

Comprueba los cambios y también cada 15 segundos.

## Recarga de peak shaving hasta SOC

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/peak_shaving_recharge_blueprint.yaml)

- **Finalidad:** repone opcionalmente la batería desde la red mientras está activa la **Capacity Protection** (peak shaving), para que un SOC bajo no te deje sin protección frente al siguiente pico.
- **Requiere:** los sensores de SOC del sistema y estado de integración, además del número **PD Target Grid Power**.
- **Controla:** solo el número **PD Target Grid Power**, moviéndolo a un valor de importación positivo para cargar cuando el SOC del sistema cae por debajo del suelo configurado.
- **Compatible con:** cualquier instalación que use peak shaving (protección de capacidad) y control de seguimiento de red PD.
- **No hace:** sobrescribir un cambio manual posterior ni el objetivo de otra automatización; solo restaura el objetivo en reposo cuando el objetivo de recarga que estableció sigue aplicado.

Configura el suelo de SOC, un objetivo de recuperación de SOC más alto, la potencia de carga y el objetivo en reposo. Restaura el objetivo en reposo cuando el SOC alcanza el objetivo de recuperación o termina la protección de capacidad.

## Reenviar notificaciones persistentes a Telegram

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/persistent_notification_to_telegram_blueprint.yaml)

- **Finalidad:** reenvía notificaciones persistentes de Home Assistant nuevas o actualizadas a una entidad de notificación de Telegram seleccionada.
- **Requiere:** una entidad de notificación `telegram_bot` configurada.
- **Controla:** nada en Omnibattery; solo lee notificaciones y envía mensajes de Telegram.
- **Compatible con:** cualquier notificación persistente, no solo las de Omnibattery; un filtro opcional por prefijo de ID lo delimita. El valor predeterminado `marstek_venus_` conserva la compatibilidad con las notificaciones creadas por la integración anterior; borra el filtro para reenviar todas las notificaciones persistentes o sustitúyelo por otro prefijo.
- **No hace:** reenviar notificaciones existentes cuando se reinicia Home Assistant.

Envía el título, ID y mensaje de la notificación escapando HTML de forma segura.

## Descartar notificaciones de carga predictiva

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/dismiss_predictive_charging_notifications_blueprint.yaml)

- **Finalidad:** descarta automáticamente notificaciones persistentes sobre carga predictiva desde red, incluidas evaluaciones, inicios de franjas de precio y reevaluaciones nocturnas.
- **Requiere:** ninguna entidad ni helper adicional.
- **Controla:** nada en Omnibattery; solo descarta notificaciones persistentes coincidentes.
- **Compatible con:** cualquier instalación que use carga predictiva.
- **No hace:** tocar alarmas de batería, mensajes de equilibrio de celdas o notificaciones de modo manual; solo se descartan notificaciones de carga predictiva.

La notificación puede ser visible brevemente antes de que Home Assistant ejecute la automatización. Desactiva la automatización en cualquier momento para volver a recibir notificaciones de carga predictiva.

## Reserva de descarga según previsión solar

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/solar_forecast_reserve_discharge_blueprint.yaml)

- **Finalidad:** mantiene una reserva nocturna de SOC bloqueando la descarga por debajo de ella, salvo que la previsión solar restante sea suficiente para recargar desde el SOC mínimo configurado hasta la reserva durante una ventana diurna especificada.
- **Requiere:** los interruptores Allow Discharge que se controlarán, los sensores de SOC y energía total del sistema, un sensor de previsión solar *restante* en kWh y los números de SOC mínimo de las baterías controladas.
- **Controla:** solo los interruptores **Allow Discharge** seleccionados.
- **Compatible con:** cualquier instalación con un sensor de previsión de producción solar restante en kWh.
- **No hace:** escribir registros Modbus ni forzar modos de batería; solo conmuta Allow Discharge.

Configura la reserva, la histéresis de liberación, el margen de previsión y la ventana diurna.

??? "Detalles avanzados"
    El cálculo incluye una hipótesis fija de eficiencia de carga del 78% y el margen de seguridad.
