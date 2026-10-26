"""Generate a unified Grafana dashboard for AI consumption and A2A access telemetry."""

import json
from pathlib import Path

DATASOURCE = {"type": "grafana-azure-monitor-datasource", "uid": "${DS_AZURE_MONITOR}"}
RESOURCE_ID = (
    "/subscriptions/0c2b616d-05ea-49f8-b176-e2e06149c164"
    "/resourceGroups/finops-for-ai-rg/providers/Microsoft.Insights/components/finops4aiappsins"
)
BASIC_DIMENSIONS = ("service", "agent", "product", "subscriptionId")
IDENTITY_DIMENSIONS = ("department", "team", "user")
REQUEST_DIMENSIONS = BASIC_DIMENSIONS + IDENTITY_DIMENSIONS
DIMENSIONS = REQUEST_DIMENSIONS + ("model",)
METRICS = ("Prompt Tokens", "Completion Tokens", "Total Tokens")
MISSING = "(sin dimension)"
BASE = """customMetrics
| where $__timeFilter(timestamp)
| where name in ("Prompt Tokens", "Completion Tokens", "Total Tokens")
| extend
    service = tostring(customDimensions["Service ID"]),
    agent = tostring(customDimensions["agent"]),
    product = tostring(customDimensions["product"]),
    subscriptionId = tostring(customDimensions["subscriptionId"]),
    department = tostring(customDimensions["department"]),
    team = tostring(customDimensions["team"]),
    user = tostring(customDimensions["user"]),
    model = tostring(customDimensions["model"]),
    Tokens = todouble(valueSum)
| extend
""" + ",\n".join(
    f'    {dimension} = iff(isempty(trim(@"\\s+", {dimension})), '
    f'"{MISSING}", trim(@"\\s+", {dimension}))'
    for dimension in DIMENSIONS
)


def base(dimensions=DIMENSIONS):
    return filtered(BASE, dimensions)


def filtered(source, dimensions):
    return source + "".join(
        f"\n| where $__contains({dimension}, ${dimension})" for dimension in dimensions
    )


def total_base():
    return base() + '\n| where name == "Total Tokens"'


def raw_requests():
    fields = {
        "service": "Service ID",
        "agent": "gen_ai.agent.id",
        "product": "Product Name",
        "subscriptionId": "Subscription Name",
        "department": "department",
        "team": "team",
        "user": "user",
    }
    return (
        "requests\n| where $__timeFilter(timestamp)"
        '\n| where tostring(customDimensions["API Type"]) =~ "a2a"'
        '\n| where tostring(customDimensions["HTTP Method"]) =~ "POST"'
        "\n| extend\n"
        + ",\n".join(
            f'    {name} = tostring(customDimensions["{key}"])' for name, key in fields.items()
        )
        + ",\n    Code = tostring(resultCode),"
        "\n    Successful = coalesce(tobool(success), false),"
        "\n    Represented = coalesce(tolong(itemCount), long(1))"
        "\n| extend\n"
        + ",\n".join(
            f'    {dimension} = iff(isempty(trim(@"\\s+", {dimension})), '
            f'"{MISSING}", trim(@"\\s+", {dimension}))'
            for dimension in REQUEST_DIMENSIONS
        )
    )


