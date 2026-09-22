# Límite de carga por temperatura

Reduce la potencia de carga y descarga cuando la batería se calienta. Por encima de un límite de temperatura configurable, la potencia se reduce proporcionalmente y se recupera cuando la batería se enfría.

La reducción es lineal: la potencia de carga baja desde el límite normal hasta un mínimo configurable mientras la temperatura atraviesa el límite y la banda de rampa. El mínimo queda limitado por la potencia operativa mínima de cada batería (Marstek v2/v3/vA/vD y Zendure = 0 W). También se puede activar la reducción de descarga para mantenerse por debajo del corte por sobretemperatura del BMS.

## Configuración desde el dashboard

| Campo | Descripción | Por defecto |
|---|---|---|
| **Límite de temperatura (°C)** | La carga funciona a plena potencia hasta esta temperatura; la reducción empieza por encima. | `40 °C` |
| **Banda de rampa (°C)** | Rango de temperatura por encima del límite en el que la potencia de carga baja gradualmente hasta el mínimo. | `10 °C` |
| **Potencia mínima de carga (%)** | Potencia de carga al alcanzar el límite más la banda, como porcentaje del máximo normal. `0 %` detiene la carga cuando hace mucho calor. | `40 %` |
| **Reducir también la descarga** | Aplica la misma reducción a la descarga para mantenerse por debajo del corte por sobretemperatura del BMS. | Desactivado |

![Configuración del límite de carga por temperatura](../assets/screenshots/configuration/advanced-temperature-charge-limit-config.png){ width="650" style="display: block; margin: 0 auto;"}

Los controles están disponibles en los seis idiomas compatibles.
