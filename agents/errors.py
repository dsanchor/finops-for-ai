import logging
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError

logger = logging.getLogger(__name__)
Metadata = dict[str, str | int | float | bool]


class InvocationFailure(RuntimeError):
    """A sanitized failure safe to expose through A2A and exception telemetry."""


def model_exception(error: Exception) -> Exception | None:
    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending:
        candidate = pending.pop()
        if id(candidate) in visited:
            continue
        visited.add(id(candidate))
        if isinstance(candidate, (APIError, httpx.HTTPError)):
            return candidate
        for cause in (
            candidate.__cause__,
            candidate.__context__,
            getattr(candidate, "inner_exception", None),
        ):
            if isinstance(cause, BaseException):
                pending.append(cause)
    return None


def retry_after(headers: Mapping[str, str]) -> float | None:
    milliseconds = headers.get("retry-after-ms")
    value = milliseconds if milliseconds is not None else headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value) / (1000 if milliseconds is not None else 1)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            seconds = (date - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            logger.warning("Invalid upstream retry guidance; retry_after_seconds omitted")
            return None
    if not math.isfinite(seconds) or seconds < 0:
        logger.warning("Invalid upstream retry guidance; retry_after_seconds omitted")
        return None
    return seconds


def failure_details(error: Exception) -> Metadata:
    upstream = model_exception(error)
    details: Metadata = {
        "error_origin": "model" if upstream is not None else "agent",
        "error_kind": "model_error" if upstream is not None else "agent_error",
        "exception_type": type(upstream if upstream is not None else error).__name__,
        "retryable": False,
    }
    if isinstance(upstream, (APITimeoutError, httpx.TimeoutException)):
        details.update(error_kind="timeout", retryable=True)
    elif isinstance(upstream, (APIConnectionError, httpx.TransportError)):
        details.update(error_kind="connection", retryable=True)
    elif isinstance(upstream, (APIStatusError, httpx.HTTPStatusError)):
        status = upstream.response.status_code
        details["upstream_status"] = status
        code = getattr(upstream, "code", None)
        reason = upstream.response.headers.get("X-Apim-Error-Reason", "")
        quota_codes = {"quotaexceeded", "tokenquotaexceeded", "insufficient_quota"}
        if str(code).casefold() in quota_codes or reason.casefold() in quota_codes:
            details["error_kind"] = "quota"
        else:
            details["error_kind"] = {
                400: "invalid_request",
                401: "authentication",
                403: "forbidden",
                404: "not_found",
                408: "timeout",
                409: "conflict",
                422: "invalid_request",
                429: "rate_limit",
            }.get(status, "server_error" if status >= 500 else "model_error")
            details["retryable"] = status in (408, 409, 429) or status >= 500
        delay = retry_after(upstream.response.headers)
        if delay is not None:
            details["retry_after_seconds"] = delay
    return details


def failure_message(details: Metadata) -> str:
    status = details.get("upstream_status")
    suffix = f" (HTTP {status})" if status is not None else ""
    return f"{details['error_origin']} execution failed: {details['error_kind']}{suffix}"
