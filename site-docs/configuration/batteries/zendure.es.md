# Zendure SolarFlow

Omnibattery se conecta a los equipos Zendure SolarFlow mediante su API HTTP
local. El asistente consulta el dispositivo y detecta el modelo
automáticamente; no tienes que seleccionarlo manualmente.

!!! warning "Desactiva HEMS"
    Mantén **HEMS desactivado** en la aplicación de Zendure mientras Omnibattery controle el equipo. HEMS sobrescribe las consignas manuales de potencia después de unos segundos.

## Conexión

Introduce un nombre descriptivo, la IP local del equipo y su puerto HTTP. El
puerto predeterminado es `80`. El dispositivo debe ser accesible desde Home
Assistant en la red local o mediante enrutamiento.

| Campo | Descripción | Por defecto |
|---|---|---|
| **Nombre** | Nombre usado para el dispositivo de batería | — |
| **IP del host** | IP local del SolarFlow | — |
| **Puerto HTTP** | Puerto de la API local | `80` |

La prueba de conexión lee `/properties/report`, verifica el equipo y prepara los
límites de potencia del modelo detectado.

## Modelos compatibles y límites de potencia

| Modelo | Carga máxima en CA | Descarga máxima en CA |
|---|---:|---:|
| SolarFlow 800 / 800 Plus / 800 Pro | `1000 W` | `800 W` |
| SolarFlow 1600 AC+ | `1600 W` | `1600 W` |
| SolarFlow 2400 AC Pro / 2400 AC+ | `2400 W` | `2400 W` |
| SolarFlow 4000 Mix AC+ | `4000 W` | `4000 W` |
| SolarFlow 4000 Mix Pro | `4000 W` | `4000 W` |

El informe del dispositivo tiene prioridad si anuncia un límite inferior. Los
El 4000 Mix Pro expone telemetría MPPT dual de CC; los modelos acoplados en CA
1600 AC+, 2400 AC+ y 4000 Mix AC+ no exponen telemetría MPPT de CC a través de
esta conexión.

Las entradas existentes de 2400 AC+ se promocionan automáticamente cuando el
equipo informa del identificador de producto 4000 Mix AC+ o 4000 Mix Pro. Se
conservan los límites de potencia elegidos por el usuario; si quieres usar el
margen mayor, auméntalos en las opciones de la batería.

## Ajustes específicos de Zendure

La página de límites incluye potencia de carga/descarga, SOC máximo, SOC mínimo,
histéresis de carga y umbral de backup offgrid. Zendure usa un rango de SOC
mínimo de 5–50 % y no utiliza la reducción de carga por tensión de Marstek.

La capacidad nominal es opcional. Introdúcela si quieres que Omnibattery
calcule la energía almacenada y la eficiencia a partir del SOC; Zendure no
ofrece un contador de capacidad nominal en su informe.

### Control manual

Zendure no tiene entidades nativas de modo forzado ni de consigna de
carga/descarga en esta API. Por eso Omnibattery ofrece los controles de
software `Modo forzado`, `Potencia de carga` y `Potencia de descarga`. Activa
primero el switch **Control Manual de Batería** antes de usarlos; el controlador
reaplica una consigna de software distinta de reposo en cada ciclo mientras el
switch está activado. Mantén HEMS desactivado o la aplicación de Zendure puede
sobrescribir la orden.

Para los controles comunes en tiempo de ejecución y los límites del sistema,
consulta la [configuración de baterías](index.md).
