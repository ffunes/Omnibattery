# Límite de carga por temperatura

El límite de carga por temperatura protege una batería caliente reduciendo su potencia a medida que aumenta la temperatura interna. Úsalo como límite operativo adicional junto al sistema de gestión de batería (BMS), cuyas protecciones de seguridad siguen siendo determinantes.

## ¿Lo necesito?

**Úsalo si** una o varias baterías se calientan repetidamente por su ubicación, el clima o una potencia alta sostenida, y prefieres una reducción gradual antes de que el BMS alcance su propio corte.

**No lo necesitas si** la temperatura de la batería se mantiene cómoda a plena potencia o tu batería no publica una lectura de temperatura interna en Omnibattery.

## Antes de empezar

- Confirma que cada batería que quieras proteger tiene una entidad **Temperatura interna** con valor numérico.
- Es una función a nivel de sistema, pero Omnibattery calcula el límite por separado para cada batería a partir de su propia temperatura.
- Decide si solo debe limitarse la carga. Limitar la descarga es opcional y usa la misma curva de temperatura.

## Cómo activarlo

1. Abre el panel lateral de Omnibattery y selecciona **Control**.
2. En **Límite de carga por temperatura**, activa **Límite de carga por temperatura**.
3. Configura **Límite de carga por temperatura**, **Banda del límite de carga por temperatura** y **Suelo del límite de carga por temperatura**.
4. Activa **Límite de descarga por temperatura** solo si también quieres que las baterías calientes reduzcan la potencia de descarga.

![Configurar el límite de carga por temperatura](../assets/screenshots/configuration/advanced-temperature-charge-limit-config.png){ width="650" style="display: block; margin: 0 auto;"}

Los controles predeterminados son:

| Control | Efecto | Predeterminado |
|---|---|---:|
| **Límite de carga por temperatura** | Se permite plena potencia a esta temperatura o por debajo; la reducción comienza por encima | 40 °C |
| **Banda del límite de carga por temperatura** | Intervalo de temperatura en que la potencia cae hasta el suelo configurado | 10 °C |
| **Suelo del límite de carga por temperatura** | Porcentaje mínimo del techo de potencia actual en la parte superior de la banda | 40% |
| **Límite de descarga por temperatura** | Aplica la misma curva a la descarga | Desactivado |

## Qué verás

Por debajo del límite configurado, esta función no cambia el techo de potencia de la batería. A medida que la batería se calienta a través de la banda de rampa, la potencia permitida baja suavemente; al enfriarse, vuelve a subir suavemente.

Con los valores predeterminados, la reducción empieza por encima de 40 °C y alcanza el suelo del 40% a 50 °C. Otros límites de batería activos aún pueden producir un techo inferior.

**Estado de la integración** incluye detalles `temperature_charge_limit` de cada batería: temperatura informada, límite configurado, banda de rampa, suelo, factor de reducción y si la reducción está activa.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| La potencia no se reduce cuando una batería está caliente | La función está desactivada o la temperatura no ha superado el límite configurado | **Límite de carga por temperatura**, **Temperatura interna** y el umbral configurado |
| Una batería reduce potencia y otra no | Los límites se calculan a partir de la temperatura propia de cada batería | Los valores de **Temperatura interna** de ambas baterías |
| Una batería sin lectura de temperatura sigue cargando | Los datos de temperatura ausentes o no válidos dejan sin cambios su límite existente | Disponibilidad de la batería y entidad de temperatura interna |
| La potencia no baja tanto como sugiere el porcentaje | El controlador declara una potencia operativa mínima fiable | El rango de potencia admitido de la batería y **Estado de la integración** |
| La descarga sigue sin límite | Limitar la descarga es opcional | **Límite de descarga por temperatura** |

??? "Detalles avanzados"
    El suelo configurado es un porcentaje del techo actual por batería, tras cualquier límite de control anterior. Para temperatura `T`, límite `L`, banda de rampa `B` y fracción de suelo `F`, Omnibattery usa:

    ```text
    factor = 1                                      when T <= L
    factor = 1 - ((T - L) / B) × (1 - F)           when L < T < L + B
    factor = F                                      when T >= L + B
    temperature_limited_power = current_limit × factor
    ```

    La curva es continua y no tiene un cierre de enfriamiento ni histéresis independientes. Una temperatura ausente o no numérica mantiene sin cambios el límite existente.

    Omnibattery nunca aumenta un techo impuesto en otro lugar. Tras aplicar el porcentaje, también respeta cualquier potencia mínima fiable de carga o descarga declarada por el controlador de batería. Los controladores que no declaran mínimo pueden alcanzar 0 W cuando el suelo configurado es 0%; uno con mínimo no nulo se mantiene en el suelo operativo declarado.

    **Límite de descarga por temperatura** usa la misma curva ajustada para carga. Está desactivado de forma predeterminada porque la descarga puede tolerar el calor de otra manera; actívalo cuando reducir ambas direcciones sea apropiado para tu instalación.
