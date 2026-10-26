import json
import re
from pathlib import Path

import pytest

from dashboards.grafana.generate import (
    DATASOURCE,
    DIMENSIONS,
    METRICS,
    build_dashboards,
)

DASHBOARDS = build_dashboards()
DIRECTORY = Path(__file__).resolve().parents[1] / "dashboards" / "grafana"


@pytest.fixture(params=list(DASHBOARDS))
def dashboard(request):
    return DASHBOARDS[request.param]


def queries(dashboard):
    for variable in dashboard["templating"]["list"]:
        if variable["type"] == "query":
            yield variable["query"]
    for panel in dashboard["panels"]:
        yield from panel.get("targets", [])


@pytest.mark.parametrize("filename", list(DASHBOARDS))
def test_generated_json_is_current(filename):
    content = (DIRECTORY / filename).read_text(encoding="utf-8")
    assert json.loads(content) == DASHBOARDS[filename]
    assert content == json.dumps(DASHBOARDS[filename], indent=2, ensure_ascii=True) + "\n"


def test_only_unified_dashboard_is_generated():
    assert list(DASHBOARDS) == ["tokens-consumption.json"]
    assert DASHBOARDS["tokens-consumption.json"]["uid"] == "ai-tokens-consumption"
    assert sorted(path.name for path in DIRECTORY.glob("*.json")) == list(DASHBOARDS)


def test_import_contract_and_sections(dashboard):
    assert dashboard["id"] is None
    assert dashboard["__inputs"][0]["name"] == "DS_AZURE_MONITOR"
    assert dashboard["__inputs"][0]["pluginId"] == DATASOURCE["type"]
    assert dashboard["timezone"] == "utc"
    assert dashboard["refresh"] == "5m"
    assert dashboard["links"] == []
    rows = [panel for panel in dashboard["panels"] if panel["type"] == "row"]
    assert [row["title"] for row in rows] == [
        "Resumen de consumo",
        "Reparto organizativo",
        "Usuarios",
        "Calidad de datos",
    ]
    assert all(row["collapsed"] is False and row["panels"] == [] for row in rows)
    ids = [panel["id"] for panel in dashboard["panels"]]
    assert len(ids) == len(set(ids))


def test_unified_dashboard_does_not_duplicate_consumption_panels(dashboard):
    titles = [panel["title"] for panel in dashboard["panels"] if panel.get("targets")]
    assert len(titles) == len(set(titles))
    assert titles.count("Usuarios con metricas") == 1
    assert titles.count("Evolucion por agente - Top N") == 1
    assert titles.count("Consumo por departamento - Top N") == 1
    assert "Tokens totales seleccionados" not in titles
    for required in (
        "Tokens totales",
        "Consumo por departamento y equipo",
        "Detalle de consumo por usuario y contexto",
        "Calidad de las metricas",
    ):
        assert required in titles


def test_azure_monitor_query_shape(dashboard):
    for query in queries(dashboard):
        assert query["queryType"] == "Azure Log Analytics"
        assert query["datasource"] == DATASOURCE
        assert query["refId"] == "A"
        logs = query["azureLogAnalytics"]
        assert logs["resources"] == ["${appInsightsResourceId}"]
        assert logs["resultFormat"] in {"table", "time_series"}
        assert logs["dashboardTime"] is False
        assert logs["timeColumn"] == "timestamp"


def test_kql_uses_preaggregated_sums_and_dashboard_time(dashboard):
    for query in queries(dashboard):
        kql = query["azureLogAnalytics"]["query"]
        assert "customMetrics\n" in kql
        assert "$__timeFilter(timestamp)" in kql
        assert "Tokens = todouble(valueSum)" in kql
        assert "AppMetrics" not in kql
        assert not re.search(r"\b(?:sum|avg)\(value(?:Count)?\)", kql)
        assert "sum(valueSum) + " not in kql
        for metric in METRICS:
            assert f'"{metric}"' in kql
        if query["azureLogAnalytics"]["resultFormat"] == "time_series":
            assert "bin(timestamp, $__interval)" in kql
            assert kql.endswith("| order by timestamp asc")
            assert "make-series" not in kql


