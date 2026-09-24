# Hoymiles MS-A2

Esta guía lleva un MS-A2 instalado desde la puesta en servicio de S-Miles Home al control local a través de Home Assistant y Omnibattery. La batería se comunica a través del broker MQTT ya configurado en Home Assistant.

## ¿Lo necesito?

**Utilice esta guía si** tiene un Hoymiles MS-A2 cuyo firmware muestra **Servicio MQTT**. Para los modelos HiBattery, utilice la [guía de batería Hoymiles MQTT](batteries/hoymiles.md) compartida y el manual eléctrico del producto.

Un MS-A2 tiene una capacidad nominal de `2.24 kWh` y un techo de carga/descarga de `1,000 W`. Un sistema de dos unidades compatible puede proporcionar `4.48 kWh` y hasta `2,000 W`, sujeto al sobre MQTT publicado por el dispositivo.

## Antes de empezar

Necesitas:

- un MS-A2 puesto en servicio en **S-Miles Home**;
- firmware que expone **Servicio MQTT**;
- Home Assistant con integración **MQTT** conectada y corredor local;
- Omnibatería instalada con sensor de potencia de red;
- el ID completo del dispositivo MS-A2 MQTT.

!!! warning "Instalación eléctrica"
Siga la [guía de instalación oficial de MS-A2](https://www.hoymiles.com/statics/5/hoymiles/picture/User-Manual_MS_A2_Global_EN_REV1.4.pdf)] actual, las etiquetas del producto y las normas eléctricas locales. Aísle la batería y el equipo solar antes de cambiar las conexiones de CA.

## como agregarlo

1. **Pon en servicio la batería.** Inspecciona la unidad, el cable y los conectores; instálelo en posición vertical con los espacios libres y la protección contra la intemperie requeridos por Hoymiles. Con el equipo aislado, conecte el sistema microinversor y el cable AC como se muestra en la guía oficial, conecte el tomacorriente Schuko homologado y luego encienda el MS-A2. Únase a la red Wi-Fi compatible en **S-Miles Home**, confirme el estado de carga y la actualización de energía e instale las actualizaciones de firmware ofrecidas.
2. **Prepare Home Assistant MQTT.** Abra **Configuración → Dispositivos y servicios**, confirme que **MQTT** esté conectado y cree un usuario intermediario dedicado para la batería cuando su intermediario admita usuarios.
3. **Conecte la batería al corredor.** En **S-Miles Home → Servicio MQTT**, ingrese la dirección local, el puerto, el nombre de usuario y la contraseña del corredor. Guarde la configuración y registre la ID completa del dispositivo, incluido su prefijo `MSA-`.
4. **Agrega la batería.** Abre **Configuración → Dispositivos y servicios → Agregar integración → Omnibattery**, elige **Hoymiles MQTT**, ingresa el ID del dispositivo y deja **Modelo de batería** en **Detección automática**.
5. **Revisar y verificar.** Mantenga la capacidad detectada y el límite de energía seguro, complete los límites comunes y confirme el estado de carga, la energía de la batería, el voltaje, la temperatura y la actualización diaria de energía.

## lo que veras

La carga es energía positiva de la batería en Omnibattery, la descarga es negativa y la inactividad es cero. El control automático y los controles manuales del software utilizan la misma convención; active el **Control manual de batería** antes de probar un objetivo manual.

Después de la configuración, verifique un comando de carga, descarga e inactividad de baja potencia. El estado de carga y la energía de la batería deben seguir S-Miles Home, y la telemetría MQTT debe continuar actualizándose a través del corredor local.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| El asistente informa **No se puede conectar** | Home Assistant MQTT está desconectado, el servicio MQTT está desactivado o la identificación está incompleta | Confirme la integración, el servicio y la identificación completa del dispositivo |
| La aplicación funciona pero Omnibattery no recibe datos | Los datos del corredor en S-Miles Home son incorrectos o no se puede acceder a ellos desde la batería | Verifique la dirección del corredor, el puerto, las credenciales y el enrutamiento Wi-Fi |
| La batería vuelve al control autónomo | MQTT se desconectó el tiempo suficiente para que expire el control externo | Verifique los reinicios del broker y la estabilidad de la conexión |
| Un sistema emparejado sigue estando limitado a 1.000 W | Omnibattery no ha leído el sobre retenido emparejado | Empareje ambas unidades, luego reconfigure la batería |
| Las entidades detalladas se actualizan más lentamente que el poder | Hoymiles publica esos temas con menos frecuencia | Confirman que sus marcas de tiempo aún avanzan |
| Se rechaza el control | El firmware es antiguo o el ID pertenece a la unidad incorrecta | Actualice el firmware y utilice el ID del dispositivo maestro o independiente |

??? "Detalles avanzados"
El MS-A2 publica el estado rápido en `homeassistant/sensor/<device_id>/quick/state`; por ejemplo, `homeassistant/sensor/MSA-280024341346/quick/state`. Omnibattery se suscribe a través de Home Assistant, por lo que no es necesario crear sensores o automatizaciones MQTT. Los temas de voltaje, temperatura y energía diaria normalmente se actualizan con menos frecuencia que el tema de estado rápido.

Mantenga al corredor en la red local confiable y cree un usuario de batería dedicado siempre que sea posible.

El controlador selecciona `mqtt_ctrl` y actualiza el objetivo exacto cada `30 s`. Un reintento de actualización fallido después de `5 s`. El control externo caduca si se detienen las actualizaciones de comandos, por lo que la estabilidad del intermediario es importante. Cuando Omnibattery se descarga, envía `0 W` y restaura el modo `general`.

El firmware `01.06.03` puede anunciar una envolvente asimétrica `−1,000…+2,000 W` para una unidad aunque su límite de hardware sea simétrico en `1,000 W`. Omnibattery utiliza la magnitud del lado de la carga para mantener simétrica una sola unidad. Un dispositivo emparejado que publica `−2,000…+2,000 W` conserva ese rango.

El protocolo MQTT no expone límites de SOC grabables ni voltajes de celda individuales. Omnibattery impone límites de estado de carga en el software; Las funciones de equilibrio de celda y reducción de voltaje no están disponibles. Los valores predeterminados de configuración son SOC máximo `100%`, SOC mínimo `10%` e histéresis de carga `2%`.

Comience la verificación a baja potencia y deténgase si la dirección medida no coincide con la dirección de carga o descarga solicitada.

Mantenga al corredor en la red local o detrás de una VPN segura. No exponga un oyente MQTT no cifrado directamente a Internet.

Referencias oficiales:

    - [Página del producto Hoymiles MS-A2](https://www.hoymiles.com/products/micro-storage.html)
    - [Guía de instalación y usuario de MS-A2](https://www.hoymiles.com/statics/5/hoymiles/picture/User-Manual_MS_A2_Global_EN_REV1.4.pdf)
    - [Guía protocolo Hoymiles MQTT](https://www.hoymiles.com/uploadfile/1/202511/9350aa1077.txt)
