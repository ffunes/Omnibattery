# Carga predictiva — Modo Franja Horaria

Carga desde la red durante una **ventana horaria fija** (típicamente tarifa nocturna barata).

## Configuración

| Campo | Descripción |
|---|---|
| **Ventana de carga 1** | Inicio y fin de la primera franja de carga (p. ej. `02:00` – `05:00`), más los días de la semana en que aplica |
| **Ventanas de carga 2 y 3** | (Opcional) Hasta dos ventanas más, cada una con su inicio/fin y días |
| **Sensor de previsión solar** | Sensor de producción solar del día actual en kWh (opcional) |

!!! note "Hasta 3 ventanas"
    Puedes configurar 1, 2 o 3 ventanas de carga — útil para una tarifa con bloque nocturno y otro de mediodía. Rellena solo la ventana 1 para el comportamiento de ventana única anterior; cada ventana extra necesita **tanto** una hora de inicio como de fin (rellena ambas o déjalas ambas vacías). Estas franjas solo programan la carga predictiva desde la red: el historial del consumo de la casa sigue cubriendo las 24 horas, mientras que la potencia AC negativa de la batería elimina del consumo derivado la energía usada para cargarla.

!!! note "Sin sensor solar"
    Si no tienes paneles solares, deja vacío el sensor de previsión. El sistema cargará siempre que la energía de la batería sea insuficiente para cubrir el consumo esperado.

![Formulario de configuración — Modo Franja Horaria](../../assets/screenshots/configuration/predictive-charging/time-slot-form.png){ width="650"  style="display: block; margin: 0 auto;"}

## Flujo de evaluación

1. **Al entrar en el slot**: el sistema evalúa inmediatamente el balance restante si no hay sensor de previsión solar o si el sensor configurado es legible. Si la previsión está temporalmente indisponible, la evaluación se reintenta durante un máximo de cinco minutos para evitar una decisión falsa durante una actualización del proveedor.
2. **Después de la evaluación**: el sistema simula consumo, solar y energía utilizable de batería en intervalos de 15 minutos hasta medianoche. Si la previsión sigue indisponible tras ese margen, evalúa de forma conservadora suponiendo cero solar.
3. Cada ventana configurada recibe su propia cuota en kWh. La energía necesaria antes de un cruce previsto del SOC mínimo solo se asigna a ventanas capaces de entregarla a tiempo; la energía posterior se reparte entre las demás ventanas configuradas.
4. Se envía una notificación con la decisión. Si ninguna ventana puede cumplir un plazo, los atributos diagnósticos muestran los kWh no cubiertos en vez de afirmar que una ventana posterior los resuelve.
5. La carga se detiene al almacenar la cuota de la ventana actual o cuando termina la ventana. Así la primera franja ya no consume por defecto todo el objetivo flexible del día.

El planificador nunca abre una ventana no configurada. Un *shortfall* de plazo significa que las ventanas elegidas o la potencia física no pueden entregar suficiente energía a tiempo; la vivienda todavía puede importar de red cuando la batería alcance su mínimo.

## Reevaluación dentro de la ventana

La decisión tomada al entrar en el slot no es definitiva. Mientras la ventana está abierta, el balance energético se vuelve a evaluar cuando:

- **El SOC cae un 30 % o más** respecto al último punto de evaluación (p. ej. por un consumo elevado).
- **Se cruza o se recupera el suelo de SOC mínimo garantizado**, si esa opción está activada.
- **El proveedor revisa la previsión solar** en 1,5 kWh o más en cualquier dirección. Una previsión restante baja sola a lo largo del día, así que la lectura guardada se proyecta hacia adelante con la solar realmente producida desde entonces y solo cuenta como revisión la diferencia contra esa proyección. Limitado a un *cooldown* de 30 minutos y cuatro reevaluaciones al día.
- **Cambia un ajuste del que depende el balance**: el SOC mínimo o máximo de una batería, el margen de seguridad de la previsión solar, el margen de carga de red predictiva o el suelo de SOC mínimo garantizado.
- **Pulsas el botón Reevaluar Carga Predictiva** (`button.*_reevaluate_dynamic_pricing`) en el dispositivo del sistema.

Solo una reevaluación que invierte la decisión del slot reemplaza la notificación; las demás son silenciosas.

Estos disparadores actúan **solo dentro de una ventana de carga**: fuera de ella no hay nada que replanificar, porque este modo nunca carga de red fuera de sus ventanas configuradas. Si tus ventanas son nocturnas y la previsión se desploma a mediodía, la corrección ocurre en la siguiente ventana, no al instante. Precio Dinámico, que programa sus propias franjas, no tiene esta limitación.

Franja Horaria y Precio Dinámico usan un único timeline solar fechado y un
único presupuesto de energía restante. El perfil aprendido cambia
automáticamente los plazos intradía cuando alcanza la madurez; mientras tanto
se usa la curva sinusoidal. Nunca aumenta el total previsto ni abre una ventana
que no esté configurada.
