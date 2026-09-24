# Configurar una carga grande o un cargador para vehículos eléctricos

Agregue un dispositivo aquí cuando su demanda necesite un tratamiento de batería diferente al del resto de la casa. Esta página cubre los sensores y los campos de configuración; [Cargar exclusión](../features/load-exclusion.md) explica qué comportamiento elegir y qué hace.

## ¿Lo necesito?

**Úselo si** desea agregar una carga grande, como un cargador de vehículo eléctrico (EV), una caja de pared, una bomba de calor o un calentador de inmersión, que la batería debe ignorar total o parcialmente.

**No lo necesita si** la batería debe tratar el dispositivo exactamente como la demanda doméstica normal.

## Antes de empezar

- Para un dispositivo con medidor, prepare un **sensor de potencia del dispositivo** numérico en vatios.
- Para el control dinámico de energía (DPC), también prepare un **Sensor de carga EV/activo del dispositivo** que informa cuando el dispositivo solicita energía.
- Para un cargador de vehículos eléctricos sin telemetría de potencia, prepare ese sensor de actividad en lugar de un sensor de vatios.
- Decide si tu sensor principal de casa/red ya incluye el consumo de este dispositivo.
- Opcional: preparar un sensor de energía para la demanda que aún se espera hoy y una entidad de presencia cuando la carga predictiva deba reservar energía solar para el dispositivo.

## Cómo activarlo

1. Abra **Configuración → Dispositivos y servicios → Omnibattery → Configurar → Dispositivos excluidos**, habilite **Configurar dispositivos especiales** y agregue un dispositivo.
2. Seleccione **Sensor de energía del dispositivo**. Para un cargador de solo estado, déjelo vacío, seleccione **Dispositivo activo/sensor de carga EV** y habilite **Cargador EV sin telemetría de energía**.
3. Configure **El consumo está incluido en el sensor de consumo del hogar** mediante la prueba siguiente.
4. Seleccione el comportamiento que necesita: **Permitir usar excedente solar (no cargar la batería)**, **El dispositivo tiene control dinámico de energía** o **Cubrir la casa mientras el dispositivo está activo**. Utilice la [guía de comportamiento](../features/load-exclusion.md) antes de combinarlos.
5. Opcional: agregue **Demanda restante esperada (kWh)** y su entidad de presencia, luego guarde el dispositivo.

```text
Main sensor measures the whole home, including this device
→ Enable "Consumption is included in home consumption sensor"

Main sensor measures only the domestic circuit and cannot see this device
→ Leave it disabled
```

![Configurar un dispositivo excluido](../assets/screenshots/configuration/excluded-device-form.png){ width="650" style="display: block; margin: 0 auto;"}

!!! warning "La captura de pantalla necesita actualizarse"
El formulario actual también incluye la demanda restante esperada y su entidad de presencia, que no son visibles en esta captura de pantalla.

## lo que veras

Cada dispositivo guardado expone controles en vivo en el dispositivo del sistema Omnibattery:

| Controlar | Disponibilidad |
|---|---|
| **Dispositivo – Habilitado** | Cada dispositivo configurado |
| **Dispositivo – Excedente solar** | Cada dispositivo configurado; setup determina su estado inicial |
| **Dispositivo – Control dinámico de potencia** | Dispositivos medidos; setup determina su estado inicial |
| **Dispositivo – Portada de portada** | Cada dispositivo configurado; setup determina su estado inicial |
| **Dispositivo – % de exclusión** | Dispositivos medidos |

