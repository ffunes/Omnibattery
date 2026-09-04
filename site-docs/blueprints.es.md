# Blueprints

Los blueprints son automatizaciones opcionales de Home Assistant que complementan Omnibattery. No forman parte de la configuración de la integración ni modifican su código. Importa cualquiera desde **Ajustes → Automatizaciones y escenas → Blueprints** usando el enlace correspondiente y crea después una automatización basada en él.

Para la instalación manual, copia el archivo YAML en `/config/blueprints/automation/omnibattery/` y recarga los blueprints. Consulta los pasos generales en [Instalación](installation.es.md#instalación-de-blueprints).

## Balanceo activo de una batería Marstek

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/marstek_active_balance_blueprint.yaml)

Ejecuta el perfil de balanceo activo para exactamente una batería Marstek. Crea un `input_boolean` persistente y una automatización por batería; después selecciona el dispositivo de esa batería creado por Omnibattery. El blueprint descubre automáticamente en ese dispositivo la telemetría y los controles estándar, incluido el número `charging_cutoff_capacity` que representa el límite máximo de SOC. Hay sobrescrituras avanzadas opcionales por ID de entidad para instalaciones que hayan renombrado alguna entidad. La automatización usa el interruptor por batería **Battery Manual Mode** como límite de propiedad: mientras dure el ciclo, el controlador automático de Omnibattery y otras automatizaciones manuales no pueden escribir setpoints en competencia.

El blueprint solo utiliza entidades de Home Assistant; no accede directamente a Modbus. Valida la telemetría, las opciones del modo forzado, el orden de tensiones y los límites de los números antes de tomar el control. Sus valores predeterminados son 3,49 V → 3,60 V, carga superior a 95 W, descarga a 200 W, reposo de 60 s, objetivo de 30 mV y suelo de reintento adaptativo de 3,40 V. Si el BMS rechaza la carga antes de 3,60 V pero todavía dentro de la ventana superior, el blueprint hace el mismo reposo de 60 s y publica la medición antes de continuar con la descarga adaptativa; los rechazos por debajo de esa ventana no se incorporan al histórico formal. Las opciones de `force_mode` se llaman **None**, **Charge** y **Discharge**; las entidades ESPHome antiguas en minúsculas siguen siendo compatibles durante la migración.

Activa el helper de solicitud para iniciar o reanudar tras un reinicio y desactívalo para cancelar. El único helper que debes crear es este `input_boolean`; el blueprint no necesita crear otro switch ni sensor. La línea base de la notificación procede del valor persistente `Cell Delta` de la integración, que representa la última lectura formal al 100%/OCV y no la telemetría instantánea de las celdas. En cada salida intenta escribir 0 W en ambas direcciones, restaurar el SOC máximo normal y liberar Battery Manual Mode. Si falla alguna confirmación de seguridad, el interruptor se deja deliberadamente activado para inspeccionar la batería antes de permitir otro control automático.

Después de cada medición estable tras 60 segundos de reposo, el blueprint emite el evento público `omnibattery_balance_measurement_ready` con el dispositivo seleccionado y un identificador de medida. Omnibattery resuelve el dispositivo, lee las tensiones desde su propio coordinador y guarda el resultado en el histórico existente de `Cell Delta` con `source: blueprint`. El evento es de solo lectura y no devuelve a la integración la propiedad de la batería.

## Reporte de estado a webhook central

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/central_status_webhook_reporter_blueprint.yaml)

Envía entidades seleccionadas de Omnibattery y Home Assistant desde cada instalación a un endpoint HTTP central. Es útil para tener un único panel con varias viviendas o baterías.

Elige un identificador único de instalación, los sensores que se reportarán —por ejemplo SOC, potencia de batería, potencia de red y estado de la integración— y el intervalo. También se envía un reporte al iniciar Home Assistant. Cada reporte contiene el identificador, hora de envío y el estado, nombre, unidad y clase de dispositivo de cada entidad seleccionada.

Home Assistant exige definir una vez el comando HTTP saliente en `configuration.yaml`; un blueprint no puede crearlo por sí mismo:

```yaml
rest_command:
  omnibattery_status_report:
    url: !secret omnibattery_status_webhook_url
    method: POST
    content_type: application/json
    payload: "{{ report }}"
```

Guarda la URL en `secrets.yaml`, reinicia Home Assistant y conserva en el blueprint el servicio REST predeterminado, salvo que hayas elegido otro nombre. Usa HTTPS y trata la URL del endpoint como un secreto.

