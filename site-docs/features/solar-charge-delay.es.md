# Retraso de carga solar

Retrasa la carga matutina de la batería (tanto solar como desde la red) mientras la producción solar prevista sea suficiente para cubrir la energía necesaria. Evita cargar la batería a primera hora —ya sea con solar o con red— cuando el sol podrá hacerlo más tarde.

## Aplicación

- Carga matutina normal (cuando la batería se ha descargado durante la noche).
- Carga semanal al 100 % (espera a que el sol complete la carga antes de recurrir a la red).

## Modelo solar

La integración usa un **modelo sinusoidal** basado en la previsión solar del día actual para estimar la producción hora a hora a lo largo del día. Compara la producción acumulada esperada desde la hora actual hasta el anochecer con la energía que falta por cargar.

```
Si producción_solar_restante >= energía_a_cargar:
    Esperar (el sol lo cargará)
Si no:
    Iniciar carga (solar o desde la red)
```

## Previsión en tiempo real

La integración lee el sensor de previsión solar en tiempo real, sin captura ni almacenamiento nocturno. Al guardar **Restante de hoy**, se usa directamente y sustituye el ajuste legado del día completo. Las entradas legadas que no se modifiquen siguen siendo compatibles durante la transición y se convierten en una estimación de la producción restante.

Cada vez que el valor del sensor cambia en más de 0,05 kWh, la integración reevalúa el balance energético:

- **La previsión empeora** hasta que `(energía_usable + previsión) < consumo_diario_medio` → desbloquea el retraso y la carga comienza de inmediato.
- **La previsión mejora** mientras el retraso sigue activo → el sistema sigue esperando a que el sol cargue la batería.

Una vez que el retraso se desbloquea, permanece desbloqueado el resto del día.

## Horizonte de demanda del hogar

Cuando el perfil horario de consumo de 28 días está maduro, el Retraso de Carga
Solar usa el mismo rango local de consumo restante que la carga predictiva. El
perfil mantiene las franjas de carga predictiva dentro del rango solicitado
porque el hogar sigue consumiendo mientras opera la batería; la energía usada
para cargarla desde la red ya queda cancelada por el término de potencia AC. La
demanda ya observada hoy no se cuenta de nuevo. Mientras aprende, el retraso vuelve a
la estimación diaria heredada. Los atributos del sensor de Retraso de Carga
exponen `consumption_forecast_source`, `profile_coverage_ratio` y `profile_days`
para verificar esta transición.

!!! note "Cortes transitorios de la previsión y reevaluación manual"
    Que un sensor de previsión configurado lea `unavailable`/`unknown` por un instante —mientras se actualiza, o durante la ventana tras reiniciar Home Assistant antes de que todos los sensores hayan cargado— ya **no** desactiva el retraso durante todo el día. La misma protección cubre un `0 kWh` provisional del sensor **Restante de hoy** durante la primera hora tras medianoche. El retraso se mantiene mediante una breve ventana de gracia (estado del sensor `Waiting for forecast`) y solo se desbloquea si la lectura no se recupera al superarla. Si el retraso ya se había desbloqueado y quieres recuperarlo el mismo día, **desactiva y vuelve a activar el switch de Retraso de Carga Solar**: eso reevalúa el retraso desde cero en lugar de esperar al reinicio de medianoche.

## SOC de arranque del retraso

Un SOC de setpoint opcional (12–90 %, desactivado por defecto) divide la carga en dos fases:

1. **Por debajo del setpoint** — la batería carga libremente (solar y red), el retraso está inactivo. Estado del sensor: `Charging to setpoint`.
2. **En el setpoint o por encima** — se activa la lógica de retraso solar y decide si continuar cargando o esperar.

Esto es útil cuando la batería está muy descargada y necesita un mínimo garantizado antes de que entre en juego la decisión solar. Por ejemplo, con un setpoint del 50 % la batería carga hasta el 50 % sin restricciones; a partir de ahí, el sistema evalúa si la producción solar restante es suficiente para completar la carga y espera si lo es.

Durante esta primera fase, el sensor publica `soc_setpoint`, `estimated_setpoint_time` y `projected_unlock_time`. Las dos horas son proyecciones informativas; cuando el retraso solar ya está activo, `estimated_unlock_time` pasa a ser la estimación autoritativa calculada por el controlador.

El setpoint se habilita con un checkbox independiente en la configuración. Si está desactivado, el retraso aplica desde el primer momento de carga. El valor mínimo es el 12 %, correspondiente al SOC mínimo de descarga de las baterías Venus.

## Configuración desde el dashboard

| Campo | Descripción | Por defecto |
|---|---|---|
| **Margen de seguridad (h)** | Horas antes del atardecer en las que la carga debe haber terminado. | 1 h |
| **Sensor de previsión solar** | Se usa cuando no se configuró un sensor de previsión durante la configuración inicial. | — |
| **Activar SOC mínimo antes del retraso** | Si se activa, la batería carga hasta el SOC configurado antes de que empiece el retraso solar. | Desactivado |
| **SOC mínimo (%)** | SOC que debe alcanzar la batería antes de que se aplique el retraso solar. | — |
| **Banda muerta del balance (kWh)** | Tolerancia para la comprobación del balance energético. Si batería más previsión quedan por debajo del consumo esperado, el retraso dura más. | `0,5 kWh` |

Un margen mayor (por ejemplo, 180 minutos) desbloquea antes la carga desde la red; un margen menor espera más tiempo a que el sol cubra la energía.

![Configuración del retraso de carga solar](../assets/screenshots/configuration/advanced-solar-charge-delay-config.png){ width="650" style="display: block; margin: 0 auto;"}

## Requisitos

- Sensor de previsión solar configurado en el [paso inicial](../configuration/main-sensor.md).

![Atributos del retraso de carga solar](../assets/screenshots/features/solar-charge-delay-attributes.png){ width="650"  style="display: block; margin: 0 auto;"}
