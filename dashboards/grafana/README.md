# Dashboard unificado de consumo y acceso A2A

Un unico [dashboard importable](tokens-consumption.json) para Grafana con el datasource **Azure Monitor**,
consultando **Logs** en el recurso **Application Insights**, con las tablas
`customMetrics` y `requests`. No usa el servicio Metrics del datasource ni consulta
`AppMetrics` en un workspace.

## Secciones incluidas

| Seccion | Preguntas que responde |
| --- | --- |
| Resumen de consumo | Cuanto se consume, cuanto es entrada/salida, como evoluciona y cuantos agentes y entidades tienen metricas. |
| Acceso y rechazos A2A | Cuantas peticiones se observan, cuantas reciben HTTP429, su porcentaje y que agentes o subscriptions concentran esas respuestas. |
| Productos y subscriptions | Como se distribuyen tokens y peticiones por producto, subscription y modelo. |
| Reparto organizativo | Que agentes, departamentos y equipos concentran consumo y respuestas HTTP429. |
| Usuarios | Que usuarios concentran consumo o respuestas HTTP429, y a traves de que subscriptions. |
| Calidad de datos | Si las metricas estan completas, que peticiones tienen identidad y si hay sampling. |

Las seis secciones comparten un unico intervalo y conjunto de filtros, con las
excepciones de modelo y cobertura de identidad indicadas mas abajo.
Estan organizadas en filas desplegables y se abren expandidas.
No se repiten los KPI ni los graficos comunes a varias secciones.

### Paneles principales

- **Resumen**: tokens totales, entrada, salida, porcentaje de salida, entidades
  distintas exactas, series de entrada/salida y tabla diaria UTC.
- **Calidad de datos**: minutos desde la ultima metrica, diferencia
  `total - entrada - salida`, filas sin `valueSum`, valores negativos y tokens
  sin alguna dimension; cobertura de identidad, origen de la atribucion y sampling.
- **Acceso**: peticiones observadas, respuestas HTTP429, porcentaje HTTP429,
  otros fallos, evolucion de resultados y tablas por agente/subscription.
- **Productos**: comparativa de peticiones, respuestas HTTP429 y tokens,
  consumo por producto/modelo y desglose de entrada/salida.
- **Organizacion**: rankings de agentes y departamentos, tabla de equipos
  identificados por `department + team`, evolucion, matriz de atribucion
  e impacto de respuestas HTTP429.
- **Usuarios**: ranking y evolucion Top N, concentracion del consumo identificado,
  tokens sin usuario, detalle por contexto y rankings de respuestas HTTP429
  tanto absolutas como porcentuales, con volumen minimo configurable.

No se incluyen costes, presupuestos, cuota restante ni limites configurados.
HTTP429 y HTTP403 son respuestas observadas: no demuestran por si solas que
se haya ejecutado una politica concreta de throttling o cuota de APIM.

## Importacion

1. Configura un datasource **Azure Monitor** en Grafana y comprueba su conexion.
   La identidad del datasource necesita acceso de consulta a los logs de
   Application Insights. Un rol como **Monitoring Reader** sobre el recurso
   es un punto de partida; las restricciones de acceso al workspace asociado
   pueden requerir permisos adicionales. No incluyas claves en los JSON.
2. En **Dashboards > New > Import**, sube [tokens-consumption.json](tokens-consumption.json).
3. Selecciona tu datasource en el campo **Azure Monitor** del formulario de
   importacion.
4. Comprueba la variable **Application Insights resource ID** del dashboard.
   El valor inicial es el recurso facilitado para este proyecto:

   ```text
   /subscriptions/0c2b616d-05ea-49f8-b176-e2e06149c164/resourceGroups/finops-for-ai-rg/providers/Microsoft.Insights/components/finops4aiappsins
   ```

   Para otro entorno, sustituye ese valor por el ARM resource ID del componente
   Application Insights, no el instrumentation key, App ID ni workspace ID.
5. Elige un intervalo con datos. Inicialmente se consultan las ultimas 24 horas,
   con actualizacion cada 5 minutos y zona horaria UTC.
