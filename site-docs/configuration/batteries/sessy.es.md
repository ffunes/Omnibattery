# Sessy Home Battery

Omnibattery controla una Sessy mediante la interfaz local de su dongle de red. No se necesita conexión a la nube.

## ¿Lo necesito?

**Úsalo si** Home Assistant puede acceder al dongle Sessy y tienes el nombre de usuario y contraseña impresos en él. **Espera una primera respuesta con retraso:** una Sessy que sale del modo de espera puede tardar hasta 65 s en seguir su primera orden distinta de cero.

El soporte de Sessy aún busca más personas para probarlo. Incluye el modelo y el firmware al informar de un comportamiento que difiera de esta página.

## Antes de empezar

- Asigna al dongle Sessy una dirección local estable y confirma que Home Assistant puede acceder a él.
- Busca el nombre de usuario y contraseña de la API del dongle.
- Conoce la capacidad nominal de la batería; la interfaz local no la comunica.
- Mantén la conexión dentro de tu red local de confianza.

## Cómo añadirla

1. En el asistente de configuración de Omnibattery, elige **Sessy**.
2. Introduce un **Name** descriptivo y el **Host** del dongle.
3. Mantén **HTTP port** en `80` salvo que el dongle use otro puerto.
4. Introduce el **Username** y **Password** del dongle y espera la prueba de conexión.
5. Introduce la capacidad nominal y elige los límites comunes de potencia y estado de carga.

## Qué verás

Omnibattery proporciona control automático y controles por software **Force Mode** y de potencia. Activa **Battery Manual Control** antes de enviar un objetivo manual de carga, descarga o reposo. La integración envía un único objetivo de potencia neta mediante la interfaz local de Sessy.

El estado de carga (SOC), potencia, temperatura, energía y lectura FV del dispositivo pueden aparecer como entidades. La lectura FV es informativa: el driver Sessy no la anuncia como telemetría solar para los cálculos de control de Omnibattery.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El asistente no puede conectarse | La dirección, puerto, nombre de usuario o contraseña son incorrectos | Copia las credenciales del dongle y prueba la conectividad de red |
| Parece que la primera orden no hace nada | La batería está saliendo del modo de espera | Espera hasta 65 s antes de considerar fallida la orden |
| Faltan la energía almacenada o los cálculos predictivos | La capacidad nominal no se introdujo correctamente | Vuelve a configurar la batería e introduce su capacidad |
| Los valores manuales no tienen efecto | La batería sigue en el grupo automático | Activa **Battery Manual Control** |
| La FV es visible pero no cuenta como solar del sistema | Sessy no declara esta lectura como fuente solar del controlador | Configura el sensor solar habitual de la instalación si es necesario |

??? "Detalles avanzados"
    Sessy usa límites asimétricos: `2,200 W` para carga y `1,700 W` para descarga. El formulario de configuración establece inicialmente esos límites y acepta una capacidad nominal de `0.01–100 kWh`.

    La interfaz local utiliza valores de generación/descarga positivos, mientras que el signo interno de Omnibattery usa carga positiva y descarga negativa. El driver convierte el signo antes de enviar la consigna neta y la lee de vuelta para confirmarla.

    La estrategia y el objetivo de potencia de Sessy se controlan mediante su API local autenticada. No tiene registros de modo forzado como Marstek, escrituras de SOC de hardware, reducción gradual por tensión de celda ni telemetría de equilibrio de celdas.

    Para controles compartidos en tiempo de ejecución y límites del sistema, consulta [Elige la conexión de tu batería](index.md).
