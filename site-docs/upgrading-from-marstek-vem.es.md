# Actualizar desde Marstek Venus Energy Manager

Pasa de la antigua integración `marstek_venus_energy_manager` a Omnibattery sin reconstruir tus ajustes, paneles ni automatizaciones. La migración conserva los ID de entidad existentes para que el historial de Home Assistant siga conectado.

## ¿Esta guía es para mí?

**Úsala si** Home Assistant tiene actualmente, o tuvo antes, una entrada de integración de **Marstek Venus Energy Manager**.

**No la necesitas si** esta es tu primera instalación. Sigue la [guía de instalación](installation.md) en su lugar.

## Antes de empezar

- Crea una copia de seguridad completa de Home Assistant en **Ajustes → Sistema → Copias de seguridad**.
- Actualiza Marstek Venus Energy Manager a **v2.0.6** mientras todavía esté instalado.
- Reinicia Home Assistant y espera a que cargue la integración antigua. Esto escribe la copia de recuperación que se usa si después se elimina la entrada de configuración.

!!! important "Conserva la entrada de configuración antigua"
    No elimines **Marstek Venus Energy Manager** de **Ajustes → Dispositivos y servicios** antes de ejecutar la migración. Omnibattery puede migrar directamente una entrada antigua activa. La copia de recuperación es un método alternativo si esa entrada ya se ha eliminado.

## Ejecutar la actualización

1. Añade el repositorio de Omnibattery a Home Assistant Community Store (HACS), descarga **Omnibattery** y reinicia Home Assistant.
2. Abre **Ajustes → Dispositivos y servicios**, selecciona **Añadir integración** y busca **Omnibattery**.
3. En **Migrar desde Marstek Venus**, confirma la migración.
4. Espera el mensaje de éxito y recarga la página del navegador. Haz una recarga completa si no aparece el panel lateral de Omnibattery.

El flujo crea por sí mismo las entradas de Omnibattery. No debería aparecer un formulario de instalación normal cuando Home Assistant todavía tiene una entrada antigua que migrar.

## Qué se conserva

| Elemento | Resultado tras la migración |
|---|---|
| Conexiones de batería y ajustes de integración | Se copian a la entrada de configuración de Omnibattery |
| Ajuste de control, franjas horarias, límites y otras opciones | Se copian sin cambios |
| ID de entidad e ID únicos | Se conservan para que los paneles, automatizaciones y plantillas sigan haciendo referencia a las mismas entidades |
| Historial del registrador y estadísticas a largo plazo | Siguen vinculados a los ID de entidad sin cambios |
| Energía diaria, acumuladores e historial de integración | Se copian a las nuevas claves de almacenamiento de la integración |
| Nombres de entidad y asignaciones de área | Se conservan en el registro de entidades de Home Assistant |

Los ID de entidad existentes pueden seguir empezando por `marstek_venus_`. Es lo esperado y protege el historial del registrador y las referencias existentes.

## Comprueba el resultado

1. Abre **Ajustes → Dispositivos y servicios → Omnibattery** y confirma que aparece cada configuración anterior.
2. Abre el panel lateral de Omnibattery y comprueba la potencia de red y batería, y el estado de carga en directo.
3. Comprueba un panel o automatización existente que use una entidad `marstek_venus_*`.
4. Revisa **Ajustes → Sistema → Reparaciones** y el registro de Home Assistant antes de descartar la copia de seguridad previa a la actualización.

## Si la integración antigua ya se eliminó

Inicia **Añadir integración → Omnibattery**. Si existe la copia de recuperación y no hay entradas antiguas ni nuevas activas, Omnibattery muestra **Restaurar configuración anterior**. Deja activada **Restaurar configuración anterior** y envía el formulario.

Esta vía de recuperación recrea las entradas de configuración a partir de los datos de conexión y las opciones guardadas. También vuelve a conectar el historial de entidades y copia el almacenamiento persistente de la integración al espacio de nombres de la nueva entrada.

Si no aparece ni la pantalla de migración ni la de restauración, la entrada de configuración antigua y su copia de recuperación no están disponibles. Restaura la copia de seguridad completa de Home Assistant que hiciste antes de actualizar, actualiza la integración antigua a v2.0.6, reiníciala y vuelve a empezar.

## Si no funciona

| Síntoma | Causa probable | Qué comprobar |
|---|---|---|
| Se abre la configuración normal de Omnibattery | No se encontró una entrada antigua activa ni una copia de recuperación utilizable | Restaura la copia de seguridad de Home Assistant y confirma que carga la integración antigua antes de cambiar |
| Aparece **Restaurar configuración anterior** en lugar de **Migrar desde Marstek Venus** | Se eliminó la entrada de configuración antigua, pero sobrevivió su copia de recuperación | Acepta la restauración; es el método alternativo previsto |
| La migración tiene éxito pero una batería no está disponible | Su dirección de red guardada, puente o credenciales ya no son accesibles | Abre el dispositivo de batería, comprueba su conexión y usa la [guía de batería](configuration/batteries/index.md) correspondiente |
| El panel lateral todavía muestra el panel antiguo o no muestra ninguno | Los archivos del frontend del navegador están en caché | Haz una recarga completa en el navegador o borra la caché del frontend de Home Assistant |
| Un ID de entidad sigue empezando por `marstek_venus_` | La migración lo ha conservado deliberadamente | Déjalo sin cambios salvo que estés dispuesto a actualizar todas las referencias externas |

??? "Detalles avanzados"
    Omnibattery busca primero las entradas de configuración que pertenecen al dominio antiguo `marstek_venus_energy_manager`. Descarga cada entrada antigua, crea su sustituta de Omnibattery con los mismos datos y opciones, redirige las entidades del registro a la nueva plataforma, copia los archivos de almacenamiento de la integración y después carga la nueva entrada.

    Si se eliminó la entrada antigua, el método alternativo lee un almacén de recuperación independiente del dominio escrito por la integración anterior. Recrea las entradas bajo el dominio `omnibattery` y copia los archivos de almacenamiento desde el espacio de nombres de la entrada guardada.

    La base de datos del registrador no se reescribe. Home Assistant sigue encontrando su historial y estadísticas a largo plazo porque la migración conserva cada ID de entidad e ID único.

    Más adelante puedes usar la acción **Recrear ID de entidad** de Home Assistant si quieres que nuevas entidades del sistema usen el prefijo `omnibattery_*`. Ese cambio de nombre requiere actualizar automatizaciones, plantillas, entradas del panel de Energía y cualquier consumidor externo que use los ID antiguos.