def request_context():
    keys = "service, agent, subscriptionId"
    valid_keys = " and ".join(
        f'{dimension} != "{MISSING}"' for dimension in ("service", "agent", "subscriptionId")
    )
    direct_complete = " and ".join(f'{d} != "{MISSING}"' for d in IDENTITY_DIMENSIONS)
    mapping_complete = " and ".join(f'mapped_{d} != "{MISSING}"' for d in IDENTITY_DIMENSIONS)
    conflict = " or ".join(
        f'({d} != "{MISSING}" and {d} != mapped_{d})' for d in IDENTITY_DIMENSIONS
    )
    return (
        "let IdentityContexts = "
        + base(())
        + '\n| where name == "Total Tokens"'
        + f"\n| where {valid_keys}"
        + f"\n| summarize by {keys}, department, team, user"
        + "\n| summarize MappingContexts = count(), "
        + ", ".join(f"mapped_{d} = take_any({d})" for d in IDENTITY_DIMENSIONS)
        + f" by {keys};"
        + "\nlet AgentRequests = "
        + raw_requests()
        + f"\n| join kind=leftouter IdentityContexts on {keys}"
        + f"\n| extend DirectComplete = {direct_complete},"
        + f"\n    MappingComplete = MappingContexts == 1 and {mapping_complete},"
        + f"\n    IdentityConflict = MappingContexts == 1 and ({conflict})"
        + "\n| extend CanInfer = coalesce(MappingComplete and not(IdentityConflict), false)"
        + "\n| extend Attribution = case("
        + '\n    DirectComplete, "directa",'
        + '\n    IdentityConflict, "conflicto",'
        + '\n    CanInfer, "inferida de metricas",'
        + '\n    MappingContexts > 1, "ambigua",'
        + '\n    MappingContexts == 1, "mapeo incompleto",'
        + '\n    "sin correspondencia")'
        + "\n| extend "
        + ",\n    ".join(
            f'{d} = iff({d} == "{MISSING}" and CanInfer, mapped_{d}, {d})'
            for d in IDENTITY_DIMENSIONS
        )
        + "\n| project timestamp, service, agent, product, subscriptionId, department, team, user,"
        + " Code, Successful, Represented, Attribution;"
        + "\n"
    )


def request_base(dimensions=REQUEST_DIMENSIONS):
    return request_context() + filtered("AgentRequests", dimensions)


def request_summary(groups=()):
    group_by = f"\n    by {', '.join(groups)}" if groups else ""
    return (
        "\n| summarize Peticiones = count(),"
        '\n    HTTP429 = countif(Code == "429"),'
        '\n    HTTP403 = countif(Code == "403"),'
        '\n    Correctas = countif(Successful and Code !in ("429", "403")),'
        '\n    OtrosFallos = countif(not(Successful) and Code !in ("429", "403")),'
        '\n    Inferidas = countif(Attribution == "inferida de metricas"),'
        "\n    SinIdentidad = countif("
        + " or ".join(f'{d} == "{MISSING}"' for d in IDENTITY_DIMENSIONS)
        + "),"
        "\n    Representadas = sum(Represented)"
        + group_by
        + "\n| extend Porcentaje = iff(Peticiones > 0, 100.0 * HTTP429 / Peticiones, real(null))"
    )


def dimension_query(dimension, preceding):
    metrics = base(preceding) + '\n| where name == "Total Tokens"' + f"\n| project {dimension}"
    if dimension == "model":
        query = metrics
    elif dimension in BASIC_DIMENSIONS:
        query = (
            "union ("
            + metrics
            + "),\n("
            + filtered(raw_requests(), preceding)
            + f"\n| project {dimension})"
        )
    else:
        query = (
            request_context()
            + "union ("
            + metrics
            + "),\n("
            + filtered("AgentRequests", preceding)
            + f"\n| project {dimension})"
        )
    return query + f"\n| distinct {dimension}\n| order by {dimension} asc"


def target(query, result_format="table"):
    return {
        "refId": "A",
        "datasource": DATASOURCE,
        "queryType": "Azure Log Analytics",
        "azureLogAnalytics": {
            "query": query,
            "resources": ["${appInsightsResourceId}"],
            "resultFormat": result_format,
            "dashboardTime": False,
            "timeColumn": "timestamp",
        },
    }