6. Selecciona servicio, agente, producto, subscription, departamento, equipo,
   usuario y modelo. Los filtros
   admiten seleccion multiple y **All**. Los valores disponibles dependen del
   rango temporal y de los filtros precedentes:

   ```text
   servicio -> agente -> producto -> subscriptionId -> departamento -> equipo -> usuario -> modelo
   ```

   Al ampliar un filtro anterior, revisa las selecciones de los posteriores.
   **Top N** admite 5, 10, 20 o 50 y afecta solo a rankings, series Top N y
   concentracion, no a los totales ni a las tablas de detalle.
   **Min. peticiones para ranking %** admite 1, 10, 50 o 100, con 10 por defecto,
   y se aplica a los rankings porcentuales de HTTP429.
   **Modelo (solo tokens)** no filtra peticiones: `requests` no contiene un modelo
   fiable para estas llamadas A2A. En la comparativa de productos, los tokens
   respetan el modelo seleccionado y las peticiones siguen incluyendo todos los modelos.
   Los controles de cobertura/sampling ignoran departamento, equipo, usuario y
   modelo para no ocultar peticiones que no se puedan atribuir.
7. Guarda el dashboard tras ajustar su configuracion.

El JSON usa el formato clasico de dashboard (`schemaVersion: 39`, referencia
Grafana 10.4+) y el plugin integrado Azure Monitor. No requieren plugins
adicionales. La importacion y ejecucion deben comprobarse en tu version de
Grafana: no se ha realizado una importacion contra tu instancia.
Se conserva el UID **`ai-tokens-consumption`**: si ya tienes la version anterior,
reimporta el archivo y confirma la sobrescritura de ese dashboard. Si conservas
los tres dashboards separados antiguos, puedes retirarlos tras comprobar esta
vista. El generador no modifica tu instancia.

## Semantica de las consultas

### No sumar dos veces los mismos tokens

Para la muestra facilitada:

| Metrica | Tokens |
| --- | ---: |
| Prompt Tokens | 138 |
| Completion Tokens | 120 |
| Total Tokens | 258 |

El consumo total es **258**, no `138 + 120 + 258 = 516`.

Los totales y repartos se calculan solo con `name == "Total Tokens"`.
Entrada y salida se agregan por separado y se presentan como desglose.
Si falta Total Tokens, no se reconstruye silenciosamente a partir de las otras
metricas: se debe investigar la calidad de la ingesta.

### Metricas preagregadas

Se suma **`valueSum`**, convertido a `real`, no `value`, `valueCount` ni el numero
de filas. Por ejemplo, una fila agregada con `valueCount = 5`, `value = 24` y
`valueSum = 120` aporta **120 tokens**, no 24 ni 600.

`valueSum` nulo no tiene fallback a otra columna: KQL lo excluye de la suma y
el panel **Calidad de las metricas** permite detectarlo. Los registros negativos
se muestran en ese control y no se corrigen u ocultan automaticamente.
En consultas de tokens, `count()` se utiliza para calidad de telemetria y
recuentos de entidades, nunca para estimar llamadas a partir de metricas.

### Dimensiones y huecos

Se extraen de `customDimensions` las claves exactas:

```text
Service ID, agent, product, subscriptionId, department, team, user, model
```

Las dimensiones vacias o con solo espacios se agrupan como `(sin dimension)`.
No se descartan sus tokens. Las columnas de tokens sin dimension pueden
solaparse: no hay que sumarlas entre si. Para auditar atribucion global,
selecciona **All** en todos los filtros.

Los recuentos de entidades excluyen `(sin dimension)`. Los equipos se cuentan
por departamento y equipo para evitar mezclar equipos homonimos.
La concentracion de usuarios excluye los no identificados tanto del numerador
como del denominador; el ranking puede mostrar `(sin dimension)` como categoria.

Las series utilizan el intervalo automatico de Grafana con minimo de un minuto.
Cada punto es **tokens en el bucket**, no tokens/segundo ni tokens/minuto.
No se rellenan huecos con ceros porque la ausencia de datos no demuestra
ausencia de consumo. Los Top N se eligen sobre todo el rango para mantener
las mismas entidades a lo largo de la serie; no incluyen el resto.

