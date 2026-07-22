# Dispositivos excluidos

Permite "enmascarar" cargas pesadas para que la batería no intente cubrirlas.

## Caso de uso típico

Si tienes un cargador de vehículo eléctrico de 7 kW y una batería de 2,5 kW, sin exclusión la batería intentará compensar todo el consumo del cargador y se agotará rápidamente. Con la exclusión activa, el controlador ignora esa potencia y la batería solo gestiona el resto del hogar.

---

## Configuración de un dispositivo excluido

| Campo | Descripción |
|---|---|
| **Sensor de potencia del dispositivo** | Entidad HA que mide la potencia numérica del dispositivo (p. ej. `sensor.wallbox_power`). Es opcional para un cargador VE sin telemetría. |
| **Sensor de dispositivo activo / carga del VE** | Sensor de estado o binario que indica `on`, `Charging`, `Cargando` u otro estado reconocido de carga. Es obligatorio para Control Dinámico de Potencia y para configuraciones nuevas sin telemetría; en los demás casos es opcional. |
| **Incluido en el consumo** | Marca si tu sensor principal **ya** incluye esta carga |
| **Permitir excedente solar** | Si está activo, la batería no cargará para compensar este dispositivo cuando hay excedente solar. También puede activarse en tiempo real desde una entidad switch (ver más abajo). |
| **El dispositivo tiene control dinámico de potencia** | Actívalo para una carga, como una wallbox por excedente, que ajuste su propia demanda mediante un contador de red. Requiere **Permitir excedente solar**. |
| **Cubrir el hogar mientras el dispositivo está activo** | Permite que la batería cubra el consumo real del hogar mientras solo permanece excluida la parte de red del dispositivo. Requiere **Permitir excedente solar** y un sensor de producción solar. |
| **Cargador VE sin telemetría de potencia** | Marca si el sensor es un sensor de estado que indica `Charging`/`Cargando` en lugar de un valor en vatios. Ver [Cargador VE sin telemetría](#cargador-ve-sin-telemetría-de-potencia) más abajo. |

### ¿Incluido en el consumo?

```
Sensor principal lee: toda la casa
Cargador VE forma parte de "toda la casa" → ✅ Incluido en el consumo

Sensor principal lee: solo circuito doméstico
Cargador VE está en circuito separado → ❌ No incluido en el consumo
```

La integración usa esta configuración para calcular correctamente el consumo neto sin el dispositivo excluido.

![Formulario de dispositivo excluido](../assets/screenshots/configuration/excluded-device-form.png){ width="650"  style="display: block; margin: 0 auto;"}

---

## Switch de excedente solar

Por cada dispositivo excluido se crea automáticamente una entidad switch **Solar Surplus – \<nombre del dispositivo\>** que refleja el ajuste *Permitir excedente solar* y puede activarse en cualquier momento sin entrar en el flujo de opciones.

Esto permite cambiar la prioridad de carga desde automatizaciones — por ejemplo:

- Activar cuando el VE está conectado, para que el solar cargue primero el coche.
- Desactivar a una hora programada para que la batería capture el excedente de la mañana.
- Reaccionar al SOC de la batería: activar por encima del 80 %, desactivar por debajo del 50 %.

El estado del switch se persiste en la entrada de configuración y sobrevive reinicios.

---

## Control dinámico de potencia

Los dispositivos con telemetría también disponen de un switch **Control Dinámico
de Potencia**. Está pensado para cargas flexibles, como wallboxes, que se regulan
mediante el mismo contador de red que Omnibattery. Debe activarse junto con
**Excedente Solar**.

El **Sensor de dispositivo activo / carga del VE** permite que Omnibattery ceda
mientras la wallbox solicita potencia pero todavía marca 0 W, evitando el bloqueo
de arranque en el que la batería absorbe toda la exportación. Automáticamente:

- bloquea la carga de batería mientras el sensor de actividad solicita potencia
  y la wallbox todavía marca 0 W;
- cede la carga de batería durante 30 segundos cuando el dispositivo supera 100 W;
- deja que el regulador externo aumente potencia antes de usar el excedente restante;
- vuelve a ceder durante 20 segundos si la producción solar aumenta al menos 200 W;
- bloquea la carga durante 5 minutos cuando desaparece el consumo, para permitir
  que una wallbox reinicie tras una nube o un cambio de fase;
- realiza una comprobación cada 5 minutos si no hay sensor de producción solar.

Las entradas antiguas de Control Dinámico de Potencia sin sensor de actividad
siguen usando como fallback la primera lectura superior a 100 W. Este control no
está disponible para el modo **Cargador VE sin telemetría de potencia**, porque
ese modo ya gestiona la batería directamente con el mismo sensor de actividad.

---

## Slider de % de exclusión

La exclusión no es todo o nada. Cada dispositivo excluido tiene además un slider de **% de exclusión** (`<dispositivo> – Exclusion %`, `number.*_exclusion_pct`, 0–100 %, por defecto `100`) que controla **cuánta** de su demanda se mantiene fuera de la batería:

- `100 %` (por defecto) — el dispositivo se enmascara por completo, igual que antes. La batería no cubre nada de su carga.
- `0 %` — el dispositivo se trata como carga doméstica normal; la batería lo cubre como cualquier otra cosa.
- p. ej. `60 %` — el 60 % de la potencia del dispositivo se mantiene fuera de la batería; la batería puede cubrir el 40 % restante.

Esto permite que la batería cubra *parte* de una carga grande en vez de todo o nada — por ejemplo dejar que una batería de 2,5 kW ayude con un cargador VE de 7 kW hasta su parte, en lugar de ignorar el cargador por completo. El slider es por dispositivo y ajustable en tiempo de ejecución.

---

## Cargador VE sin telemetría de potencia

Algunas integraciones de cargadores de vehículo eléctrico no exponen un sensor de potencia en tiempo real — solo informan del **estado de carga** (p. ej. `Charging`, `Idle`, `Disconnected`). Esta opción está diseñada para esos cargadores.

En configuraciones nuevas, selecciona la entidad de estado en **Sensor de
dispositivo activo / carga del VE**; el sensor numérico de potencia puede quedar
vacío. Las configuraciones existentes que guardaron la entidad de estado en
**Sensor de potencia del dispositivo** siguen siendo totalmente compatibles y
se precargan automáticamente al editarlas. Se reconoce el estado binario `on` y
palabras de carga sin distinguir mayúsculas, lo que cubre:

- `Charging` (la mayoría de integraciones en inglés)
- `Cargando`, `Cargando VE`, `Cargando Vehículo` (español)

### Comportamiento cuando el VE empieza a cargar

```
t = 0  Estado VE → "Charging" detectado
       Batería forzada a 0 W (carga Y descarga bloqueadas)
       Estado del controlador PD congelado

t = 5 min  Pausa finalizada
           La batería puede cargar con excedente solar
           La descarga permanece bloqueada mientras el VE sigue cargando

t = N  Estado VE → cualquier otro valor (Idle / Disconnected / …)
       Operación normal reanudada
```

### ¿Por qué la pausa de 5 minutos?

Cuando un cargador VE se activa, negocia la corriente disponible con el coche durante una breve fase de handshake. Cualquier descarga de la batería durante esa ventana puede reducir temporalmente la capacidad de red aparente, haciendo que el cargador se estabilice en una corriente más baja. La pausa da tiempo al handshake para completarse antes de que la batería actúe.

### Comparativa con la opción estándar de excedente solar

| | Exclusión estándar + Excedente solar | VE sin telemetría |
|---|---|---|
| Requiere sensor de potencia | Sí | No |
| La batería descarga para el VE | Nunca | Nunca |
| La batería carga con solar mientras el VE carga | Sí | Sí (tras pausa de 5 min) |
| Pausa inicial de 5 minutos | No | Sí |
| Reacciona automáticamente al estado del VE | No | Sí |
