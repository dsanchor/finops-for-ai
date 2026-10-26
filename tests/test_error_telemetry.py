import logging
from uuid import uuid4

import httpx
import pytest
from azure.monitor.opentelemetry.exporter.export.logs._exporter import _convert_log_to_envelope
from openai import APIConnectionError, APIStatusError, APITimeoutError
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from agents.errors import failure_details, retry_after
from agents.telemetry import ExceptionTelemetry


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, None, "invalid_request"),
        (401, None, "authentication"),
        (403, None, "forbidden"),
        (403, "QuotaExceeded", "quota"),
        (429, "insufficient_quota", "quota"),
        (429, None, "rate_limit"),
        (500, None, "server_error"),
    ],
)
def test_http_error_classification_requires_quota_evidence(status, code, expected):
    response = httpx.Response(
        status, request=httpx.Request("POST", "https://model.example/responses")
    )
    root = APIStatusError("private response body", response=response, body={"code": code})
    wrapped = RuntimeError("private framework message")
    wrapped.__cause__ = root
    details = failure_details(wrapped)
    assert details["error_origin"] == "model"
    assert details["error_kind"] == expected
    assert details["upstream_status"] == status
    assert "private" not in str(details)


@pytest.mark.parametrize("error_type", [APIConnectionError, APITimeoutError])
def test_connection_errors_have_no_fabricated_http_status(error_type):
    error = error_type(request=httpx.Request("POST", "https://model.example/responses"))
    details = failure_details(error)
    assert details["error_origin"] == "model"
    assert "upstream_status" not in details
    assert details["retryable"] is True


def test_unknown_error_remains_agent_error_and_exception_cycles_terminate():
    root = RuntimeError("private prompt")
    root.__cause__ = root
    details = failure_details(root)
    assert details["error_origin"] == "agent"
    assert details["error_kind"] == "agent_error"
    assert "private" not in str(details)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("30", 30), ("0", 0), ("nan", None), ("inf", None), ("-1", None), ("invalid", None)],
)
def test_retry_guidance_is_finite_and_nonnegative(value, expected):
    assert retry_after(httpx.Headers({"Retry-After": value})) == expected
    assert retry_after(httpx.Headers({"retry-after-ms": "1250"})) == 1.25


def test_failures_are_exported_as_exception_data_not_traces(monkeypatch, caplog):
    exporter = InMemoryLogRecordExporter()
    monkeypatch.setattr("agents.telemetry.AzureMonitorLogExporter", lambda **kwargs: exporter)
    caplog.set_level(logging.ERROR, logger="agents.telemetry")
    telemetry = ExceptionTelemetry("test-connection-string", "risk")
    metadata = {
        "event": "agent_invocation_failed",
        "schema_version": "1",
        "invocation_id": str(uuid4()),
        "model_call_id": str(uuid4()),
        "correlation_source": "header",
        "service": "fixture",
        "agent": "risk",
        "task_id": "test-task",
        "model": "test-deployment",
        "error_origin": "model",
        "error_kind": "rate_limit",
        "upstream_status": 429,
        "retryable": True,
    }
    try:
        telemetry.record_failure(metadata)
        assert telemetry.provider.force_flush()
        logs = exporter.get_finished_logs()
        assert len(logs) == 1
        envelope = _convert_log_to_envelope(logs[0])
        assert envelope.name == "Microsoft.ApplicationInsights.Exception"
        assert envelope.data.base_type == "ExceptionData"
        for key, value in metadata.items():
            assert envelope.data.base_data.properties[key] == str(value)
        assert envelope.data.base_data.exceptions[0].type_name.endswith("InvocationFailure")
        assert "test-connection-string" not in caplog.text
    finally:
        telemetry.close()


def test_missing_connection_string_is_explicit(caplog):
    telemetry = ExceptionTelemetry(None, "risk")
    assert telemetry.logger is None
    assert "exception export disabled" in caplog.text
    telemetry.close()
