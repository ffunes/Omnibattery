# Monitor de equilibrio de celdas

Registra la diferencia de tensión entre la celda más alta y la más baja en la parte final de una carga completa. Esa lectura se usa para ver si el pack mantiene las celdas equilibradas con el tiempo y para generar avisos cuando el desbalanceo es alto.

## Por qué es necesario en baterías LFP

Las baterías Marstek Venus usan celdas LFP. La química LFP es muy estable y duradera, pero tiene una curva de tensión muy plana durante casi todo el rango útil de SOC. En la zona media de carga, dos celdas pueden tener un SOC distinto y aun así mostrar tensiones muy parecidas. Por eso una lectura de tensión a medio SOC no sirve bien para medir el equilibrio real.

La zona útil para medir y balancear está cerca del final de carga. A partir de unos 3.45 V por celda, la curva de tensión LFP sube mucho más deprisa y las diferencias entre celdas se hacen visibles. También es la zona en la que el BMS debería hacer balanceo pasivo, descargando ligeramente las celdas más altas.

En la práctica, el BMS de Marstek no siempre balancea bien las celdas por sí solo. Si el pack llega al 100 % rápido y vuelve enseguida al uso normal, una celda puede quedar repetidamente más alta que las demás. Por eso la integración hace dos cosas:

- ralentiza la parte final de la carga al 100 % para dar tiempo al BMS a trabajar en la ventana de balanceo;
- mide el desbalanceo siempre en un punto de tensión alto y repetible, en lugar de usar lecturas ruidosas a medio SOC.

## La curva de carga LFP en detalle

La química LFP (LiFePO4) tiene una curva de carga/descarga radicalmente distinta de la del Li-ion NMC o NCA. Entenderla es lo que justifica cada uno de los umbrales de tensión que usa esta integración.

Una celda LFP típica de 3,2 V nominales se comporta así durante una carga a corriente constante:

| Rango de SOC | Rango de tensión de celda | Pendiente |
|---|---|---|
| 0 – 10 % | 2,50 V → 3,20 V | Rodilla de entrada muy pronunciada |
| 10 – 90 % | 3,20 V → 3,30 V | Casi plana — alrededor de 1 mV por % de SOC |
| 90 – 97 % | 3,30 V → 3,45 V | Empieza una subida suave |
| 97 – 99 % | 3,45 V → 3,55 V | Rodilla — la tensión empieza a subir con fuerza |
| 99 – 100 % | 3,55 V → 3,65 V | Rodilla superior abrupta — el "acantilado" del final de carga |

Esa larga meseta plana es la razón por la que, en mitad de la curva, la tensión LFP apenas dice nada sobre el estado de carga. Dos celdas que parecen idénticas a 3,28 V pueden tener en realidad un 5 – 10 % de diferencia de SOC entre ellas, lo cual es enorme.

La meseta también significa que **el BMS no puede hacer un balanceo pasivo útil en mitad de la curva**. El balanceo pasivo funciona drenando corriente de la celda más alta a través de una resistencia. Para poder decidir cuál es la celda "más alta", el BMS necesita que la diferencia entre celdas se eleve por encima del ruido de medida. En la meseta todas las celdas leen prácticamente lo mismo, así que el BMS no tiene nada con lo que actuar.

Solo cuando el pack entra en la rodilla superior (por encima de unos 3,45 V) las tensiones de celda se separan lo suficiente para que el BMS identifique a la celda líder. Una diferencia de 10 mV en la meseta puede corresponder a un 5 % de diferencia de SOC, pero los mismos 10 mV por encima de 3,50 V representan un delta de SOC minúsculo — que es justo lo que interesa al final de carga.

Por eso el balanceo en LFP solo es eficaz en una ventana estrecha: aproximadamente el último 1 – 3 % de carga, por encima de 3,45 V. Fuera de esa ventana el BMS es prácticamente ciego al desbalanceo, y todo el tiempo que el pack pasa por debajo de la rodilla es tiempo durante el que las celdas *no* se están balanceando.

## Disponibilidad

