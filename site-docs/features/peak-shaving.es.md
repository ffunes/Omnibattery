# Protección de capacidad (también conocido como peak shaving)

Reserva parte de la capacidad de la batería para satisfacer picos de demanda que superen un umbral de potencia configurable. En lugar de cubrir todo el consumo doméstico, la batería retiene energía y solo descarga para compensar la parte del consumo que supera el límite de pico — manteniendo capacidad en reserva para cuando realmente se necesita.

## Comportamiento sin peak shaving

El controlador PD cubre todo el consumo doméstico → la batería puede descargarse completamente si el consumo es alto y continuo.

## Comportamiento con peak shaving activo

Cuando el SOC está por debajo del umbral:
- La batería **no** cubre todo el consumo.
- Solo descarga para compensar la parte del consumo que supera el **límite de potencia de pico** configurado.

```
Potencia_batería = max(0, consumo_red - límite_pico)
```

## Configuración desde el dashboard

| Campo | Descripción | Por defecto | Rango |
|---|---|---|---|
| **Umbral de SOC (%)** | La protección de capacidad se activa por debajo de este SOC. | `30 %` | 20–100 % |
| **Límite de potencia de pico (W)** | Umbral de potencia de red. Con la protección activa, la batería solo descarga el exceso sobre este límite. | `2500 W` | 500–10000 W |

![Configuración de protección de capacidad](../assets/screenshots/configuration/advanced-capacity-protection-config.png){ width="650" style="display: block; margin: 0 auto;"}

## Ejemplo

```
Límite pico: 3 000 W
Consumo actual: 4 500 W

Potencia batería = 4 500 - 3 000 = 1 500 W
La red cubre 3 000 W y la batería solo 1 500 W
```

Si el consumo fuera de 2 000 W (< límite), la batería no descargaría nada.

## Interacción con la carga predictiva desde red

Durante una franja predictiva activa, el consumo del hogar siempre tiene
prioridad sobre la carga desde red. El controlador reduce primero la potencia
de carga de la batería; si alcanza el techo de importación, ordena reposo y
espera a que la telemetría del inversor y del contador se estabilice antes de
considerar una descarga.

Después de la estabilización:

- Con Protección de Capacidad activa, Peak Shaving descarga únicamente el
  exceso sobre `min(capacity_protection_limit, max_contracted_power)`.
- Independientemente del switch de Protección de Capacidad, una importación
  física superior a `max_contracted_power` activa la emergencia por potencia
  contratada. Las cargas excluidas de la cobertura ordinaria no pueden
  excluirse de esta comprobación porque la conexión de red también las ve.
- Las restricciones económicas de descarga por precio y las ventanas protegidas
  de precio negativo no pueden impedir una orden legítima de Peak Shaving o
  emergencia. Siguen aplicándose las protecciones físicas y del usuario: SOC
  mínimo, disponibilidad, propiedad manual o de franjas, backup, fases y límites
  de potencia.

Detener la carga predictiva, descargar por Peak Shaving, descargar por PD normal
y descargar por emergencia de potencia contratada son, por tanto, acciones
distintas. La protección de pico nunca convierte una franja barata en descarga
económica normal hacia el objetivo de red del PD. Cuando vuelve un margen
estable, primero detiene la descarga, espera otra estabilización y reanuda la
carga predictiva con histéresis. Conserva su SOC objetivo y los kWh pendientes.

Consulta [Consumo del hogar durante una franja de carga predictiva](../configuration/predictive-charging/index.es.md#consumo-del-hogar-durante-una-franja-de-carga)
para ver la secuencia completa y un ejemplo.

## Peak shaving para dispositivos excluidos

El switch opcional **Reducción de picos para dispositivos excluidos** extiende
el límite de pico a las cargas que normalmente están excluidas de la batería.
Está desactivado por defecto.

Cuando se activa y el SOC está por encima del umbral de conservación, la batería
sigue cubriendo el consumo normal del hogar. Si la parte excluida provocaría que
la importación de red superase el límite configurado, la batería cubre solamente
ese exceso.

Por ejemplo, con 1 000 W de consumo normal, 4 000 W de consumo excluido y un
límite de 3 000 W, la batería suministra 2 000 W: 1 000 W para el hogar y
1 000 W para recortar el pico del dispositivo excluido. La red queda en 3 000 W.

Cuando el SOC está por debajo del umbral de conservación, el comportamiento
existente ya aplica el límite de pico a la demanda total. El SOC mínimo, la
potencia de descarga disponible y el resto de protecciones continúan
aplicándose siempre.

## Cuándo usarlo

Útil cuando:
- La red tiene un coste fijo por potencia máxima contratada y quieres limitar los picos.
- Quieres asegurarte de tener reserva de batería para la noche.

![Configuración de peak shaving](../assets/screenshots/features/peak-shaving-config.png){ width="650"  style="display: block; margin: 0 auto;"}
