# Marstek

Omnibattery es compatible con las baterías Marstek Venus E/C, Venus A y Venus
D. La conexión puede usar Modbus TCP, Modbus RTU mediante USB–RS485 o un puente
LilyGo RS485 con el firmware ESPHome compatible.

## Opciones de conexión

Para Modbus TCP, la Venus E v2 normalmente necesita un conversor RS485 a TCP,
como un Elfin-EW11. Venus E v3, Venus A y Venus D tienen Ethernet nativo. Para
Modbus RTU, conecta un adaptador USB–RS485 al equipo donde se ejecuta Home
Assistant.

El asistente solicita:

| Campo | Descripción | Por defecto |
|---|---|---|
| **Nombre** | Nombre usado para el dispositivo de batería | — |
| **IP del host** | IP de la batería o del conversor Modbus; déjala vacía para RTU | — |
| **Puerto Modbus** | Puerto TCP | `502` |
| **Puerto serie** | Ruta USB–RS485, por ejemplo `/dev/ttyUSB0` o `COM3`; se usa en lugar del host para RTU | — |
| **ID de esclavo Modbus** | ID de unidad cuando varias baterías comparten un endpoint | `1` |
| **Versión de batería** | Mapa de registros del modelo instalado | — |

Al usar el puente LilyGo, elige **Marstek mediante LilyGo RS485 (ESPHome)** en
el selector de marca y selecciona el dispositivo ESPHome. El puente debe
exponer en Home Assistant las entidades Marstek requeridas.

![Formulario de conexión Marstek](../../assets/screenshots/configuration/battery-connection-form.png){ width="650"  style="display: block; margin: 0 auto;"}

## Versiones de batería

| Versión | Modelos |
|---|---|
| `v1/v2` | Venus E v1, Venus E v2 |
| `v3` | Venus E v3 |
| `vA` | Venus A |
| `vD` | Venus D |

!!! warning "La potencia máxima depende del modelo"
    Venus E v2/v3 admite **2500 W**, Venus A admite **1500 W** y Venus D admite **2200 W** antes del firmware EMS 149 (o mientras se desconoce su versión) y **2500 W** a partir de EMS 149. Usa el máximo correspondiente solo si tienes la certeza de que la instalación doméstica puede soportarlo de forma segura.

## Límites específicos de Marstek

La página de límites incluye los controles comunes de potencia de carga/descarga,
SOC y umbral de backup. Marstek también ofrece la **reducción de carga por
voltaje al 100 %**: cuando el objetivo es 100 %, la carga se limita a 200 W
desde una tensión máxima de celda de 3,48 V. En los modelos Venus E se detiene
a 3,60 V para que la integración pueda medir el desequilibrio de celdas tras
60 segundos; en Venus A/D con packs acoplados continúa a 200 W hasta que corta
la BMS.

Esta protección por tensión y el monitor de equilibrio de celdas son específicos
de Marstek. Consulta el [Monitor de equilibrio de celdas](../../features/cell-balance-monitor.md)
para la secuencia de medición y recuperación.

![Formulario de configuración Marstek](../../assets/screenshots/configuration/battery-config-form.png){ width="650"  style="display: block; margin: 0 auto;"}

Para los sliders de SOC, los límites de potencia en tiempo de ejecución, los
límites del sistema y los umbrales de backup, consulta los [ajustes comunes de batería](index.md).