El monitor de equilibrio de celdas está siempre activo. No hay una opción de configuración separada porque las lecturas son datos útiles de salud de la batería y por sí solas no cambian el funcionamiento normal.

Hay un control integrado que decide cuándo se lleva la batería a la ventana de medición en tensión alta:

- **Reducción por voltaje al cargar al 100 %**: opción por batería. Cuando el objetivo de carga es 100 %, la integración ralentiza la carga final y registra una lectura de balance en tensión alta.

La carga semanal completa puede fijar temporalmente el SOC máximo de la batería al 100 %. Cuando lo hace, se usan exactamente las mismas reglas de reducción por voltaje al 100 %.

Las Venus A/D con packs acoplados son la excepción a la parada en tensión alta:
también reducen la carga a 200 W desde 3,48 V, pero llegar a 3,60 V no detiene
la integración ni inicia la medición de 60 segundos. La carga reducida continúa
hasta que la BMS confirma su corte, evitando que el primer pack lleno deje sin
terminar los demás packs acoplados.

Otros packs Venus E/v2/v3 también pueden ser cortados por su BMS justo por
debajo de 3,60 V. Cuando ese corte se confirma mientras la batería sigue en la
zona de reducción, el propio corte activa la medición: la integración detiene
la carga, espera 60 segundos y registra el delta de celdas estabilizado.

