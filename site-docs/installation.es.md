# Instala Omnibattery

Instala la integración, conecta una batería compatible y elige el sensor de Home Assistant que mide el intercambio de red de tu casa. Puedes terminar primero una configuración básica y añadir más tarde previsiones, horarios y carga basada en precios.

¿Ya usas **Marstek Venus Energy Manager**? Detente aquí y sigue [Actualizar desde Marstek VEM](upgrading-from-marstek-vem.md) para conservar tu configuración e historial.

Comprueba [si tu batería exacta es compatible](compatibility.md) antes de instalar.

## Antes de empezar {#before-you-start}

Necesitas:

- Home Assistant **2024.4.1** o posterior
- una [batería compatible](configuration/batteries/index.md) accesible desde Home Assistant
- un sensor de potencia de Home Assistant que mida la importación y exportación total de red en vatios o kilovatios

La batería o su puente deben ser accesibles mediante la conexión local correspondiente. El sensor de red es obligatorio porque Omnibattery lo usa para decidir cuánto deben cargar o descargar las baterías.

| Si tienes | Prepara esto antes de configurar | Conexión |
|---|---|---|
| **Marstek Venus E v2/v3, Venus A o Venus D** | Activa o añade la conexión descrita en la [guía de Marstek](configuration/batteries/marstek.md). | Modbus TCP, Modbus RTU mediante USB–RS-485 o un puente LilyGo RS-485/ESPHome para Venus E v2 |
| **Zendure SolarFlow 800, 800 Plus, 800 Pro, 1600 AC+, 2400 AC+, 2400 AC Pro, 3000 Mix AC+, 4000 Mix AC+ o 4000 Mix Pro** | Mantén desactivado el control **HEMS** del fabricante para que no sustituya la orden de potencia de Omnibattery. | API HTTP local |
| **Anker SOLIX Solarbank Max AC, Solarbank 4 E5000 Pro o Solarbank XE AC** | Activa **Third-Party Control** en la aplicación de Anker y desconecta cualquier otro cliente Modbus. | Modbus TCP |
| **Huawei SUN2000 + LUNA2000** | Prepara la conexión Modbus del inversor. El método de control predeterminado también necesita la integración Huawei Solar. | Modbus TCP a través del inversor o un proxy compartido |
| **Sessy Home Battery** | Ten disponibles las credenciales locales impresas en el dongle Sessy. | API HTTP local |
| **Hoymiles MS-A2 o HiBattery** | Configura la integración MQTT de Home Assistant y un broker MQTT local; después activa **MQTT Service** en S-Miles Home. | MQTT a través de Home Assistant |

!!! warning "Usa un sensor de red con respuesta rápida"
    Se recomienda una actualización cada **1–2 segundos**. Se aceptan sensores que publican cada **10 segundos o más**, pero Omnibattery muestra una advertencia de Reparaciones porque las lecturas retrasadas reducen la calidad del control. La última lectura sigue siendo válida hasta **65 segundos**.

    Consulta [Sensor principal](configuration/main-sensor.md) para las convenciones de signo, unidades y guía específica de medidores.

### Información opcional

Puedes dejar estos campos vacíos o desactivados durante la primera configuración y añadirlos después en **Ajustes → Dispositivos y servicios → Omnibattery → Configurar**:

- **Previsión solar restante hoy** para la carga predictiva y el retraso de carga solar
- **Sensor de producción solar** cuando un inversor externo mide paneles que no alimentan las propias entradas solares de la batería
- **Sensor de potencia aislada de la red** cuando un medidor independiente mide el circuito respaldado
- **Protección de corriente trifásica** cuando hay sensores de corriente por fase; consulta [Protección de corriente trifásica](configuration/three-phase.md)

Establece **Potencia máxima contratada** en el límite de importación real de tu casa. Omnibattery la usa como techo de seguridad de carga.

??? "Detalles de conexión por tipo de batería"
    **Marstek:** Venus E v2 necesita una conexión RS-485, como un adaptador USB o un convertidor RS-485 a TCP. Venus E v3, Venus A y Venus D pueden usar su conexión de red. La vía LilyGo necesita el firmware ESPHome compatible y sus entidades estándar de Home Assistant.

    **Zendure y Sessy:** Home Assistant debe poder alcanzar el punto final HTTP local del dispositivo. Omnibattery se comunica localmente, en lugar de mediante una cuenta en la nube del fabricante.

    **Anker:** su servidor Modbus acepta un cliente cada vez. Cierra otra integración o herramienta antes de que Omnibattery pruebe la conexión.

    **Huawei:** Omnibattery lee a través del inversor SUN2000. De forma predeterminada, las órdenes de control usan servicios Huawei Solar; las escrituras Modbus directas son una opción de configuración opcional.

    **Hoymiles:** Omnibattery usa el broker MQTT configurado en Home Assistant. No instala ni gestiona un broker.

## Instalar con HACS

Home Assistant Community Store (HACS) es el método de instalación recomendado.

1. Usa el botón siguiente para añadir el repositorio a HACS.

    [![Añadir Omnibattery a HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ffunes&repository=Omnibattery&category=integration)

2. En HACS, busca **Omnibattery** y selecciona **Descargar**.
3. Reinicia Home Assistant cuando HACS te lo pida.

## Instalar manualmente

1. Descarga el archivo zip de la [última versión de Omnibattery](https://github.com/ffunes/Omnibattery/releases).
2. Extrae la carpeta `omnibattery` en el directorio `custom_components` de Home Assistant.
3. Confirma que la ruta resultante es `custom_components/omnibattery/manifest.json`.
4. Reinicia Home Assistant.

## Añadir la integración

1. Abre **Ajustes → Dispositivos y servicios**.
2. Selecciona **Añadir integración** y busca **Omnibattery**.
3. Elige el sensor de red e introduce los ajustes eléctricos de tu instalación.
4. Añade cada batería y, después, termina o configura las secciones opcionales de franjas horarias, dispositivos excluidos y carga predictiva.

![Añadir Omnibattery desde el diálogo de integración de Home Assistant](assets/screenshots/installation/add-integration.png){ width="600" style="display: block; margin: 0 auto;" }

La [guía de configuración](configuration/index.md) explica cada página del asistente de configuración.

## Comprueba el resultado

Cuando termine el asistente:

- abre el panel lateral de Omnibattery y confirma que **Red**, **Casa** y **Batería** muestran valores de potencia plausibles
- abre **Baterías** y confirma que cada batería informa de su estado de carga (SOC) y potencia
- abre **Control** para activar solo las funciones opcionales que quieras

Si la batería no está disponible o sus valores tienen el signo equivocado, utiliza la [guía de configuración de batería](configuration/batteries/index.md) correspondiente y [Solución de problemas](troubleshooting.md) antes de activar el control automático.

## Instalación de blueprints {#blueprint-installation}

Los blueprints son opcionales y se instalan por separado de la integración. Omnibattery funciona sin ellos. Cuando la integración básica funcione correctamente, consulta [Blueprints](automations/blueprints.md) para los pasos de instalación manual y desde Home Assistant.