def variables():
    result = [
        {
            "name": "appInsightsResourceId",
            "label": "Application Insights resource ID",
            "type": "textbox",
            "query": RESOURCE_ID,
            "current": {"text": RESOURCE_ID, "value": RESOURCE_ID},
            "options": [{"text": RESOURCE_ID, "value": RESOURCE_ID, "selected": True}],
            "hide": 0,
        }
    ]
    labels = (
        "Servicio APIM",
        "Agente",
        "Producto",
        "Subscription ID",
        "Departamento",
        "Equipo",
        "Usuario",
        "Modelo (solo tokens)",
    )
    for index, (dimension, label) in enumerate(zip(DIMENSIONS, labels, strict=True)):
        query = dimension_query(dimension, DIMENSIONS[:index])
        result.append(
            {
                "name": dimension,
                "label": label,
                "type": "query",
                "datasource": DATASOURCE,
                "query": target(query),
                "definition": query,
                "refresh": 2,
                "sort": 1,
                "multi": True,
                "includeAll": True,
                "allValue": "all",
                "current": {"text": "All", "value": "$__all"},
                "options": [],
            }
        )
    result.append(
        {
            "name": "topN",
            "label": "Top N",
            "type": "custom",
            "query": "5,10,20,50",
            "current": {"text": "10", "value": "10"},
            "options": [
                {"text": str(n), "value": str(n), "selected": n == 10} for n in (5, 10, 20, 50)
            ],
            "multi": False,
            "includeAll": False,
        }
    )
    result.append(
        {
            "name": "minRequests",
            "label": "Min. peticiones para ranking %",
            "type": "custom",
            "query": "1,10,50,100",
            "current": {"text": "10", "value": "10"},
            "options": [
                {"text": str(n), "value": str(n), "selected": n == 10} for n in (1, 10, 50, 100)
            ],
            "multi": False,
            "includeAll": False,
        }
    )
    return result