Las tablas de detalle estan limitadas a 1000 combinaciones, ordenadas por consumo
o por respuestas HTTP429 segun el panel. Si hay mas combinaciones, no representan
todo el ambito; los KPI de resumen siguen consultando todas las filas seleccionadas.

### Peticiones A2A y respuestas HTTP429/HTTP403

Solo se incluyen registros de `requests` con `customDimensions["API Type"] = "a2a"`
y `customDimensions["HTTP Method"] = "POST"`. Se excluyen descargas GET de la
Agent Card y peticiones a las APIs de modelos. Son peticiones HTTP, no
conversaciones ni tareas A2A unicas.

- **Peticiones**: `count()` de registros observados.
- **HTTP429 / HTTP403**: registros con ese `resultCode`.
- **Correctas**: `success`, convertido a booleano, excluyendo HTTP429/HTTP403.
- **OtrosFallos**: fallos distintos de HTTP429/HTTP403. El KPI general
  **Otros fallos A2A (sin HTTP429)** incluye tambien HTTP403.
- **Porcentaje HTTP429**: `100 * HTTP429 / Peticiones`, nulo si no hay peticiones.
  Tanto numerador como denominador son observados, sin ponderar por sampling.
- **Representadas**: suma de `itemCount` (1 si no se informa). Es el volumen
  representado por la telemetria, no un contador exacto del gateway.

El panel de sampling compara observadas y representadas y cuenta registros
con `itemCount > 1`. Si hay sampling, el porcentaje observado puede no coincidir
con el del trafico completo. No se corrige automaticamente.
Una respuesta HTTP429 puede proceder del gateway o de un backend; una HTTP403
puede tener causas de autorizacion ademas de cuotas. Para distinguir politicas
hace falta telemetria explicita del motivo del rechazo.

### Atribucion de peticiones sin multiplicar filas

Las dimensiones de `requests` se extraen asi:

| Dimension | Clave de `customDimensions` |
| --- | --- |
| service | `Service ID` |
| agent | `gen_ai.agent.id` |
| subscriptionId | `Subscription Name` |
| product | `Product Name` |
| department / team / user | `department` / `team` / `user`, si se emiten directamente |

En este entorno, `Subscription Name` coincide con `subscriptionId` de las
metricas y `Product Name` con `product`. No es una garantia de APIM: nombre
visible e ID pueden diferir. Si cambian, normaliza la emision o las consultas;
no se intenta adivinar equivalencias.

Para peticiones sin identidad completa se construye un mapa usando solamente
`Total Tokens` del intervalo seleccionado. Se deduplican los contextos
`department + team + user` y se agrega **una unica fila por
`service + agent + subscriptionId`**, antes de hacer un `leftouter` con requests.
Solo se completan dimensiones si existe exactamente un contexto completo y
compatible con cualquier identidad ya presente en la peticion.

- `directa`: identidad completa en la peticion; tiene prioridad.
- `inferida de metricas`: se completaron dimensiones desde un contexto unico.
- `conflicto`: la identidad parcial de la peticion contradice el mapa; no se rellena.
- `ambigua`: existen varios contextos; no se elige el ultimo ni uno arbitrario.
- `mapeo incompleto`: el unico contexto no contiene todas las dimensiones.
- `sin correspondencia`: no existe un mapa util, por ejemplo en una subscription
  que solo tiene rechazos y nunca ha emitido tokens.

El mapa no se restringe por filtros de identidad o modelo, para que una seleccion
no convierta artificialmente un contexto ambiguo en unico. Las peticiones no
emparejadas se conservan con sus dimensiones ausentes y las subscriptions que
solo tienen rechazos siguen disponibles en los filtros.

**La inferencia no es correlacion causal**: no demuestra quien hizo cada peticion
ni detecta cambios de titular sin metricas durante el intervalo. Cambiar el rango
temporal puede cambiar su resultado. Para atribucion fiable, emite identidad
directamente en cada registro de request desde APIM. Los paneles muestran
cuantas peticiones se infirieron o carecen de identidad completa.

La comparativa por producto agrega por separado tokens y peticiones antes de
un `fullouter`; no une filas crudas de ambas tablas. Los tokens quedan nulos
si no hay agregado de metricas para un producto con peticiones.

