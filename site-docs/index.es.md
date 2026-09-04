![Omnibattery](assets/logo-github.png){ width="420" }

**Omnibattery** es una integración personalizada para Home Assistant que monitoriza y controla baterías solares enchufables de varias marcas. Actualmente es compatible con:

- **Marstek** Venus E/C (v2/v3), Venus A y Venus D mediante Modbus TCP, Modbus RTU o un puente LilyGo RS485/ESPHome.
- **Zendure** SolarFlow 4000 Mix Pro, 4000 Mix AC+, 2400 AC+, 2400 AC Pro, 1600 AC+, 800 Pro, 800 Plus y 800 mediante HTTP local.
- **Anker SOLIX** Solarbank Max AC y Solarbank 4 E5000 Pro mediante Modbus TCP.
- **Sessy** Home Battery mediante la API local de su dongle (se buscan testers).
- **Hoymiles** MS-A2 mediante la integración MQTT configurada en Home Assistant.

<div class="grid cards" markdown>

-   :material-battery-charging: **Control dinámico de potencia**

    Controlador PD dirigido por eventos que mantiene el intercambio con la red cerca del objetivo, con perfiles de ajuste de un clic y un sensor de calidad para encontrar una respuesta estable.

-   :material-calendar-clock: **Carga predictiva**

    Carga desde la red solo cuando la solar y la energía almacenada no bastan, con modos por franja horaria, precio dinámico y precio en tiempo real.

-   :material-battery-sync: **Multi-batería**

    Coordina hasta 10 baterías con prioridades de SOC, histéresis de energía y distribución de potencia basada en la eficiencia.

-   :material-brand_family: **Multi-marca**

    Combina baterías Marstek, Zendure, Anker SOLIX, Sessy y Hoymiles en una misma instalación, compartiendo el bucle de control, las entidades de sistema y las funciones de gestión energética.

-   :material-view-dashboard: **Dashboard integrado**

    Panel lateral integrado en Home Assistant con diagrama de flujo de potencia, gráficos históricos, estado de salud de las baterías y todos los ajustes de control en un único lugar, sin tarjetas HACS adicionales ni YAML.

-   :material-tune: **Altamente configurable**

    Ajusta franjas horarias, límites de SOC y potencia, peak shaving, carga semanal completa, retraso de carga solar y cargas excluidas desde Home Assistant.

</div>

## Dashboard de control integrado

El panel se instala automáticamente como un panel lateral de Home Assistant; no necesita tarjetas HACS adicionales ni configuración YAML. Incluye tres pestañas:

- **Resumen** con anillo de SOC animado, diagrama de flujo energético Red↔Casa↔Batería↔Solar, diagnósticos, una cuadrícula de gráficos 2×2 y una línea temporal diaria real/prevista
- **Baterías** con SOC/potencia por batería, salud y celdas, energía diaria, MPPT opcional, información de firmware y controles
- **Control** con los ajustes de todo el sistema agrupados por funcionalidad, cada uno con su interruptor y sus parámetros

![Dashboard](/assets/MVEM%20-%20Dashboard.gif)

## Características principales

- **Controlador PD (Zero Export/Import)**: ajusta en tiempo real la potencia de la batería para mantener el intercambio con la red próximo a cero.
- **Perfiles PD de un clic y sensor de calidad de control**: selecciona una respuesta de Muy suave a Muy agresiva y consulta el veredicto de calidad para saber si la regulación es estable, oscilante o lenta.
- **Modo de seguimiento directo sin PD** (opt-in): la batería sigue el sensor de consumo 1:1 en cada ciclo — sin integral, derivada, suavizado ni limitador de rampa — para instalaciones que prefieren seguimiento directo al controlador PD.
- **Compatibilidad multi-marca**: combina baterías compatibles Marstek, Zendure, Anker SOLIX, Sessy y Hoymiles en la misma instalación.
- **Carga predictiva**: tres modos (franja horaria, precio dinámico, precio en tiempo real — incluyendo Tibber) que cargan desde la red solo cuando el balance energético lo requiere. Utiliza una media móvil de 7 días del consumo real del hogar para decidir si es necesario cargar desde la red.
- **Gestión multi-batería**: selección inteligente con prioridades de SOC, histéresis de energía y eficiencia por zona de operación.
- **Franjas horarias**: controlan de forma independiente las ventanas de carga y descarga, con parámetros de SOC y potencia por franja.
- **Peak shaving**: reserva capacidad de la batería para satisfacer picos de demanda que superen un umbral de potencia configurable.
- **Carga semanal completa**: carga al 100% una vez por semana para equilibrar celdas.
- **Monitor de equilibrio de celdas**: mide la diferencia de tensión entre la celda más y menos cargada después de cada carga completa; hace seguimiento de la tendencia de desequilibrio a lo largo del tiempo, envía alertas ante desequilibrios moderados o altos y bloquea la descarga durante el periodo de reposo en circuito abierto.
- **Retraso de carga solar**: pospone la carga matutina de la batería (solar y desde la red) mientras la producción solar prevista es suficiente para cubrir la energía restante necesaria.
- **Balance neto horario**: ajusta el punto de trabajo del controlador PD de forma continua para mantener la energía neta de red en un objetivo configurable (por defecto: balance neto cero por hora). Compatible con sensores externos de balance neto y se combina limpiamente con el resto de funcionalidades mediante el registro de puntos de trabajo.
- **Exclusión de cargas**: excluye dispositivos de alta potencia (p. ej. cargadores de VE) para que el controlador no intente compensar su consumo. Cada dispositivo excluido tiene un slider de porcentaje de exclusión individual (0–100%).
- **Notificaciones proactivas de alarmas (solo baterías Marstek v2)**: monitoriza los registros de fallos y alarmas de la batería cada 5 segundos y envía una notificación de Home Assistant en el momento en que se detecta una nueva condición, con el nombre exacto del fallo o alarma. El sensor de sistema `System Alarm Status` (`OK` / `Warning` / `Fault`) ofrece una vista rápida del estado de todas las baterías.

## Aviso de responsabilidad

!!! danger "Exención de responsabilidad"
    Este software se proporciona "tal cual", sin garantía de ningún tipo. El uso es bajo tu propio riesgo. El desarrollador no asume ninguna responsabilidad por daños a baterías, inversores, instalación eléctrica, pérdidas económicas o lesiones personales.

    **Si no aceptas estos términos, NO instales ni uses esta integración.**

## Soporte

Si encuentras útil esta integración, puedes apoyar el proyecto:

<a href="https://buymeacoffee.com/ffunes" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="40" width="145"></a>