def panel(title, kind, query, x, y, w, h, description, unit="short"):
    result = {
        "title": title,
        "type": kind,
        "description": description,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": DATASOURCE,
        "targets": [target(query, "time_series" if kind == "timeseries" else "table")],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": 1 if unit == "percent" else 0,
                "noValue": "Sin datos",
                "color": {"mode": "palette-classic"},
                "custom": {},
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "Porcentaje"},
                    "properties": [
                        {"id": "unit", "value": "percent"},
                        {"id": "decimals", "value": 1},
                    ],
                }
            ],
        },
        "options": {},
    }
    if kind == "stat":
        result["options"] = {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "none",
            "textMode": "auto",
        }
    elif kind == "timeseries":
        result["interval"] = "1m"
        result["fieldConfig"]["defaults"]["custom"] = {
            "drawStyle": "line",
            "lineWidth": 2,
            "fillOpacity": 10,
            "showPoints": "never",
            "spanNulls": False,
        }
        result["options"] = {
            "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["sum"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        }
    elif kind == "barchart":
        result["options"] = {
            "orientation": "horizontal",
            "showValue": "auto",
            "legend": {"showLegend": False},
            "tooltip": {"mode": "single"},
            "xTickLabelRotation": 0,
        }
    elif kind == "table":
        result["options"] = {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}
    return result


def note(content):
    return {
        "title": "Alcance y lectura",
        "type": "text",
        "gridPos": {"x": 0, "y": 0, "w": 24, "h": 3},
        "options": {"mode": "markdown", "content": content},
    }


def token_stat(metric, title, x):
    return panel(
        title,
        "stat",
        base() + f'\n| where name == "{metric}"\n| summarize Tokens = sum(Tokens)',
        x,
        3,
        6,
        4,
        f"Suma de valueSum para {metric} en el intervalo y filtros seleccionados.",
    )


def top_bar(dimension, title, x, y, width=12):
    return panel(
        title,
        "barchart",
        total_base()
        + f"\n| summarize Tokens = sum(Tokens) by {dimension}\n| top $topN by Tokens desc",
        x,
        y,
        width,
        8,
        "Top N por Total Tokens. Excluye del grafico las categorias fuera del Top N; "
        "no representa por si solo el total seleccionado.",
    )


def top_trend(dimension, title, x, y, width=12):
    query = (
        "let Selected = materialize("
        + total_base()
        + ");\nlet Leaders = Selected"
        + f"\n| summarize Tokens = sum(Tokens) by {dimension}"
        + f"\n| top $topN by Tokens desc\n| project {dimension};"
        + f"\nSelected\n| where {dimension} in (Leaders)"
        + f"\n| summarize Tokens = sum(Tokens) by bin(timestamp, $__interval), {dimension}"
        + "\n| order by timestamp asc"
    )
    return panel(
        title,
        "timeseries",
        query,
        x,
        y,
        width,
        8,
        "Top N calculado sobre todo el intervalo, no por cada bucket. "
        "Los huecos no se rellenan: ausencia de telemetria no implica consumo cero.",
    )


def breakdown(groups, title, y):
    query = (
        base()
        + "\n| summarize"
        + '\n    Total = sumif(Tokens, name == "Total Tokens"),'
        + '\n    Entrada = sumif(Tokens, name == "Prompt Tokens"),'
        + '\n    Salida = sumif(Tokens, name == "Completion Tokens")'
        + f"\n    by {', '.join(groups)}"
        + "\n| extend Porcentaje = iff(Total > 0, 100.0 * Salida / Total, real(null))"
        + "\n| top 1000 by Total desc"
    )
    return panel(
        title,
        "table",
        query,
        0,
        y,
        24,
        9,
        "Total, entrada y salida se agregan por separado; no se suman entre si. "
        "Porcentaje = salida / total. Maximo 1000 filas, ordenadas por total; "
        "no sumar filas para reconstruir el total si se supera ese limite.",
    )


def request_table(groups, title, y, order="HTTP429"):
    return panel(
        title,
        "table",
        request_base() + request_summary(groups) + f"\n| top 1000 by {order} desc",
        0,
        y,
        24,
        9,
        "Peticiones y HTTP429 son filas observadas; Representadas pondera itemCount "
        "(estimacion si hay sampling). Porcentaje = HTTP429 / Peticiones. "
        "HTTP403 no equivale a cuota confirmada. Inferidas identifica peticiones enriquecidas "
        "desde metricas; SinIdentidad tiene alguna dimension de identidad ausente. "
        "Modelo no filtra peticiones. Maximo 1000 filas.",
    )


def access_panels():
    panels = [
        note(
            "**Peticiones A2A POST, no llamadas al modelo.** HTTP429 es un rechazo observado, "
            "no prueba por si solo su origen en APIM; HTTP403 no confirma cuota agotada. "
            "**Modelo solo filtra tokens.** Identidades sin correspondencia permanecen visibles "
            "con All; la atribucion desde metricas es inferida para el rango seleccionado."
        )
    ]
    for index, (title, expression, unit) in enumerate(
        (
            ("Peticiones A2A observadas", "Peticiones", "short"),
            ("Respuestas HTTP429", "HTTP429", "short"),
            ("Porcentaje HTTP429 observado", "Porcentaje", "percent"),
            ("Otros fallos A2A (sin HTTP429)", "HTTP403 + OtrosFallos", "short"),
        )
    ):
        panels.append(
            panel(
                title,
                "stat",
                request_base() + request_summary() + f"\n| project Valor = {expression}",
                index * 6,
                3,
                6,
                4,
                "Calculado desde requests A2A POST, aplicando filtros excepto Modelo. "
                "Recuento observado, no extrapolado por sampling. "
                "Un porcentaje sin peticiones se muestra como Sin datos.",
                unit,
            )
        )
    panels.extend(
        [
            panel(
                "Evolucion de respuestas A2A",
                "timeseries",
                request_base()
                + '\n| extend Resultado = case(Code == "429", "HTTP429",'
                + ' Code == "403", "HTTP403 (causa sin confirmar)",'
                + ' Successful, "Correctas", "Otros fallos")'
                + "\n| summarize Peticiones = count() by bin(timestamp, $__interval), Resultado"
                + "\n| order by timestamp asc",
                0,
                7,
                12,
                8,
                "Resultados mutuamente excluyentes; incluye respuestas parcialmente enviadas "
                "como otros fallos si success=false. Modelo no filtra esta serie.",
            ),
            panel(
                "Porcentaje HTTP429 por agente - Top N",
                "barchart",
                request_base()
                + request_summary(("agent",))
                + "\n| where Peticiones >= $minRequests"
                + "\n| top $topN by Porcentaje desc\n| project agent, Porcentaje",
                12,
                7,
                12,
                8,
                "Solo agentes con al menos Min. peticiones. "
                "El porcentaje es sobre todas sus peticiones observadas, no solo los fallos.",
                "percent",
            ),
            request_table(("service", "agent"), "Acceso a agentes: volumen y resultados", 15),
        ]
    )
    return panels


def product_comparison():
    return (
        request_context()
        + "let RequestsByProduct = "
        + filtered("AgentRequests", REQUEST_DIMENSIONS)
        + request_summary(("service", "product"))
        + ";\nlet TokensByProduct = "
        + base()
        + '\n| summarize Total = sumif(Tokens, name == "Total Tokens"),'
        + ' Entrada = sumif(Tokens, name == "Prompt Tokens"),'
        + ' Salida = sumif(Tokens, name == "Completion Tokens")'
        + " by service, product;"
        + "\nRequestsByProduct\n| join kind=fullouter TokensByProduct on service, product"
        + "\n| project service = coalesce(service, service1),"
        + "\n    product = coalesce(product, product1),"
        + "\n    Peticiones = coalesce(Peticiones, long(0)),"
        + " HTTP429 = coalesce(HTTP429, long(0)), HTTP403 = coalesce(HTTP403, long(0)),"
        + "\n    Porcentaje, Representadas, Inferidas, SinIdentidad, Entrada, Salida, Total"
        + "\n| order by Peticiones desc"
    )


def product_panels():
    return [
        note(
            "**Consumo y acceso por producto/subscription.** Cada fuente se agrega antes "
            "de cruzarla; no se multiplican peticiones por filas de tokens. "
            "Tokens ausentes quedan como Sin datos, no cero. El filtro Modelo solo afecta "
            "al consumo. Product Name de requests debe coincidir con product de customMetrics."
        ),
        top_bar("product", "Consumo por producto - Top N", 0, 3),
        top_bar("model", "Consumo por modelo - Top N", 12, 3),
        panel(
            "Comparativa de productos: peticiones, HTTP429 y tokens",
            "table",
            product_comparison(),
            0,
            11,
            24,
            9,
            "Join fullouter de agregados unicos por servicio y producto. "
            "Mantiene productos sin tokens o sin peticiones. Porcentaje = HTTP429 / Peticiones. "
            "Modelo filtra solo Entrada/Salida/Total, nunca el denominador de peticiones. "
            "Sin correspondencia de identidad no se inventa un usuario.",
        ),
        breakdown(("service", "product", "model"), "Consumo por producto y modelo", 20),
        request_table(
            ("service", "agent", "product", "subscriptionId"),
            "Subscriptions: peticiones y respuestas HTTP429",
            29,
        ),
    ]


def overview_panels():
    panels = [
        note(
            "**Consumo observado, no facturacion.** Total = `Total Tokens`; "
            "entrada/salida se muestran por separado. Modelo solo filtra consumo; los demas "
            "filtros son comunes a todas las secciones. "
            "Comprueba frescura y calidad antes de interpretar un cero como ausencia de consumo."
        ),
        token_stat("Total Tokens", "Tokens totales", 0),
        token_stat("Prompt Tokens", "Tokens de entrada", 6),
        token_stat("Completion Tokens", "Tokens de salida", 12),
        panel(
            "Salida / total",
            "stat",
            base()
            + '\n| summarize Total = sumif(Tokens, name == "Total Tokens"),'
            + ' Salida = sumif(Tokens, name == "Completion Tokens")'
            + "\n| project Porcentaje = iff(Total > 0, 100.0 * Salida / Total, real(null))",
            18,
            3,
            6,
            4,
            "Proporcion de tokens de salida; no es proporcion del coste. "
            "Sin total, sin porcentaje.",
            "percent",
        ),
    ]
    for x, (group, title) in enumerate(
        (
            ("agent", "Agentes con metricas"),
            ("department", "Departamentos con metricas"),
            ("department, team", "Equipos con metricas"),
            ("user", "Usuarios con metricas"),
        )
    ):
        valid = " and ".join(f'{g} != "{MISSING}"' for g in group.split(", "))
        panels.append(
            panel(
                title,
                "stat",
                total_base()
                + f"\n| where {valid}\n| summarize by {group}\n| summarize Entidades = count()",
                x * 6,
                7,
                6,
                4,
                "Recuento exacto de entidades con metricas Total Tokens. Excluye dimensiones "
                "ausentes. Los equipos se identifican por departamento + equipo.",
            )
        )
    panels.extend(
        [
            panel(
                "Evolucion de entrada y salida",
                "timeseries",
                base()
                + '\n| where name in ("Prompt Tokens", "Completion Tokens")'
                + "\n| summarize Tokens = sum(Tokens) by bin(timestamp, $__interval), name"
                + "\n| order by timestamp asc",
                0,
                11,
                12,
                8,
                "Tokens por intervalo, no una tasa por segundo. Total Tokens no se agrega "
                "como tercera serie para evitar doble contabilizacion.",
            ),
            panel(
                "Consumo diario UTC",
                "table",
                base()
                + '\n| summarize Total = sumif(Tokens, name == "Total Tokens"),'
                + ' Entrada = sumif(Tokens, name == "Prompt Tokens"),'
                + ' Salida = sumif(Tokens, name == "Completion Tokens")'
                + "\n    by Dia = startofday(timestamp)\n| order by Dia asc",
                12,
                11,
                12,
                8,
                "Dias UTC dentro del intervalo seleccionado; "
                "primer y ultimo dia pueden ser parciales.",
            ),
        ]
    )
    return panels


def quality_panels():
    return [
        panel(
            "Minutos desde la ultima metrica",
            "stat",
            base()
            + "\n| summarize Ultima = max(timestamp)"
            + "\n| project Minutos = datetime_diff('minute', now(), Ultima)",
            0,
            27,
            6,
            5,
            "Edad respecto a ahora de la ultima metrica dentro del rango y filtros. "
            "En rangos historicos sera alta. Sin metricas: Sin datos.",
            "m",
        ),
        panel(
            "Diferencia: total - entrada - salida",
            "stat",
            base()
            + '\n| summarize Total = sumif(Tokens, name == "Total Tokens"),'
            + ' Entrada = sumif(Tokens, name == "Prompt Tokens"),'
            + ' Salida = sumif(Tokens, name == "Completion Tokens")'
            + "\n| project Diferencia = Total - Entrada - Salida",
            6,
            27,
            6,
            5,
            "Esperado: cero si las tres metricas estan completas y alineadas. "
            "Un valor distinto puede indicar ingesta parcial, bordes temporales o telemetria "
            "inconsistente; no demuestra por si solo un error de facturacion.",
        ),
        panel(
            "Calidad de las metricas",
            "table",
            base()
            + "\n| summarize Registros = count(),"
            + " SinSuma = countif(isnull(Tokens)), Negativos = countif(Tokens < 0)"
            + " by Metrica = name\n| order by Metrica asc",
            12,
            27,
            12,
            5,
            "Registros son filas de telemetria, NO peticiones. SinSuma y Negativos deben "
            "ser cero. Las sumas no incluyen valueSum nulo.",
        ),
        panel(
            "Tokens sin atribucion completa",
            "table",
            total_base()
            + "\n| summarize Total = sum(Tokens), "
            + ", ".join(
                f'Sin_{d} = sumif(Tokens, {d} == "{MISSING}")' for d in DIMENSIONS if d != "service"
            ),
            0,
            32,
            24,
            5,
            "Un mismo token puede carecer de varias dimensiones: las columnas Sin_* "
            "se solapan y NO deben sumarse. Los filtros pueden excluir registros incompletos; "
            "selecciona All para una auditoria global.",
        ),
        panel(
            "Peticiones con identidad completa - cobertura",
            "stat",
            request_base(BASIC_DIMENSIONS)
            + "\n| summarize Total = count(), Identificadas = countif("
            + " and ".join(f'{d} != "{MISSING}"' for d in IDENTITY_DIMENSIONS)
            + ")"
            + "\n| project Porcentaje = iff(Total > 0, 100.0 * Identificadas / Total, real(null))",
            0,
            37,
            6,
            5,
            "Cobertura directa o inferida antes de filtros de departamento, equipo, usuario "
            "y modelo. Solo aplica servicio/agente/producto/subscription. "
            "No demuestra correlacion individual ni estabilidad historica de la identidad.",
            "percent",
        ),
        panel(
            "Peticiones sin identidad completa",
            "stat",
            request_base(BASIC_DIMENSIONS)
            + "\n| summarize Peticiones = countif("
            + " or ".join(f'{d} == "{MISSING}"' for d in IDENTITY_DIMENSIONS)
            + ")",
            6,
            37,
            6,
            5,
            "Incluye mapas ausentes, incompletos, ambiguos y conflictos sin identidad completa. "
            "Se calcula antes de filtros de identidad/modelo para hacer visible lo no atribuible.",
        ),
        panel(
            "Sampling de requests",
            "table",
            request_base(BASIC_DIMENSIONS)
            + "\n| summarize Observadas = count(), Representadas = sum(Represented),"
            + " FilasPonderadas = countif(Represented != 1)",
            12,
            37,
            12,
            5,
            "Representadas usa sum(coalesce(itemCount, 1)); si difiere de Observadas "
            "los contadores del dashboard no representan todas las peticiones reales. "
            "Antes de filtros de identidad y modelo.",
        ),
        panel(
            "Cobertura y origen de la identidad de requests",
            "table",
            request_base(BASIC_DIMENSIONS)
            + request_summary(("Attribution",))
            + "\n| order by Peticiones desc",
            0,
            42,
            24,
            8,
            "directa: tres campos presentes en requests. inferida: contexto unico de metricas "
            "en el rango, compatible con campos presentes. ambigua: varios contextos; "
            "no se elige el ultimo. El join es leftouter y el mapa tiene una fila por clave. "
            "Antes de filtros de identidad/modelo.",
        ),
    ]


def organization_panels():
    return [
        note(
            "**Atribucion del consumo, no facturacion.** Compara agentes y departamentos; "
            "los equipos se distinguen por departamento. Las tablas muestran hasta 1000 filas. "
            "Top N solo limita los graficos, no las tablas."
        ),
        top_bar("agent", "Consumo por agente - Top N", 0, 3),
        top_bar("department", "Consumo por departamento - Top N", 12, 3),
        breakdown(("department", "team"), "Consumo por departamento y equipo", 11),
        top_trend("agent", "Evolucion por agente - Top N", 0, 20),
        top_trend("department", "Evolucion por departamento - Top N", 12, 20),
        breakdown(
            ("agent", "department", "team"),
            "Matriz de atribucion: agente / departamento / equipo",
            28,
        ),
        request_table(
            ("department", "team"),
            "Departamentos y equipos: impacto de HTTP429",
            37,
        ),
    ]


def user_panels():
    return [
        note(
            "**Vista con identificadores de usuario: restringe su acceso.** "
            "Permite identificar concentracion de consumo y atribucion por organizacion. "
            "Los tokens y las peticiones se calculan por separado. Las tablas de acceso "
            "identifican atribucion inferida o ausente. Top N se calcula para todo el rango."
        ),
        panel(
            "Concentracion en Top N usuarios",
            "stat",
            "let PerUser = materialize("
            + total_base()
            + f'\n| where user != "{MISSING}"'
            + "\n| summarize Tokens = sum(Tokens) by user);"
            + "\nlet TotalIdentificado = toscalar(PerUser | summarize sum(Tokens));"
            + "\nPerUser\n| top $topN by Tokens desc\n| summarize TopTokens = sum(Tokens)"
            + "\n| project Porcentaje = iff(TotalIdentificado > 0,"
            + " 100.0 * TopTokens / TotalIdentificado, real(null))",
            0,
            3,
            12,
            4,
            "Porcentaje del consumo con usuario identificado que corresponde al Top N. "
            "El denominador excluye (sin dimension).",
            "percent",
        ),
        panel(
            "Tokens sin usuario",
            "stat",
            total_base() + f'\n| where user == "{MISSING}"\n| summarize Tokens = sum(Tokens)',
            12,
            3,
            12,
            4,
            "Tokens sin identificador de usuario dentro de los filtros. "
            "Para auditar todos, selecciona All en Usuario.",
        ),
        top_bar("user", "Consumo por usuario - Top N", 0, 7),
        top_trend("user", "Evolucion por usuario - Top N", 12, 7),
        breakdown(
            ("user", "department", "team", "agent"),
            "Detalle de consumo por usuario y contexto",
            15,
        ),
        request_table(
            ("user", "department", "team", "subscriptionId", "product", "agent", "Attribution"),
            "Usuarios y subscriptions: impacto de HTTP429",
            24,
        ),
        panel(
            "Usuarios con mas HTTP429 - Top N",
            "barchart",
            request_base()
            + request_summary(("user",))
            + "\n| where HTTP429 > 0\n| top $topN by HTTP429 desc\n| project user, HTTP429",
            0,
            33,
            12,
            8,
            "Rechazos absolutos observados; incluye (sin dimension) si no se puede atribuir. "
            "La tabla de detalle distingue identidad directa, inferida y desconocida.",
        ),
        panel(
            "Usuarios con mayor porcentaje HTTP429 - Top N",
            "barchart",
            request_base()
            + request_summary(("user",))
            + "\n| where Peticiones >= $minRequests"
            + "\n| top $topN by Porcentaje desc\n| project user, Porcentaje",
            12,
            33,
            12,
            8,
            "Exige el minimo de peticiones seleccionado; no compara 1/1 con 100/100 "
            "sin contexto. Consulta volumen y calidad de atribucion en la tabla de detalle.",
            "percent",
        ),
    ]


def dashboard(uid, title, panels):
    for index, item in enumerate(panels, 1):
        item["id"] = index
    return {
        "__inputs": [
            {
                "name": "DS_AZURE_MONITOR",
                "label": "Azure Monitor",
                "description": (
                    "Datasource Azure Monitor con acceso de lectura a Application Insights."
                ),
                "type": "datasource",
                "pluginId": "grafana-azure-monitor-datasource",
                "pluginName": "Azure Monitor",
            }
        ],
        "__requires": [
            {"type": "grafana", "id": "grafana", "name": "Grafana", "version": "10.4.0"},
            {
                "type": "datasource",
                "id": "grafana-azure-monitor-datasource",
                "name": "Azure Monitor",
                "version": "1.0.0",
            },
        ],
        "id": None,
        "uid": uid,
        "title": title,
        "description": (
            "Consumo de tokens y acceso A2A/APIM desde customMetrics y requests "
            "de Application Insights. HTTP429 observado, sin atribuir su causa automaticamente."
        ),
        "tags": ["ai-token-consumption", "finops", "azure-monitor"],
        "editable": True,
        "schemaVersion": 39,
        "version": 2,
        "timezone": "utc",
        "time": {"from": "now-24h", "to": "now"},
        "refresh": "5m",
        "timepicker": {"refresh_intervals": ["1m", "5m", "15m", "30m", "1h"]},
        "links": [],
        "templating": {"list": variables()},
        "panels": panels,
        "annotations": {"list": []},
    }


def build_dashboards():
    panels = []
    y = 0
    for title, items in (
        ("Resumen de consumo", overview_panels()),
        ("Acceso y rechazos A2A", access_panels()),
        ("Productos y subscriptions", product_panels()),
        ("Reparto organizativo", organization_panels()),
        ("Usuarios", user_panels()),
        ("Calidad de datos", quality_panels()),
    ):
        panels.append(
            {
                "title": title,
                "type": "row",
                "collapsed": False,
                "panels": [],
                "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
            }
        )
        offset = y + 1 - min(item["gridPos"]["y"] for item in items)
        for item in items:
            item["gridPos"]["y"] += offset
        panels.extend(items)
        y = max(item["gridPos"]["y"] + item["gridPos"]["h"] for item in items)
    return {
        "tokens-consumption.json": dashboard(
            "ai-tokens-consumption", "AI Tokens - Consumo y acceso unificado", panels
        ),
    }


if __name__ == "__main__":
    for filename, content in build_dashboards().items():
        path = Path(__file__).resolve().parent / filename
        path.write_text(json.dumps(content, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        print(path.name)