Estas entidades le permiten pausar o cambiar el comportamiento guardado sin volver a abrir la configuración. Consulte [Cargar exclusión](../features/load-exclusion.md#what-you-will-see) para conocer el efecto de cada control.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El formulario requiere un sensor de potencia | El dispositivo está configurado como carga medida | Seleccione un sensor de vatios numérico o habilite **Cargador EV sin telemetría de energía** y proporcione un sensor de actividad |
| El control dinámico de potencia no se puede guardar | Falta el sensor de actividad requerido | Seleccione **Dispositivo activo/sensor de carga EV** |
| La batería compensa la cantidad incorrecta | La opción incluida en el consumo no coincide con el contador principal | Compruebe si cambiar el dispositivo cambia la lectura del sensor principal |
| **Cover Home** no tiene ningún efecto útil | Faltan datos de Solar Surplus o producción solar externa | Habilite Solar Surplus y configure el sensor de producción solar en **Sensores** |
| La carga predictiva reserva energía para un vehículo eléctrico ausente | El sensor de demanda permanece por encima de cero tras la desconexión | Configure **Entidad de presencia para la demanda restante (opcional)** usando una entidad conectada/presente confiable |
| Falta un control de tiempo de ejecución | La definición del dispositivo no habilita ese comportamiento o su entidad está deshabilitada | Vuelva a abrir la configuración del dispositivo y verifique las entidades deshabilitadas del dispositivo Omnibattery |

??? "Detalles avanzados"
**Requisitos de campo**

Omnibattery admite hasta 4 dispositivos especiales configurados. Un dispositivo excluido normal requiere un sensor de potencia numérico. Una nueva configuración de EV de solo estado requiere un sensor de actividad. El control dinámico de energía también requiere un sensor de actividad y es significativo solo con Solar Surplus habilitado. Cover Home requiere Solar Surplus y un sensor externo de producción solar.

Las entradas EV existentes de solo estado que almacenaron su entidad de estado en **Sensor de potencia del dispositivo** siguen siendo compatibles. La detección de actividad acepta `on` binario y palabras de carga sin distinguir entre mayúsculas y minúsculas.

**Demanda restante esperada**

El sensor opcional debe informar energía convertible como `Wh`, `kWh` o `MJ`. Omnibattery lo utiliza sólo cuando **El consumo está incluido en el sensor de consumo del hogar** está habilitado. Se omiten los dispositivos eléctricos estatales porque su demanda ya está representada por la previsión de consumo. El porcentaje de exclusión del tiempo de ejecución escala la cantidad reservada así como la corrección de carga.

La reserva no puede exceder el resto solar después del margen de seguridad predictivo:

    ```text
    claim = min(expected remaining demand, remaining solar after safety margin)
    solar available to the battery = remaining solar after safety margin - claim
    ```

Si el valor de la demanda no está disponible, es desconocido, no es numérico o no es una unidad de energía, no se realiza ningún reclamo. La entidad de presencia opcional evita reservar energía solar cuando una integración upstream sigue informando la demanda de un dispositivo desconectado. Valores de estado completo `on`, `true`, `home`, `present`, `connected`/`plugged`/`plugged in` (EN), `verbunden` (DE), `aangesloten`/`aanwezig` (NL), `connesso` (IT), `connecté`/`branché` (FR), `conectado` (ES/PT), `connectat` (CA) y `charging`/`cargando`/`laden` cuentan como presentes. La coincidencia es completa, nunca un fragmento, porque una frase negativa contiene su propia frase positiva: `disconnected` contiene `connected`. Los estados compuestos desconocidos, no disponibles, faltantes y no coincidentes (`Connected, not charging`) cuentan como ausentes; utilice un sensor binario si el estado de un texto es ambiguo. Dejar el campo vacío siempre cuenta como un sensor de demanda válido.

Los usuarios de evcc pueden seleccionar `sensor.evcc_<loadpoint>_charge_remaining_energy` y vincularlo con `binary_sensor.evcc_<loadpoint>_connected`.

La reserva se distribuye en proporción a la energía en los intervalos solares restantes de hoy; no predice cuándo consumirá el dispositivo. El pronóstico de mañana no se reduce en una proyección cruzada a medianoche. La carga predictiva puede volver a planificarse cuando el reclamo cambia en al menos 2 kWh, con al menos 15 minutos entre esas evaluaciones y no más de 4 evaluaciones basadas en reclamo por día. Los diagnósticos publican el valor actual como `excluded_demand_claim_kwh` junto con `solar_surplus_kwh` y `solar_available_to_battery_kwh` en **Carga predictiva activa**.
