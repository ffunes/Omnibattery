# Anker SOLIX

Omnibattery controla las baterías Anker SOLIX Solarbank compatibles mediante la conexión local Modbus TCP. La prueba de configuración lee el modelo y sus límites de hardware en vivo antes de crear la batería.

## ¿Lo necesito?

| Modelo compatible | Conexión de control | Telemetría solar independiente |
|---|---|---|
| Solarbank Max AC | Modbus TCP | No; sus registros solares se obtienen de medidas de CA |
| Solarbank XE AC | Modbus TCP | No; sus registros solares se obtienen de medidas de CA |
| Solarbank 4 E5000 Pro | Modbus TCP | Sí |

**Úsalo si** tu Solarbank compatible expone Modbus TCP y la app Anker permite **Third-Party Control**. **No añadas otro cliente Modbus:** la batería acepta una sola sesión de cliente a la vez.

## Antes de empezar

- Activa **Third-Party Control** y Modbus TCP en la app Anker.
- Cierra o desconecta cualquier otra aplicación que use la conexión Modbus de la batería.
- Asigna una dirección IP local estable a la Solarbank.
- Anota el ID de esclavo Modbus.

## Cómo añadirla

1. En el asistente de configuración de Omnibattery, elige **Anker SOLIX Solarbank Max AC / 4 E5000 Pro**. Esta opción del asistente también detecta Solarbank XE AC.
2. Introduce un **Name** descriptivo y el **Host IP** de la Solarbank.
3. Mantén **Modbus port** en `502` salvo que el dispositivo use otro puerto.
4. Introduce el **Modbus slave ID**; su valor predeterminado es `1`.
5. Espera la prueba de conexión y después elige los ajustes comunes de estado de carga y seguridad.

## Qué verás

Anker comunica sus propios límites de carga y descarga, por lo que Omnibattery usa esos valores detectados en vez de pedir límites de potencia durante la configuración. Hay control automático y controles por software **Force Mode**, **Set Charge Power** y **Set Discharge Power**. Activa **Battery Manual Control** antes de enviar objetivos manuales y mantén activado **Third-Party Control** en la app.

El mapa de registros común proporciona estado de carga (SOC), potencia de batería, temperatura, energía y estado de salud (SoH) cuando el dispositivo implementa esas lecturas. Anker no expone la tensión del paquete ni las tensiones de celdas individuales mediante esta conexión, por lo que no están disponibles las funciones de equilibrio de celdas y reducción gradual por tensión.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El asistente no puede conectarse | Third-Party Control está desactivado, la dirección es incorrecta u otro cliente controla Modbus | Activa las opciones de la app, cierra el otro cliente y vuelve a intentarlo |
| La batería ignora una orden | Third-Party Control se desactivó después de la configuración | Vuelve a activarlo en la app Anker |
| La potencia se detiene por debajo de la especificación del producto | El límite del dispositivo en vivo o la envolvente de seguridad de Omnibattery es menor | Comprueba las entidades de límite de carga y descarga detectadas |
| Falta potencia solar en Max AC o XE AC | Estos modelos no proporcionan una fuente solar independiente mediante estos registros | Usa el sensor solar externo de la instalación si lo necesitas |
| SoH no está disponible | El modelo devolvió un valor no compatible o cero | Confirma la lectura en diagnósticos; cero se trata como no disponible |

??? "Detalles avanzados"
    Omnibattery limita las órdenes Anker a los límites de hardware en vivo y a una envolvente de integración de `3,500 W`. El objetivo mínimo de funcionamiento distinto de cero es `100 W`; los objetivos menores se cambian a reposo o a ese mínimo según corresponda.

    Los controles de SOC del dispositivo permiten un máximo de `80–100%` y un mínimo de `0–20%`. Los cortes de hardware permanecen activos. Anker no utiliza la reducción gradual por tensión de celda de Marstek.

    **Battery State of Health (SoH)** usa el registro de entrada `10015`. El mapa compartido lo pone a disposición de los modelos compatibles, pero solo se ha verificado en campo en Solarbank Max AC con código de producto `DMWH`. Un valor bruto de `0` se trata como no disponible en lugar de como 0% de salud.

    Los códigos de producto Solarbank 4 E5000 Pro exponen una fuente solar independiente. Los campos solares de Max AC y XE AC se obtienen del cálculo propio de CA de la batería y se excluyen del total solar de Omnibattery.

    En el panel, la sección **Health & cells** de la tarjeta de batería muestra temperatura interna y SoH cuando están disponibles. Las filas de tensión y celdas se omiten cuando el driver no tiene entidades correspondientes, por lo que las tarjetas Anker no muestran marcadores de posición vacíos.

    Para controles compartidos en tiempo de ejecución y límites del sistema, consulta [Elige la conexión de tu batería](index.md).
