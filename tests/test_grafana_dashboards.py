import json
import re
from pathlib import Path

import pytest

from dashboards.grafana.generate import (
    BASIC_DIMENSIONS,
    DATASOURCE,
    DIMENSIONS,
    METRICS,
    REQUEST_DIMENSIONS,
    build_dashboards,
    dimension_query,
    product_comparison,
    raw_requests,
    request_base,
    request_context,
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
        "Acceso y rechazos A2A",
        "Productos y subscriptions",
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
        "Respuestas HTTP429",
        "Comparativa de productos: peticiones, HTTP429 y tokens",
        "Usuarios y subscriptions: impacto de HTTP429",
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
        "minRequests",
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
    assert variables[-2]["query"] == "5,10,20,50"
    assert variables[-1]["query"] == "1,10,50,100"


def test_every_data_panel_applies_all_dimension_filters(dashboard):
    for panel in dashboard["panels"]:
        for query in panel.get("targets", []):
            kql = query["azureLogAnalytics"]["query"]
            required = (
                BASIC_DIMENSIONS
                if "Antes de filtros" in panel.get("description", "")
                or "antes de filtros" in panel.get("description", "")
                else REQUEST_DIMENSIONS
                if "let AgentRequests" in kql
                else DIMENSIONS
            )
            for dimension in required:
                assert f"$__contains({dimension}, ${dimension})" in kql
            if "let AgentRequests" in kql and "TokensByProduct" not in kql:
                assert "$__contains(model, $model)" not in kql
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


def test_request_scope_and_sampling_are_explicit():
    kql = raw_requests()
    assert 'customDimensions["API Type"]) =~ "a2a"' in kql
    assert 'customDimensions["HTTP Method"]) =~ "POST"' in kql
    assert 'customDimensions["gen_ai.agent.id"]' in kql
    assert 'customDimensions["Subscription Name"]' in kql
    assert 'customDimensions["Product Name"]' in kql
    assert "coalesce(tolong(itemCount), long(1))" in kql
    assert "coalesce(tobool(success), false)" in kql
    assert "$__timeFilter(timestamp)" in kql


def test_identity_map_is_unique_before_left_join_and_before_identity_filters():
    kql = request_context()
    join_at = kql.index("| join kind=leftouter IdentityContexts")
    assert "| summarize by service, agent, subscriptionId, department, team, user" in kql[:join_at]
    assert "| summarize MappingContexts = count()" in kql[:join_at]
    assert "by service, agent, subscriptionId;" in kql[:join_at]
    assert "MappingContexts == 1" in kql
    assert "MappingContexts > 1" in kql
    assert "not(IdentityConflict)" in kql
    assert "arg_max" not in kql
    assert "$__contains" not in kql
    assert '"ambigua"' in kql
    assert '"sin correspondencia"' in kql
    assert '"mapeo incompleto"' in kql
    assert '"conflicto"' in kql
    assert "DirectComplete" in kql


def test_request_filters_do_not_use_model_and_apply_after_enrichment():
    kql = request_base()
    for dimension in REQUEST_DIMENSIONS:
        assert f"$__contains({dimension}, ${dimension})" in kql
        assert kql.index(f"$__contains({dimension}, ${dimension})") > kql.index("let AgentRequests")
    assert "$__contains(model, $model)" not in kql


def test_product_join_is_one_to_one_and_preserves_missing_tokens():
    kql = product_comparison()
    join_at = kql.index("| join kind=fullouter TokensByProduct on service, product")
    assert kql[:join_at].count("by service, product") == 2
    assert "coalesce(Total" not in kql
    assert "coalesce(Entrada" not in kql
    assert "coalesce(Salida" not in kql
    assert "$__contains(model, $model)" not in kql.split("let TokensByProduct")[0]
    assert "$__contains(model, $model)" in kql.split("let TokensByProduct")[1]


def test_options_include_request_only_subscriptions_and_products():
    for dimension in BASIC_DIMENSIONS:
        kql = dimension_query(dimension, ())
        assert "union (" in kql
        assert "customMetrics" in kql and "requests" in kql
    assert "requests" not in dimension_query("model", REQUEST_DIMENSIONS)


def test_rejection_percentages_have_zero_guard_and_ranking_minimum(dashboard):
    panels = {p["title"]: p for p in dashboard["panels"]}
    summary = panels["Porcentaje HTTP429 observado"]["targets"][0]["azureLogAnalytics"]["query"]
    assert "100.0 * HTTP429 / Peticiones" in summary
    assert "iff(Peticiones > 0" in summary
    assert 'countif(Code == "403")' in summary
    assert 'countif(Code == "429")' in summary
    for title in (
        "Usuarios con mayor porcentaje HTTP429 - Top N",
        "Porcentaje HTTP429 por agente - Top N",
    ):
        kql = panels[title]["targets"][0]["azureLogAnalytics"]["query"]
        assert "| where Peticiones >= $minRequests" in kql


def test_coverage_preserves_unattributed_traffic_under_identity_filters(dashboard):
    panels = {p["title"]: p for p in dashboard["panels"]}
    for title in (
        "Peticiones con identidad completa - cobertura",
        "Peticiones sin identidad completa",
        "Sampling de requests",
        "Cobertura y origen de la identidad de requests",
    ):
        kql = panels[title]["targets"][0]["azureLogAnalytics"]["query"]
        for dimension in BASIC_DIMENSIONS:
            assert f"$__contains({dimension}, ${dimension})" in kql
        for dimension in ("department", "team", "user", "model"):
            assert f"$__contains({dimension}, ${dimension})" not in kql