## Objetivo de red distinto al cargar y descargar

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/different_grid_target_blueprint.yaml)

Establece el **objetivo de potencia de red del PD** según la dirección activa de la batería. Por defecto fija `-50 W` mientras carga (una pequeña exportación a red) y `+50 W` mientras descarga (una pequeña importación de red). Puede servir para evitar oscilaciones alrededor de un objetivo de red cero o para aplicar un sesgo deliberado de importación/exportación.

Selecciona los sensores de potencia de carga y descarga del sistema y el número **PD Target Grid Power**. El umbral de potencia activa ignora el ruido cercano a cero. Opcionalmente, puedes fijar un objetivo en reposo; de lo contrario, cuando el sistema está inactivo se mantiene el objetivo actual. La automatización actúa cuando la potencia cruza el umbral y al iniciar Home Assistant.

## Sincronización del límite de peak shaving

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/peak_shaving_limit_sync_blueprint.yaml)

Sincroniza el **límite de protección de capacidad** de Omnibattery con un sensor de pico mensual. Está pensado para tarifas o configuraciones de gestión de demanda en las que el máximo permitido sigue a una medición mensual.

Selecciona el sensor de pico mensual y el número de límite de protección de capacidad. El origen puede usar `kW` o `W`; el blueprint convierte automáticamente de `kW` a vatios. Comprueba cambios y también cada 15 segundos, y solo escribe el número si su valor difiere del pico medido.

## Recarga por peak shaving hasta SOC

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/peak_shaving_recharge_blueprint.yaml)

Opcionalmente repone la batería desde la red mientras está activa la **protección de capacidad** (peak shaving). Cuando el SOC del sistema baja del umbral configurado, mueve el objetivo de potencia de red del PD a un valor positivo de importación para que la batería cargue. Restaura el objetivo en reposo cuando el SOC alcanza el objetivo de recuperación o termina la protección de capacidad.

Selecciona los sensores de SOC del sistema y estado de integración, además del número PD Target Grid Power. Configura el umbral de SOC, un objetivo de recuperación superior, la potencia de carga y el objetivo en reposo. La automatización solo restaura el objetivo en reposo si todavía está aplicado el objetivo de recarga, así que no sobrescribe un cambio manual posterior ni el de otra automatización.

## Reenvío de notificaciones persistentes a Telegram

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/persistent_notification_to_telegram_blueprint.yaml)

Reenvía notificaciones persistentes nuevas o actualizadas de Home Assistant a una entidad de notificación de Telegram. Envía el título, el ID y el mensaje, escapando correctamente HTML.

Elige una entidad `notify` de `telegram_bot` y, opcionalmente, un filtro por prefijo de ID. El prefijo predeterminado `marstek_venus_` mantiene la compatibilidad con las notificaciones de la integración anterior; déjalo vacío para reenviar todas las notificaciones persistentes o sustitúyelo por otro prefijo. Las notificaciones ya existentes no se reenvían al reiniciar Home Assistant.

## Descartar notificaciones de carga predictiva

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/dismiss_predictive_charging_notifications_blueprint.yaml)

Descarta automáticamente las notificaciones persistentes sobre carga de red predictiva, incluidas las evaluaciones, los inicios de slots de precio y las reevaluaciones nocturnas. No afecta a las alarmas de batería, los mensajes de equilibrio de celdas ni las notificaciones de modo manual.

La notificación puede verse brevemente antes de que Home Assistant ejecute la automatización. Desactiva la automatización en cualquier momento para volver a recibir las notificaciones de carga predictiva.

## Reserva de descarga según previsión solar

[Importar blueprint](https://raw.githubusercontent.com/ffunes/Omnibattery/main/blueprints/solar_forecast_reserve_discharge_blueprint.yaml)

Mantiene una reserva nocturna de SOC controlando los interruptores **Allow Discharge** de Omnibattery. Bloquea la descarga en la reserva salvo que la previsión solar restante permita recargar desde el SOC mínimo configurado hasta esa reserva durante una ventana diurna indicada.

Selecciona todos los interruptores Allow Discharge que se controlarán, los sensores de SOC y energía total del sistema, un sensor de previsión solar *restante* en kWh y los números de SOC mínimo de las baterías controladas. Configura la reserva, la histéresis de liberación, el margen de previsión y la ventana horaria. El cálculo incorpora una eficiencia fija de carga del 78 % y el margen de seguridad. El blueprint solo conmuta Allow Discharge; nunca escribe registros Modbus ni fuerza modos de batería.
