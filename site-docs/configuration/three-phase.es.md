# Proteger cada fase de un suministro trifásico

La protección de corriente trifásica limita los comandos automáticos de la batería en la fase donde está conectada cada batería. Ayuda a mantener la carga y descarga de la batería dentro del margen actual que reserva para esa fase.

## ¿Lo necesito?

**Úsalo si** tus baterías comparten una instalación trifásica y necesitas un techo de corriente independiente para cada fase física.

**No lo necesita si** su instalación es monofásica, su protección eléctrica ya maneja la envolvente operativa requerida sin límites de software, o no puede medir la corriente firmada en cada fase que desea proteger.

## Antes de empezar

- Utilice un sensor de corriente cuadrática media (RMS) con signo en `A` o `mA` para cada fase protegida. Positivo debe significar importación de red y negativo debe significar exportación de red después de aplicar la configuración de señal de medidor global.
- Conozca el límite de corriente para cada fase y deje el margen operativo por debajo de la clasificación de la placa del fusible.
- Confirme qué fase física utiliza cada batería. Omnibattery no puede descubrir el cableado.
- Trate esta característica como una protección de software adicional, no como un reemplazo de los disyuntores, la protección del inversor o el diseño eléctrico.

## Cómo activarlo

1. Abra **Configuración → Dispositivos y servicios → Omnibattery → Configurar → Sensores** y habilite **Protección de corriente trifásica**.
2. Para cada fase protegida, seleccione su **sensor de corriente de red L1/L2/L3** e ingrese el **tamaño de fusible L1/L2/L3 (A)** correspondiente. Deje ambos campos vacíos para una fase no utilizada.
3. En la configuración de cada batería, establezca **Fase física de la batería** en su conductor real. Elija **Sin asignar** solo cuando la batería esté fuera del diseño de fase protegida.
4. Finalice la configuración, luego active **Protección de corriente trifásica** desde el panel de Omnibattery o la página del dispositivo.
5. Confirme que el **Estado de protección trifásica** informe **Activo** bajo carga normal.

## lo que veras

**Estado de protección trifásica** muestra uno de estos estados:

| Estado | Significado |
|---|---|
| **Desactivado** | El interruptor de protección de tiempo de ejecución está apagado |
| **Activo** | Los sensores de fase configurados están en buen estado y no se está reduciendo ningún comando de batería |
| **Limitación de baterías** | Se está limitando al menos un comando automático de batería |
| **Degradado / A prueba de fallos** | Una fase configurada no puede proporcionar una lectura actual válida |

Cuando el interruptor de protección está activado, el selector **Fase de batería física** de cada batería está disponible para corregir el tiempo de ejecución. Una asignación modificada persiste y se aplica en el siguiente ciclo de control automático. El sensor de energía de la red global sigue siendo la señal de control; Los sensores de fase sólo limitan el resultado.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El estado es **Degradado/A prueba de fallos** | Falta un sensor de corriente configurado, está obsoleto, no está disponible, no es numérico o está en la unidad incorrecta | Compruebe que la entidad informe un valor numérico nuevo en `A` o `mA` |
| La carga de la batería en una fase permanece en cero | Su fase protegida no tiene lectura actual válida | Verifique el sensor, el límite y la asignación física de esa fase |
| Una batería nunca está limitada | Está **Sin asignar** o su fase no tiene un par completo de sensor y límite | Establecer la fase física y configurar ambos campos para esa fase |
| Los límites a las importaciones y exportaciones actúan en la dirección equivocada | El signo de fase-corriente está invertido | Compare una importación de red conocida con el sensor y revise **Señal de medidor invertida** |
| El medidor de fase aún supera el límite configurado | Una carga externa, latencia de comando o un comando manual causaron el exceso | Reducir el límite operativo configurado y mantener los comandos manuales dentro del envolvente eléctrico |

??? "Detalles avanzados"
    Para cada fase, Omnibattery reconstruye la corriente que no proviene de la batería y luego cruza el límite del medidor con un límite absoluto de corriente de la batería:

    ```text
    base_current = phase_current - battery_current_on_phase
    battery_current_min = max(-phase_limit, -phase_limit - base_current)
    battery_current_max = min(+phase_limit, +phase_limit - base_current)
    charge_budget_current = max(0, battery_current_max)
    discharge_budget_current = max(0, -battery_current_min)
    ```

    Los comandos de batería utilizan vatios positivos para carga y vatios negativos para descarga. Los presupuestos actuales se convierten con un factor de potencia nominal de 230 V y 0,90 y luego se redondean hacia abajo en pasos de 5 W. La selección normal de batería y la asignación proporcional se ejecutan primero. La energía rechazada por un límite de fase puede pasar a fases saludables con capacidad de batería disponible, siguiendo el estado normal de carga (SOC) y la prioridad energética.

    La envolvente se aplica al control proporcional-derivativo (PD), el seguimiento directo, la carga predictiva de la red, el control automático de intervalos de tiempo, el equilibrio activo y la protección final de comando automático compartido. El total aceptado se devuelve al controlador para evitar la liquidación.

    Una lectura actual de más de 65 segundos está obsoleta. Si una fase configurada no tiene una lectura válida, la nueva carga en esa fase tiene un límite de 0 W. Se puede mantener una descarga segura medida previamente porque detenerla devolvería la carga doméstica a la red y podría aumentar la corriente de la fase. Las fases saludables continúan. Una fase sin un par de sensor y límite configurado no tiene límite de fase. Una batería **No asignada** también permanece fuera del sobre.

    La entidad de estado expone atributos de diagnóstico que incluyen `limited_batteries`, `limited_battery_details`, `unassigned_batteries`, `degraded_phases` y lecturas, presupuestos y asignaciones por fase en `phases`.

    Las escrituras manuales de registros y los comandos manuales de intervalos de tiempo pueden omitir este sobre. Home Assistant crea una reparación mientras la protección está habilitada para recordarle que mantenga esos comandos dentro de los límites actuales configurados. El protector no puede eliminar la corriente causada por una carga externa, detectar un mapeo incorrecto de conductores o emitir carga simultánea en una fase y descarga en otra.