def test_variables_are_chained_and_all_is_not_a_literal_filter(dashboard):
    variables = dashboard["templating"]["list"]
    assert [v["name"] for v in variables] == [
        "appInsightsResourceId",
        *DIMENSIONS,
        "topN",
    ]
    for index, dimension in enumerate(DIMENSIONS):
        variable = variables[index + 1]
        assert variable["name"] == dimension
        assert variable["multi"] and variable["includeAll"]
        assert variable["allValue"] == "all"
        assert variable["refresh"] == 2
        kql = variable["query"]["azureLogAnalytics"]["query"]
        for preceding in DIMENSIONS[:index]:
            assert f"$__contains({preceding}, ${preceding})" in kql
        for following in DIMENSIONS[index:]:
            assert f"$__contains({following}, ${following})" not in kql
        assert f"| distinct {dimension}" in kql
    assert variables[-1]["query"] == "5,10,20,50"


def test_every_data_panel_applies_all_dimension_filters(dashboard):
    for panel in dashboard["panels"]:
        for query in panel.get("targets", []):
            kql = query["azureLogAnalytics"]["query"]
            for dimension in DIMENSIONS:
                assert f"$__contains({dimension}, ${dimension})" in kql
            if panel["type"] == "timeseries":
                assert panel["interval"] == "1m"
                assert panel["fieldConfig"]["defaults"]["custom"]["spanNulls"] is False
            if "| top $topN" in kql and "let Leaders" in kql:
                assert kql.index("| top $topN") < kql.index("bin(timestamp")


def test_panels_fit_grid_without_overlap(dashboard):
    positions = [panel["gridPos"] for panel in dashboard["panels"]]
    for index, first in enumerate(positions):
        assert first["x"] >= 0 and first["y"] >= 0
        assert first["w"] > 0 and first["h"] > 0
        assert first["x"] + first["w"] <= 24
        for second in positions[index + 1 :]:
            overlaps_x = (
                first["x"] < second["x"] + second["w"] and second["x"] < first["x"] + first["w"]
            )
            overlaps_y = (
                first["y"] < second["y"] + second["h"] and second["y"] < first["y"] + first["h"]
            )
            assert not (overlaps_x and overlaps_y)


def test_quality_panels_and_no_double_counting():
    panels = {p["title"]: p for p in DASHBOARDS["tokens-consumption.json"]["panels"]}
    query = panels["Tokens totales"]["targets"][0]["azureLogAnalytics"]["query"]
    assert query.endswith('| where name == "Total Tokens"\n| summarize Tokens = sum(Tokens)')
    trend = panels["Evolucion de entrada y salida"]["targets"][0]["azureLogAnalytics"]["query"]
    assert '| where name in ("Prompt Tokens", "Completion Tokens")' in trend
    assert (
        "countif(isnull(Tokens))"
        in (panels["Calidad de las metricas"]["targets"][0]["azureLogAnalytics"]["query"])
    )
    assert (
        "countif(Tokens < 0)"
        in (panels["Calidad de las metricas"]["targets"][0]["azureLogAnalytics"]["query"])
    )
    assert "min" not in panels["Diferencia: total - entrada - salida"]["fieldConfig"]["defaults"]
    assert (
        "datetime_diff('minute', now(), Ultima)"
        in (panels["Minutos desde la ultima metrica"]["targets"][0]["azureLogAnalytics"]["query"])
    )


def test_query_variables_are_defined(dashboard):
    defined = {v["name"] for v in dashboard["templating"]["list"]}
    for query in queries(dashboard):
        kql = query["azureLogAnalytics"]["query"]
        used = set(re.findall(r"\$(\w+)", kql))
        assert used <= defined | {"__timeFilter", "__contains", "__interval"}


def test_user_concentration_excludes_missing_from_numerator_and_denominator():
    panel = next(
        p
        for p in DASHBOARDS["tokens-consumption.json"]["panels"]
        if p["title"] == "Concentracion en Top N usuarios"
    )
    kql = panel["targets"][0]["azureLogAnalytics"]["query"]
    assert kql.index('| where user != "(sin dimension)"') < kql.index("by user")
    assert "toscalar(PerUser | summarize sum(Tokens))" in kql
    assert "100.0 * TopTokens / TotalIdentificado" in kql
    assert "real(null)" in kql
