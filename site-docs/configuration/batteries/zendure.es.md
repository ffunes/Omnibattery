# Zendure SolarFlow

Omnibattery controla los dispositivos Zendure SolarFlow compatibles mediante su interfaz de red local. Detecta el modelo durante la configuración y aplica la envolvente de potencia correspondiente.

## ¿Lo necesito?

| Modelo compatible | Carga máxima en CA | Descarga máxima en CA | Entrada solar visible para Omnibattery |
|---|---:|---:|---|
| SolarFlow 800 / 800 Plus / 800 Pro | 1.000 W | 800 W | Sin telemetría MPPT específica |
| SolarFlow 1600 AC+ | 1.600 W | 1.600 W | No |
| SolarFlow 2400 AC Pro / 2400 AC+ | 2.400 W | 2.400 W | Sin telemetría MPPT específica |
| SolarFlow 3000 Mix AC+ | 3.000 W | 3.000 W | Sin telemetría MPPT específica |
| SolarFlow 4000 Mix AC+ | 4.000 W | 4.000 W | No |
| SolarFlow 4000 Mix Pro | 4.000 W | 4.000 W | Telemetría MPPT dual |

**Úsalo si** Home Assistant puede llegar al SolarFlow en la red local y puedes mantener desactivado el sistema de gestión energética doméstica (HEMS) de Zendure. **No uses ambos controladores a la vez:** HEMS anula la orden de Omnibattery después de unos segundos.

## Antes de empezar

- Desactiva **HEMS** en la app Zendure.
- Asigna al dispositivo una dirección IP local estable y confirma que Home Assistant puede acceder a él.
- Anota el puerto HTTP local; su valor predeterminado es `80`.
- Conoce la capacidad nominal de la batería, porque el informe local no la proporciona.

## Cómo añadirlo

1. En el asistente de configuración de Omnibattery, elige **Zendure SolarFlow**.
2. Introduce un **Name** descriptivo y el **Host IP** del dispositivo.
3. Mantén **HTTP port** en `80` salvo que tu red use otro puerto.
4. Espera mientras Omnibattery lee el informe del dispositivo y detecta el modelo.
5. Introduce la capacidad nominal y elige límites de potencia y estado de carga dentro de la envolvente detectada.

## Qué verás

Omnibattery proporciona control automático y controles por software **Force Mode**, **Set Charge Power** y **Set Discharge Power**. Activa **Battery Manual Control** antes de usar esos valores manuales. La orden activa se envía mediante la interfaz local del dispositivo; mantén HEMS desactivado para que Zendure no recupere el control.

El estado de carga (SOC), potencia de batería, temperatura, energía y otras lecturas aparecen cuando el modelo las comunica. Solo SolarFlow 4000 Mix Pro proporciona telemetría específica de seguimiento del punto de máxima potencia (MPPT) mediante esta conexión.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El asistente no puede conectarse | La dirección o el puerto HTTP son incorrectos, o la interfaz local no está accesible | Prueba la dirección del dispositivo desde la red de Home Assistant |
| La potencia cambia brevemente y vuelve al reposo | HEMS está activado | Desactiva HEMS en la app Zendure |
| El modelo o límite es menor de lo previsto | El informe del dispositivo indicó otro producto o un límite de hardware inferior | Comprueba el producto que muestra Zendure y el límite de carga comunicado |
| Los valores manuales no tienen efecto | La batería sigue en el grupo automático | Activa **Battery Manual Control** para esa batería |
| Falta la energía almacenada o la eficiencia | Falta la capacidad nominal o es incorrecta | Vuelve a configurar la batería e introduce su capacidad nominal utilizable |

??? "Detalles avanzados"
    La configuración lee `/properties/report` y asigna el producto comunicado a un perfil de modelo. El informe sigue siendo la fuente autorizada cuando anuncia un límite de carga menor que el perfil.

    Zendure no tiene registros de modo forzado como Marstek. Para el control en vivo, Omnibattery escribe el modo de carga o descarga y su límite con el control no persistente activado, y después actualiza el objetivo durante los ciclos normales del controlador. Las escrituras de configuración, como los límites de SOC, usan modo persistente.

    Las entradas existentes de SolarFlow 2400 AC+ se pueden promocionar automáticamente cuando el dispositivo comunica después un identificador de producto SolarFlow 4000 Mix. Los límites de usuario guardados se conservan hasta que los aumentes en las opciones de batería.

    El ajuste de SOC mínimo de Zendure acepta `5–50%`. La capacidad nominal acepta `0.01–100 kWh`. Zendure no usa la reducción gradual de carga por tensión de celda de Marstek.

    Para controles compartidos en tiempo de ejecución y límites del sistema, consulta [Elige la conexión de tu batería](index.md).
