"""Generate a unified Grafana dashboard for Application Insights token metrics."""

import json
from pathlib import Path

DATASOURCE = {"type": "grafana-azure-monitor-datasource", "uid": "${DS_AZURE_MONITOR}"}
RESOURCE_ID = (
    "/subscriptions/0c2b616d-05ea-49f8-b176-e2e06149c164"
    "/resourceGroups/finops-for-ai-rg/providers/Microsoft.Insights/components/finops4aiappsins"
)
DIMENSIONS = ("service", "agent", "department", "team", "user")
METRICS = ("Prompt Tokens", "Completion Tokens", "Total Tokens")
MISSING = "(sin dimension)"
BASE = """customMetrics
| where $__timeFilter(timestamp)
| where name in ("Prompt Tokens", "Completion Tokens", "Total Tokens")
| extend
    service = tostring(customDimensions["Service ID"]),
    agent = tostring(customDimensions["agent"]),
    department = tostring(customDimensions["department"]),
    team = tostring(customDimensions["team"]),
    user = tostring(customDimensions["user"]),
    Tokens = todouble(valueSum)
| extend
""" + ",\n".join(
    f'    {dimension} = iff(isempty(trim(@"\\s+", {dimension})), '
    f'"{MISSING}", trim(@"\\s+", {dimension}))'
    for dimension in DIMENSIONS
)


def base(dimensions=DIMENSIONS):
    return BASE + "".join(
        f"\n| where $__contains({dimension}, ${dimension})" for dimension in dimensions
    )


def total_base():
    return base() + '\n| where name == "Total Tokens"'


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
    labels = ("Servicio APIM", "Agente", "Departamento", "Equipo", "Usuario")
    for index, (dimension, label) in enumerate(zip(DIMENSIONS, labels, strict=True)):
        query = (
            base(DIMENSIONS[:index])
            + f'\n| where name == "Total Tokens"\n| distinct {dimension}'
            + f"\n| order by {dimension} asc"
        )
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
                "decimals": 0,
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


def overview_panels():
    panels = [
        note(
            "**Consumo de tokens, no costes ni peticiones.** Total = `Total Tokens`; "
            "entrada/salida se muestran por separado. Filtros comunes a todas las secciones. "
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
                f'Sin_{d} = sumif(Tokens, {d} == "{MISSING}")'
                for d in ("agent", "department", "team", "user")
            ),
            0,
            32,
            24,
            5,
            "Un mismo token puede carecer de varias dimensiones: las columnas Sin_* "
            "se solapan y NO deben sumarse. Los filtros pueden excluir registros incompletos; "
            "selecciona All para una auditoria global.",
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
    ]


def user_panels():
    return [
        note(
            "**Vista con identificadores de usuario: restringe su acceso.** "
            "Permite identificar concentracion de consumo y atribucion por organizacion. "
            "No equivale a numero de llamadas ni a coste. Top N se calcula para todo el rango."
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
        "description": "Consumo de tokens A2A/APIM desde customMetrics de Application Insights.",
        "tags": ["ai-token-consumption", "finops", "azure-monitor"],
        "editable": True,
        "schemaVersion": 39,
        "version": 1,
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
            "ai-tokens-consumption", "AI Tokens - Consumo unificado", panels
        ),
    }


if __name__ == "__main__":
    for filename, content in build_dashboards().items():
        path = Path(__file__).resolve().parent / filename
        path.write_text(json.dumps(content, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        print(path.name)