Para recuperar activamente un pack con desbalanceo persistente, usa el [blueprint de balanceo activo para una batería Marstek](../blueprints.es.md#balanceo-activo-de-una-batería-marstek). Es una automatización externa de Home Assistant: toma una batería mediante **Battery Manual Mode**, descubre sus entidades estándar a partir del dispositivo seleccionado y deja la propiedad manual activada si no puede confirmar la limpieza.

## Reducción por voltaje al 100 %

Esta ruta se usa siempre que la opción **Reducción por voltaje al 100 %** está activada para una batería. Se basa en tensión: se activa en cuanto `max_cell_voltage` alcanza los umbrales de abajo, sin importar el `max_soc` configurado. En la práctica ocurre cuando:

- el usuario ha configurado esa batería con `max_soc = 100`, o
- la carga semanal completa ha elevado temporalmente esa batería al 100 %, o
- un `max_soc` alto por debajo del 100 % deja igualmente que las celdas lleguen a 3.48 V.

La carga semanal completa no usa un perfil de balanceo distinto. Solo cambia el objetivo de SOC a 100 %; los voltajes, la potencia y la medición son los mismos.

### Perfil de carga

| Condición para una batería | Acción |
|---|---:|
| `max_cell_voltage` por debajo de 3.48 V | Límite de carga configurado normal |
| `max_cell_voltage` igual o superior a 3.48 V | Limita la carga a 200 W |
| Corte de BMS confirmado por debajo de 3.60 V en la zona de reducción | Detiene la carga, espera 60 s sin cargar y registra el delta |
| `max_cell_voltage` llega a 3.60 V en Venus E | La histéresis de carga configurada toma el control del umbral de parada y reanudación |
| `max_cell_voltage` llega a 3.60 V en Venus A/D | Mantiene 200 W hasta el corte de la BMS; no aplica la parada de la integración |
| Tras la espera de 60 s en Venus E | Registra `delta_mV = (Vmax - Vmin) * 1000` |
| Tras confirmar el corte de la BMS en Venus A/D | Espera 60 s sin cargar y registra `delta_mV = (Vmax - Vmin) * 1000` |

El inicio de la reducción se basa en tensión de celda: el SOC no se usa para decidir cuándo empieza, porque cerca del final de carga los registros de tensión de celda son más fiables que el SOC reportado.

En Venus E, cuando la batería llega a 3.60 V, la histéresis de carga
configurada evita que vuelva a cargar hasta cruzar su umbral de SOC. Si la BMS
corta antes de 3,60 V, el mismo diagnóstico de 60 segundos empieza después
del corte confirmado. La medición sigue siendo de mejor esfuerzo; si la carga
semanal termina antes, se deja completar la medición posterior al corte antes
de usar una captura alternativa. Las Venus A/D omiten esta pausa y medición
antes del corte de la BMS; una vez confirmado el corte final, esperan 60
segundos sin cargar y registran una medición del delta de celdas.

En sistemas con varias baterías, la lógica se evalúa por batería. Una batería puede estar limitada o pausada mientras otra sigue cargando con normalidad.

### Recalibración de SOC con tensión alta atascada (Venus E)

Algunos packs Venus E llegan al punto de pausa de 3.60 V mientras la BMS sigue reportando un SOC muy por debajo del total (por ejemplo 60–70 %). Esa diferencia puede indicar que el contador de coulombs de la BMS se ha desviado, pero alcanzar el umbral de tensión no demuestra que el SOC reportado sea incorrecto.

Cuando esto ocurre, quedarse en 3.60 V no permite que el BMS termine su propia secuencia superior de carga. Por eso, en vez de pausar, la integración sigue cargando a la potencia reducida de 200 W hasta que el propio BMS corta, *intentando* que recalibre el SOC.

Es un intento de mejor esfuerzo, no una solución garantizada. Que un corte en la parte alta de la curva realmente reinicie el SOC reportado depende del firmware del BMS: algunos packs saltan al 100 % con un corte por sobretensión, otros no. La integración solo crea las condiciones para una recalibración — no puede obligar al BMS a aplicarla.

El override se activa automáticamente cuando se cumple **todo** lo siguiente:

- la reducción por voltaje al 100 % está activa (`max_cell_voltage` en la zona alta), y
- `max_cell_voltage` ha alcanzado el punto de pausa de 3.60 V, y
- el BMS sigue reportando un SOC por debajo del 99 %.

Es autolimitado:

- la carga continúa solo a 200 W (la potencia suave de reducción), no a plena potencia;
- el corte del BMS se detecta cuando la potencia de la batería cae a ≤ 10 W y el inversor reporta Standby durante 5 ciclos consecutivos (~10 s). Si ese primer corte ocurrió por encima de 3.60 V y el SOC aún es menor del 100 %, la batería queda en espera hasta relajarse a 3.57 V y se hace un único reintento a 200 W; cuando la BMS vuelve a cortar, el override se enclava definitivamente;
- si el SOC alcanza el 100 % durante la espera o el reintento, no se realiza otro intento;
- si el SOC marca 99 % o más antes del primer corte, la condición inicial ya no se cumple, así que el override no se dispara;
- el enclavamiento solo se rearma cuando la batería sale de la zona alta (`max_cell_voltage` por debajo de 3.48 V), para que una carga completa posterior pueda recalibrar de nuevo si hace falta.

Llegar al punto de pausa de 3.60 V normalmente solo ocurre en una carga al 100 %, así que esto rara vez afecta al ciclado diario con un `max_soc` más bajo. **No** se ejecuta durante la [carga semanal completa](weekly-full-charge.md) — allí la pausa de 3.60 V se suprime por completo y el corte del BMS por sí solo finaliza el ciclo (ver esa página). En Venus A/D se usa siempre el flujo propiedad de la BMS en lugar de este reintento de recalibración. El blueprint opcional toma la batería mediante Battery Manual Mode, por lo que el controlador normal la excluye de forma natural mientras está activo.

!!! note "Desbalance de celdas"
    El override no comprueba primero la dispersión entre celdas. En un pack muy desbalanceado, la celda más alta puede llegar al corte por sobretensión del BMS antes de que el pack esté lleno, así que la recalibración es correcta pero el balanceo queda para ciclos posteriores. El BMS sigue protegiendo cada celda de forma individual.

## Blueprint opcional de balanceo activo

El [blueprint de balanceo activo para una batería Marstek](../blueprints.es.md#balanceo-activo-de-una-batería-marstek) es la ruta recomendada para recuperar un pack cuando el balanceo pasivo de las cargas normales o semanales no basta. Está deliberadamente fuera del bucle de control automático de la integración y debe configurarse una vez por batería. Al crear la automatización se selecciona el dispositivo Omnibattery de la batería; el blueprint resuelve automáticamente las entidades estándar y permite sobrescribir por ID las que se hayan renombrado.

Su perfil predeterminado es: potencia máxima configurada hasta `max_cell_voltage >= 3.49 V`, carga regulada a 95 W hasta 3.60 V, reposo de 60 s para medir, descargas a 200 W hacia 3.49 V hasta que `delta_V <= 0.03 V` y una descarga final a 200 W hasta 3.48 V. Si el BMS rechaza un tramo nuevo de carga, el blueprint espera 10 s y exige tres muestras aproximadamente a 0 W. Cuando el rechazo ocurre todavía en la ventana superior, primero reposa 60 s y publica el delta estabilizado; después baja el objetivo de reintento en 0.01 V, hasta 3.40 V, y continúa con la descarga adaptativa. Los rechazos por debajo de la ventana superior no se registran como medidas formales.

La automatización valida las entidades resueltas y las relaciones de tensión/potencia antes de escribir. Fija ambos setpoints a 0 W antes de cambiar el modo forzado, escribe temporalmente un SOC máximo del 100 % y lleva toda cancelación, reinicio o error a la misma limpieza. Restaura el SOC máximo configurado y apaga Battery Manual Mode solo después de confirmar el reposo y el SOC; si no, el interruptor permanece activado como retención de seguridad.

## Por qué estos umbrales de tensión

Todos los cortes de tensión usados por la reducción al 100 % y por el blueprint opcional de balanceo activo se eligen contra la curva LFP descrita arriba. Ninguno de estos números es arbitrario.

| Umbral | Dónde se usa | Por qué este valor |
|---|---|---|
| **3,45 V** | Referencia para el inicio de la rodilla superior | Es aproximadamente donde la curva LFP abandona la meseta. Por debajo no se puede confiar en las decisiones de balanceo, porque las tensiones de las celdas están demasiado juntas para distinguirlas. |
| **3,48 V** | Disparador para reducir la carga normal a 200 W | Un poco por encima de la rodilla. El pequeño margen confirma que el pack está realmente en la ventana de balanceo — y no en un rebote de tensión transitorio causado por un escalón de carga — antes de bajar la potencia. |
| **3,49 V** | Suelo de descarga del blueprint entre reintentos; cambio de carga "rápida" a carga regulada | Está justo dentro de la ventana de balanceo. Parar la descarga aquí mantiene el pack en la zona donde el BMS aún puede ver y drenar la celda alta. Bajar más sacaría al pack de la rodilla y desperdiciaría el tiempo ya invertido en balancear. |
| **3,60 V** | Punto de medida superior; se para la carga y se esperan 60 s antes de leer el delta | Permite que el firmware compatible alcance su comportamiento nativo de final de carga, manteniendo unos 50 mV de margen nominal respecto al techo LFP habitual de 3,65 V. El BMS de la batería conserva el corte final y puede parar antes. |
| **3,48 V (otra vez)** | Suelo de descarga al final del ciclo — la descarga final a 200 W del blueprint se detiene aquí | El mismo umbral usado para entrar en la reducción se reutiliza para salir de la ventana de balanceo. Parar a 3,48 V deja al pack justo por debajo del comienzo de la rodilla superior sin devolverlo del todo a la meseta profunda. Quedarse a 3,55 – 3,60 V durante mucho tiempo acelera el envejecimiento calendario, así que la automatización baja deliberadamente al borde inferior de la ventana antes de soltar el control. |
| **3,40 V** | Límite inferior del voltaje de reintento del blueprint cuando se detecta rechazo de carga | La automatización concede 10 s para que arranque cada nuevo tramo de carga y, si aún no ha observado potencia de carga, exige después 3 ciclos consecutivos a ~0 W antes de declarar rechazo. Entonces baja el voltaje de reintento en 0,01 V, pero nunca por debajo de 3,40 V. Bajar más saldría completamente de la ventana de balanceo y obligaría a volver a subir toda la curva, lo que es una pérdida de tiempo. |
| **0,03 V (30 mV)** | Umbral de finalización del blueprint | Se considera "suficientemente equilibrado" para un pack LFP en la parte alta de la rodilla. Forzar valores más estrictos (10 mV o menos) rara vez compensa, porque las corrientes de balanceo pasivo son minúsculas — ver la sección siguiente. |
| **0,05 V (50 mV)** | Frontera verde / amarillo | Un pack por debajo de 50 mV en la parte alta se considera sano. Es más estricto que las especificaciones típicas de fabricantes LFP (80 – 100 mV) porque la medida se toma en la ventana de balanceo, donde las diferencias entre celdas están exageradas. |

La reducción normal usa 200 W para mantener la tensión suficientemente excitada y avanzar por la zona superior sin volver a plena potencia. El blueprint opcional usa una carga más suave de 95 W. Las mediciones siempre se toman en **reposo**, 60 segundos después de detener carga y descarga, por lo que ninguna de las dos potencias contamina el delta registrado.

## Por qué tarda tanto

El balanceo de celdas **no** es un proceso rápido — y los packs Marstek Venus no son una excepción. Hay dos razones.

**1. La corriente de balanceo pasivo es muy pequeña.** Un BMS LFP típico drena la celda más alta a través de una resistencia con una corriente de entre 30 mA y 150 mA. Los packs Marstek Venus se mueven por la parte baja de ese rango. Para una celda de 100 Ah, un drenaje de 50 mA quita solo unos 0,05 % de SOC por hora a la celda alta. Por eso igualar diferencias incluso pequeñas entre celdas requiere muchas horas seguidas dentro de la ventana de balanceo.

**2. La ventana de balanceo es estrecha.** El BMS solo puede drenar cuando el pack está por encima de ~3,45 V *y* la celda más alta destaca de forma detectable sobre el resto. En cuanto se para la carga o el pack vuelve a bajar de la rodilla, el balanceo se detiene. Un ciclo de carga normal que llega al 100 % y vuelve enseguida a descargar pasa solo unos minutos en la ventana útil — muy poco para que tenga efecto visible.

La consecuencia práctica es:

> **Reducir el delta de celdas en lo alto de carga unos 5 mV requiere típicamente alrededor de 24 horas de tiempo acumulado en la parte alta de la ventana de balanceo.**

Esa cifra es coherente tanto con el cálculo de corrientes de drenaje de arriba como con lo observado en packs Venus reales. Desbalanceos mayores (50 mV o más) pueden necesitar **varios días** de sesiones repetidas de balanceo arriba antes de que el delta empiece a bajar de forma consistente. Packs que han estado crónicamente desbalanceados durante meses pueden tardar una semana o más en recuperarse.

Esa es también la razón por la que el blueprint de balanceo activo no tiene una "vía rápida":

- el límite de 95 W de carga por encima de 3,48 V está pensado para mantener al pack en la rodilla el tiempo suficiente para que el BMS avance, en lugar de atravesarla en segundos;
- los 200 W de descarga entre reintentos bajan el pack de vuelta al voltaje de reintento sin salir de la ventana;
- la automatización puede ejecutarse indefinidamente, porque cualquier duración por debajo de "muchas horas" difícilmente moverá el delta.

Si el objetivo es recuperar un pack visiblemente desbalanceado, importa el blueprint, crea una automatización para esa batería y **déjala funcionando toda la noche (o más tiempo) antes de mirar el resultado**. Mirar el delta de celdas en tiempo real esperando movimientos en cuestión de minutos solo lleva a frustración.

## Cómo se mide el desbalanceo

La única lectura que alimenta el estado de balance, los avisos y la tendencia es la medición explícita en la ventana superior:

1. la batería entra en la zona de reducción con `max_cell_voltage >= 3.48 V`;
2. llega a `max_cell_voltage >= 3.60 V`, o la BMS confirma el corte mientras la celda sigue por debajo de ese punto;
3. se detiene la carga;
4. la integración espera 60 segundos;
5. registra la diferencia entre `max_cell_voltage` y `min_cell_voltage`.

Las antiguas lecturas tipo OCV, las lecturas oportunistas y las retenciones pasivas largas ya no se usan. Medir tras un evento estabilizado de tensión alta o de corte BMS mantiene comparables las lecturas y permite trabajar con packs cuyo BMS corta justo por debajo de 3,60 V.

## Umbrales

| Estado | Rango de delta | Significado |
|---|---|---|
| Verde | < 50 mV | Buen equilibrio |
| Amarillo | 50-99 mV | Desbalanceo leve; monitorizar con el tiempo |
| Naranja | 100-149 mV | Desbalanceo moderado |
| Rojo | >= 150 mV | Desbalanceo alto |

Los umbrales son fijos y se aplican por igual a todos los packs LFP compatibles.

## Notificaciones

La integración envía notificaciones persistentes de Home Assistant en estos casos:

| Evento | Título de la notificación |
|---|---|
| Lectura naranja o roja en tensión alta | Desbalanceo de celdas - `{nombre de la batería}` |
| Rojo en 2 o más cargas completas consecutivas | Posible celda degradada - `{nombre de la batería}` |
| Tendencia creciente con media por encima de 75 mV | Tendencia de desbalanceo creciente - `{nombre de la batería}` |

## Entidades de sensor

Cuando la función está activada se crean cinco entidades de sensor por batería:

| Entidad | Descripción | Unidad |
|---|---|---|
| `sensor.*_cell_delta` | Diferencia de tensión entre la celda máxima y mínima | mV |
| `sensor.*_balance_status` | Resultado del equilibrio: `green` / `yellow` / `orange` / `red` | - |
| `sensor.*_delta_trend` | Tendencia en las lecturas recientes: `rising` / `stable` / `falling` | - |
| `sensor.*_last_balance_read` | Marca de tiempo de la última lectura | timestamp |
| `sensor.*_delta_avg_4w` | Media móvil de las últimas 4 lecturas | mV |

Los valores se restauran desde el almacenamiento persistente tras un reinicio de Home Assistant, de modo que los sensores muestran el último estado conocido al arrancar.

## Diagnóstico

El sensor **Integration Status** expone un atributo `normal_balance_protection` con detalles por batería:

| Atributo | Significado |
|---|---|
| `enabled` | Si la reducción por voltaje al 100 % está activada para esa batería |
| `in_zone` | Si `max_cell_voltage` está en la ventana de balanceo superior |
| `paused` | Si la carga está parada por tensión alta de celda |
| `pause_latched_soc` | SOC al que se enclavó la pausa; la carga sigue parada hasta que el SOC baja el margen de reanudación por debajo de este valor (vacío si no está enclavada) |
| `max_cell_voltage` / `min_cell_voltage` | Tensiones máxima y mínima actuales |
| `delta_V` | Diferencia actual de tensión en voltios |
| `voltage_taper_latched` | Si la reducción normal a 200 W está activa |
| `bms_cutoff_charge_active` | Si Venus A/D sigue disponible para cargar hasta el corte de la BMS |
| `bms_cutoff_measurement` | Estado de la medición posterior a un corte confirmado de la BMS: `pending` o `done` |
| `soc_recal_active` | Si la carga se mantiene más allá de la pausa de 3.60 V para intentar recalibrar un SOC reportado bajo |
| `soc_recal_bms_cutoff` | Si se ha alcanzado el corte del BMS durante la recalibración (override enclavado) |
| `soc_recal_retry_pending` | Si se está esperando a que la celda se relaje a 3.57 V para el único reintento |
| `soc_recal_retry_active` | Si el único reintento a 200 W está en curso |
| `soc_recal_first_cutoff_voltage` | Tensión máxima observada durante el primer corte de BMS |
| `charge_limit_w` | Límite efectivo de carga por batería antes del reparto |

La fase, el voltaje de reintento y el resultado de limpieza del blueprint se informan en sus notificaciones persistentes; no son atributos del estado de la integración. Cada medida estable tras el reposo también se registra en el histórico existente de `Cell Delta` con `source: blueprint`, usando la telemetría del propio coordinador de la integración.

!!! info
    Los registros de tensión de celda (`max_cell_voltage`, `min_cell_voltage`) se leen en todas las versiones de batería compatibles (v2, v3, vA, vD).
