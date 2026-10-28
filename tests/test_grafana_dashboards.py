import json
import re
from pathlib import Path

import pytest

from dashboards.grafana.generate import (
    BASIC_DIMENSIONS,
    DATASOURCE,
    DIMENSIONS,
    LIMIT_TYPES,
    METRICS,
    MODEL_REQUEST_DIMENSIONS,
    REQUEST_DIMENSIONS,
    build_dashboards,
    dimension_query,
    failure_context,
    final_failure_base,
    impacted_invocations,
    model_request_base,
    product_comparison,
    raw_exceptions,
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
        "Acceso al modelo y limites LLM",
        "Errores fisicos e invocaciones impactadas",
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
        "HTTP429 agentes - request-rate-limit",
        "HTTP429 modelo - llm-rate-limit",
        "HTTP429 por tipo de limite",
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
        assert "$__timeFilter(timestamp)" in kql
        if "customMetrics\n" in kql:
            assert "Tokens = todouble(valueSum)" in kql
            for metric in METRICS:
                assert f'"{metric}"' in kql
        else:
            assert "requests\n" in kql
        assert "AppMetrics" not in kql
        assert not re.search(r"\b(?:sum|avg)\(value(?:Count)?\)", kql)
        assert "sum(valueSum) + " not in kql
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
                ("service",)
                if panel["title"] == "Contexto y sampling de requests HTTP"
                else BASIC_DIMENSIONS
                if "Antes de filtros" in panel.get("description", "")
                or "antes de filtros" in panel.get("description", "")
                else REQUEST_DIMENSIONS
                if "let AgentRequests" in kql
                else DIMENSIONS
                if "customMetrics\n" in kql
                else MODEL_REQUEST_DIMENSIONS
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
    assert kql[:join_at].count("by service, product") == 3
    assert "| join kind=fullouter ModelRequestsByProduct on service, product" in kql
    assert "RequestRateLimit429 = HTTP429" in kql
    assert "LLMRateLimit429 = HTTP429" in kql
    assert "PeticionesModelo = Peticiones" in kql
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


@pytest.mark.parametrize("api_type", ["a2a", "http"])
def test_request_limit_classification_uses_api_type(api_type):
    kql = raw_requests(api_type)
    assert f'customDimensions["API Type"]) =~ "{api_type}"' in kql
    assert f'ApiType = "{api_type}"' in kql
    assert f'LimitType = "{LIMIT_TYPES[api_type]}"' in kql
    assert 'customDimensions["HTTP Method"]) =~ "POST"' in kql
    assert 'customDimensions["API Name"]' in kql
    assert "LastError" not in kql
    assert "| join" not in kql


def test_model_requests_preserve_missing_context_without_inference():
    kql = model_request_base()
    assert 'customDimensions["API Type"]) =~ "http"' in kql
    assert "customMetrics" not in kql
    assert "join" not in kql
    for dimension in ("product", "subscriptionId", "api"):
        assert f'trim(@"\\s+", {dimension})' in kql
    for dimension in MODEL_REQUEST_DIMENSIONS:
        assert f"$__contains({dimension}, ${dimension})" in kql
    for dimension in ("agent", "department", "team", "user", "model"):
        assert f"$__contains({dimension}, ${dimension})" not in kql


def test_model_panels_use_http_denominators_and_separate_sampling(dashboard):
    panels = {p["title"]: p for p in dashboard["panels"]}
    for title in (
        "Peticiones HTTP al modelo observadas",
        "HTTP429 modelo - llm-rate-limit",
        "Porcentaje HTTP429 del modelo",
        "HTTP403 del modelo - causa sin confirmar",
        "Modelo: peticiones y llm-rate-limit por API y subscription",
    ):
        kql = panels[title]["targets"][0]["azureLogAnalytics"]["query"]
        assert 'customDimensions["API Type"]) =~ "http"' in kql
        assert 'customDimensions["API Type"]) =~ "a2a"' not in kql
        assert "Peticiones = count()" in kql
        assert "100.0 * HTTP429 / Peticiones" in kql
        assert "Representadas = sum(Represented)" in kql


def test_comparison_series_separates_types_and_uses_common_filters(dashboard):
    panels = {p["title"]: p for p in dashboard["panels"]}
    kql = panels["HTTP429 por tipo de limite"]["targets"][0]["azureLogAnalytics"]["query"]
    for api_type, limit_type in LIMIT_TYPES.items():
        assert f'customDimensions["API Type"]) =~ "{api_type}"' in kql
        assert f'LimitType = "{limit_type}"' in kql
    assert 'countif(Code == "429")' in kql
    assert "by bin(timestamp, $__interval), LimitType" in kql
    for dimension in MODEL_REQUEST_DIMENSIONS:
        assert kql.count(f"$__contains({dimension}, ${dimension})") == 2
    for dimension in ("agent", "department", "team", "user", "model"):
        assert f"$__contains({dimension}, ${dimension})" not in kql


@pytest.mark.parametrize("dimension", ["service", "product", "subscriptionId"])
def test_model_only_filter_options_ignore_unavailable_agent_filter(dimension):
    preceding = DIMENSIONS[: DIMENSIONS.index(dimension)]
    kql = dimension_query(dimension, preceding)
    http_branch = kql.split('customDimensions["API Type"]) =~ "http"', 1)[1].split("),\n(", 1)[0]
    assert "$__contains(agent, $agent)" not in http_branch
    assert f"| project {dimension})" in http_branch


def test_model_context_quality_exposes_missing_product_and_subscription(dashboard):
    panel = next(
        p for p in dashboard["panels"] if p["title"] == "Contexto y sampling de requests HTTP"
    )
    kql = panel["targets"][0]["azureLogAnalytics"]["query"]
    assert "$__contains(service, $service)" in kql
    for dimension in DIMENSIONS[1:]:
        assert f"$__contains({dimension}, ${dimension})" not in kql
    assert 'SinProducto = countif(product == "(sin dimension)")' in kql
    assert 'SinSubscription = countif(subscriptionId == "(sin dimension)")' in kql
    assert "ConSampling = countif(Represented > 1)" in kql


def test_product_comparison_formats_both_percentages(dashboard):
    panel = next(
        p
        for p in dashboard["panels"]
        if p["title"] == "Comparativa de productos: peticiones, HTTP429 y tokens"
    )
    override = panel["fieldConfig"]["overrides"][0]
    assert override["matcher"]["id"] == "byRegexp"
    for field in ("Porcentaje", "PorcentajeModelo"):
        assert re.fullmatch(override["matcher"]["options"], field)
    assert {"id": "unit", "value": "percent"} in override["properties"]
    assert {"id": "decimals", "value": 1} in override["properties"]


def test_final_failures_use_only_versioned_exception_contract():
    kql = raw_exceptions()
    assert kql.startswith("exceptions\n| where $__timeFilter(timestamp)")
    assert 'customDimensions["event"]) == "agent_invocation_failed"' in kql
    assert 'customDimensions["schema_version"]) == "1"' in kql
    for field in (
        "invocation_id",
        "correlation_source",
        "service",
        "agent",
        "task_id",
        "model_call_id",
        "model",
        "department",
        "team",
        "user",
        "error_origin",
        "error_kind",
    ):
        assert f'trim(@"\\s+", tostring(customDimensions["{field}"]))' in kql
    assert 'toint(customDimensions["upstream_status"])' in kql
    assert 'todouble(customDimensions["retry_after_seconds"])' in kql
    for forbidden in ("traces", "customEvents", "outerMessage", "operation_Id", "LastError"):
        assert forbidden not in kql
    for unavailable in ("product", "subscriptionId"):
        assert f'{unavailable} = "(sin dimension)"' in kql
        assert f'customDimensions["{unavailable}"]' not in kql
    assert "case(" not in kql  # Do not infer a cause or quota from HTTP status.


def test_exception_root_join_is_exact_preaggregated_and_preserves_unjoined_events():
    kql = failure_context()
    roots, exceptions = kql.split("let FinalFailures = ", 1)
    assert 'customDimensions["Request Id"]' in roots
    assert "InvocationId;" in request_context()
    assert "| summarize arg_max(timestamp, *) by service, InvocationId;" in roots
    assert "| project service, InvocationId, RootObserved = true" in roots
    assert "| join kind=leftouter RootContext on service, InvocationId" in exceptions
    assert 'isempty(InvocationId), "sin invocation_id"' in exceptions
    assert 'service == "(sin dimension)", "sin servicio"' in exceptions
    assert '"sin raiz A2A en intervalo"' in exceptions
    for dimension in REQUEST_DIMENSIONS:
        if dimension != "service":
            assert (
                f"{dimension} = iff(isnotempty(root_{dimension}) and "
                f'root_{dimension} != "(sin dimension)", root_{dimension}, {dimension})'
            ) in exceptions
    assert "$__timeFilter(timestamp)" in roots and "$__timeFilter(timestamp)" in exceptions
    assert "$__contains" not in kql
    assert "on service, subscriptionId" not in exceptions
    assert "bin(timestamp" not in kql


@pytest.mark.parametrize("helper", [final_failure_base, impacted_invocations])
def test_logical_failure_filters_use_request_context_and_ignore_model(helper):
    kql = helper()
    for dimension in REQUEST_DIMENSIONS:
        expression = f"$__contains({dimension}, ${dimension})"
        assert kql.count(expression) == 1
        assert kql.index(expression) > kql.index("| join kind=leftouter RootContext")
    assert "$__contains(model, $model)" not in kql


def test_unique_impacts_union_only_root_failures_and_final_exceptions_with_priority():
    kql = impacted_invocations()
    http = kql.split("let HttpFailures = ", 1)[1].split("let InstrumentedFailures", 1)[0]
    instrumented = kql.split("let InstrumentedFailures = ", 1)[1].split("let Impacted", 1)[0]
    unique = kql.split("let Impacted = ", 1)[1]
    assert "| where not(Successful) or toint(Code) >= 400" in http
    assert 'ErrorOrigin = "http_a2a"' in http
    assert "SourcePriority = 0" in http and "SourcePriority = 1" in instrumented
    assert 'isnotempty(InvocationId) and service != "(sin dimension)"' in instrumented
    assert "| summarize arg_max(timestamp, *) by service, InvocationId" in instrumented
    assert "union HttpFailures, InstrumentedFailures" in unique
    assert "| summarize arg_max(SourcePriority, *) by service, InvocationId;" in unique
    assert unique.index("arg_max") < unique.index("$__contains")
    for forbidden in ("dcount", 'API Type"]) =~ "http"', "by ModelCallId", "quota", "LastError"):
        assert forbidden not in kql
    assert 'where Code == "429"' not in kql
    assert "UpstreamStatus, RootCode" in http and "UpstreamStatus, RootCode" in instrumented


def test_error_row_distinguishes_physical_logical_origins_and_missing_key_quality(dashboard):
    rows = [p for p in dashboard["panels"] if p["type"] == "row"]
    row_index = next(
        i
        for i, row in enumerate(rows)
        if row["title"] == "Errores fisicos e invocaciones impactadas"
    )
    start, end = rows[row_index]["gridPos"]["y"], rows[row_index + 1]["gridPos"]["y"]
    section = [p for p in dashboard["panels"] if start < p["gridPos"]["y"] < end]
    panels = {p["title"]: p for p in section}
    note = panels["Alcance y lectura"]["options"]["content"]
    assert "ningun fallo instrumentado observado, no exito demostrado" in note
    assert "Application Insights exceptions" in note
    for title in (
        "HTTP429 fisicos A2A - no invocaciones",
        "HTTP429 fisicos modelo - no invocaciones",
    ):
        kql = panels[title]["targets"][0]["azureLogAnalytics"]["query"]
        assert 'countif(Code == "429")' in kql
        assert "exceptions" not in kql and "dcount" not in kql
        for dimension in MODEL_REQUEST_DIMENSIONS:
            assert f"$__contains({dimension}, ${dimension})" in kql
        for dimension in ("agent", "department", "team", "user", "model"):
            assert f"$__contains({dimension}, ${dimension})" not in kql
    kql = panels["Excepciones finales observadas - no unicos"]["targets"][0]["azureLogAnalytics"][
        "query"
    ]
    assert kql.endswith("| summarize ExcepcionesFinales = count()")
    for removed in (
        "Invocaciones impactadas unicas",
        "Invocaciones impactadas por origen",
        "Cobertura de excepciones finales y correlacion",
    ):
        assert removed not in panels
    http_quality = panels["Calidad de claves de fallos HTTP A2A"]["targets"][0][
        "azureLogAnalytics"
    ]["query"]
    assert "SinInvocationId = countif(isempty(InvocationId))" in http_quality
    assert 'SinServicio = countif(service == "(sin dimension)")' in http_quality


@pytest.mark.parametrize("dimension", REQUEST_DIMENSIONS)
def test_dimension_options_retain_exception_only_context(dimension):
    kql = dimension_query(dimension, DIMENSIONS[: DIMENSIONS.index(dimension)])
    branch = kql.rsplit(",\n(", 1)[1]
    assert branch.startswith("FinalFailures")
    assert f"| project {dimension})" in branch
    assert kql.count("let AgentRequests = ") == 1
    assert kql.count("let FinalFailures = ") == 1
