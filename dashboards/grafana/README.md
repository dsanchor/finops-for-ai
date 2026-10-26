# Dashboard unificado de consumo de tokens

Un unico [dashboard importable](tokens-consumption.json) para Grafana con el datasource **Azure Monitor**,
consultando **Logs** en el recurso **Application Insights** y su tabla
`customMetrics`. No usan el servicio Metrics del datasource ni consultan
`AppMetrics` en un workspace.

## Secciones incluidas

| Seccion | Preguntas que responde |
| --- | --- |
| Resumen de consumo | Cuanto se consume, cuanto es entrada/salida, como evoluciona y cuantos agentes y entidades tienen metricas. |
| Reparto organizativo | Que agentes, departamentos y equipos concentran consumo, como evoluciona y que combinaciones agente/departamento/equipo lo explican. |
| Usuarios | Que usuarios concentran consumo, que porcentaje representa el Top N y en que departamento, equipo y agente se produce. |
| Calidad de datos | Si las metricas son recientes, completas y estan correctamente atribuidas. |

Las cuatro secciones comparten un unico intervalo y conjunto de filtros.
Estan organizadas en filas desplegables y se abren expandidas.
No se repiten los KPI ni los graficos comunes a varias secciones.

### Paneles principales

- **Resumen**: tokens totales, entrada, salida, porcentaje de salida, entidades
  distintas exactas, series de entrada/salida y tabla diaria UTC.
- **Calidad de datos**: minutos desde la ultima metrica, diferencia
  `total - entrada - salida`, filas sin `valueSum`, valores negativos y tokens
  sin alguna dimension.
- **Organizacion**: rankings de agentes y departamentos, tabla de equipos
  identificados por `department + team`, evolucion y matriz de atribucion.
- **Usuarios**: ranking y evolucion Top N, concentracion del consumo identificado,
  tokens sin usuario y detalle por usuario/departamento/equipo/agente.

No se incluyen costes, presupuestos, cuotas, rate limits ni numero de peticiones.
La estructura permite anadirlos cuando exista telemetria que los sustente.

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
6. Selecciona servicio, agente, departamento, equipo y usuario. Los filtros
   admiten seleccion multiple y **All**. Los valores disponibles dependen del
   rango temporal y de los filtros precedentes:

   ```text
   servicio -> agente -> departamento -> equipo -> usuario
   ```

   Al ampliar un filtro anterior, revisa las selecciones de los posteriores.
   **Top N** admite 5, 10, 20 o 50 y afecta solo a rankings, series Top N y
   concentracion, no a los totales ni a las tablas de detalle.
7. Guarda el dashboard tras ajustar su configuracion.

El JSON usa el formato clasico de dashboard (`schemaVersion: 39`, referencia
Grafana 10.4+) y el plugin integrado Azure Monitor. No requieren plugins
adicionales. La importacion y ejecucion deben comprobarse en tu version de
Grafana: no se ha realizado una importacion contra tu instancia.
Este archivo sustituye a los tres dashboards separados anteriores. Si ya los
importaste, puedes retirarlos de Grafana una vez comprobada la vista unificada;
el generador no modifica tu instancia.

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
`count()` solo se utiliza para calidad de telemetria y recuentos de entidades,
nunca como numero de llamadas.

### Dimensiones y huecos

Se extraen de `customDimensions` las claves exactas:

```text
Service ID, agent, department, team, user
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

Las tablas de detalle estan limitadas a las 1000 combinaciones de mayor consumo.
Si hay mas combinaciones, no representan todo el consumo; los KPI de resumen
siguen consultando todas las filas del ambito seleccionado.

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
no se corrige por sampling, duplicados o perdida de ingesta. Tampoco distinguen
tokens cacheados, modelos, tarifas o reintentos si esas dimensiones no se emiten.
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
datasource, filtros, agregacion, limites y disposicion. No sustituyen la
ejecucion de KQL ni la validacion visual en una instancia Grafana conectada.

Referencias:

- [Editor y macros Azure Monitor en Grafana](https://grafana.com/docs/grafana/latest/datasources/azure-monitor/query-editor/)
- [Variables del datasource Azure Monitor](https://grafana.com/docs/grafana/latest/datasources/azure-monitor/template-variables/)