### Tiempo y completitud

Todas las consultas filtran `timestamp` con `$__timeFilter(timestamp)`.
Los dias se agrupan en UTC; los extremos del intervalo pueden ser dias parciales.
La edad de la ultima metrica se calcula respecto a **ahora**, por lo que sera
alta al consultar un periodo historico.

`Total - Entrada - Salida` deberia ser cero con ingesta completa y alineada.
Una diferencia puede deberse a metricas ausentes, retrasos o bordes temporales.
Un cero o un total de cero no demuestra por si solo que haya datos: comprueba
tambien frescura y calidad.

Los datos representan **consumo observado en la telemetria**, no una factura:
no se corrige por sampling, duplicados o perdida de ingesta. Se incluye el modelo
emitido, pero no se distinguen tokens cacheados, tarifas o reintentos si esas
dimensiones no se emiten.
Para costes reales haran falta, como minimo, modelo/despliegue, categorias de
tokens facturables, precios y su vigencia.

## Comprobacion inicial

En un panel nuevo, selecciona **Azure Monitor > Logs**, el mismo recurso
Application Insights y ejecuta:

```kusto
customMetrics
| where $__timeFilter(timestamp)
| where name in ("Prompt Tokens", "Completion Tokens", "Total Tokens")
| summarize
    Tokens = sum(valueSum),
    Filas = count(),
    SinSuma = countif(isnull(valueSum))
    by name
| order by name asc
```

Este ejemplo usa macros de Grafana. En el portal Azure, sustituye
`$__timeFilter(timestamp)` por un filtro temporal KQL, por ejemplo
`timestamp > ago(24h)`.

Si seleccionas un rango que contenga unicamente las tres filas de la muestra,
debe mostrar 138 de entrada, 120 de salida y 258 de total.

### Si no aparecen datos

- Verifica que el intervalo contiene la fecha real de las metricas.
- Comprueba el resource ID, datasource, permisos y **Query inspector**.
- Consulta el recurso Application Insights, no el workspace con esquema
  `AppMetrics`: los nombres de tabla y columnas no son intercambiables.
- Restablece los filtros a **All** para descartar selecciones incompatibles.
- Revisa que APIM emite los nombres exactos de metrica y las dimensiones.
- Los datos historicos sin `product`, `subscriptionId` o `model` quedan en
  `(sin dimension)`; no se pueden reconstruir automaticamente. Los rechazos
  sin metricas de su subscription tampoco permiten inferir usuario.
- No actives Basic Logs: estas consultas estan preparadas para Logs Analytics
  sobre Application Insights.

## Privacidad y operacion

La dimension `user` puede contener correos o identificadores personales.
Restringe el acceso a la carpeta y al datasource segun las capacidades de tu
edicion de Grafana. Ocultar un panel o una variable no protege los datos:
un usuario con acceso de consulta al datasource puede consultar los logs.
No publiques capturas o exports con usuarios reales.

Los filtros generan consultas de logs adicionales al cambiar el tiempo o las
selecciones. Para periodos largos o muchos usuarios, evita refrescos muy
frecuentes y usa filtros de servicio/organizacion. El refresco inicial es de
5 minutos para no consultar constantemente datos agregados por minuto.

## Mantenimiento y validacion

El JSON se genera de forma determinista desde [generate.py](generate.py),
sin dependencias adicionales:

```bash
python3 dashboards/grafana/generate.py
uv run pytest tests/test_grafana_dashboards.py -q
```

Edita el generador y regenera el archivo para mantener filtros, macros y
semantica consistentes. Las pruebas verifican reproducibilidad, estructura del
datasource, filtros, agregacion, joins, clasificacion de respuestas, limites
y disposicion. No sustituyen la
ejecucion de KQL ni la validacion visual en una instancia Grafana conectada.

Referencias:

- [Editor y macros Azure Monitor en Grafana](https://grafana.com/docs/grafana/latest/datasources/azure-monitor/query-editor/)
- [Variables del datasource Azure Monitor](https://grafana.com/docs/grafana/latest/datasources/azure-monitor/template-variables/)
