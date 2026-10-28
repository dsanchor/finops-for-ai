import asyncio
import json
import logging
import random
import socket
import time
from argparse import Namespace
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import uvicorn
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.client.errors import A2AClientError
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import Event, EventQueue
from a2a.types import (
    Artifact,
    Role,
    SendMessageRequest,
    StreamResponse,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.types import Message as A2AMessage
from a2a.types import Part as A2APart
from agent_framework import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
)
from agent_framework.openai import OpenAIChatClient
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from openai import AsyncOpenAI, RateLimitError
from pydantic import ValidationError
from starlette.routing import Route

from agents.config import Settings
from agents.profiles import PROFILES
from agents.server import UsageExecutor, build_agent, create_app, credential_for
from clients.common import AgentTaskFailure, main, run, stream_request
from clients.identities import load_identities
from clients.workloads import random_prompt, with_documents


def settings(kind="risk", **overrides) -> Settings:
    return Settings(
        kind=kind,
        openai_base_url=overrides.pop("openai_base_url", "https://demo.openai.azure.com/openai/v1"),
        model="gpt-5.6-sol" if kind == "risk" else "gpt-5.4-mini",
        port=8001 if kind == "risk" else 8002,
        public_url=overrides.pop("public_url", "http://localhost:8001"),
        **overrides,
    )


def without_correlation(headers):
    return {
        name: value
        for name, value in headers.items()
        if name not in ("X-Invocation-Id", "X-Model-Call-Id", "Request-Id", "Request-Context")
    }


class FakeChatClient(BaseChatClient):
    def __init__(self):
        super().__init__()
        self.prompts: list[str] = []
        self.options: list[Mapping[str, Any]] = []
        self.request_kwargs: dict[str, dict[str, Any]] = {}

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        prompt = messages[-1].text
        self.prompts.append(prompt)
        self.options.append(options)
        self.request_kwargs[prompt] = kwargs

        async def updates() -> AsyncIterator[ChatResponseUpdate]:
            if prompt == "FAIL":
                raise RuntimeError("Synthetic model failure")
            if prompt == "RATE_LIMIT":
                raise RateLimitError(
                    "private upstream body",
                    response=httpx.Response(
                        429, request=httpx.Request("POST", "https://model.example/responses")
                    ),
                    body=None,
                )
            for text in ("Respuesta: ", prompt):
                yield ChatResponseUpdate(role="assistant", contents=[Content.from_text(text)])
                await asyncio.sleep(0.1)
            yield ChatResponseUpdate(
                contents=[
                    Content.from_usage(
                        {
                            "input_token_count": 123,
                            "output_token_count": 12,
                            "total_token_count": 135,
                        }
                    )
                ]
            )

        response = ResponseStream(updates(), finalizer=ChatResponse.from_updates)
        return response if stream else response.get_final_response()


@pytest_asyncio.fixture(params=["risk", "customer"])
async def live_agent(request, monkeypatch):
    monkeypatch.delenv("RISK_AGENT_CARD_URL", raising=False)
    monkeypatch.delenv("CUSTOMER_AGENT_CARD_URL", raising=False)
    model = FakeChatClient()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        config = settings(request.param, public_url=url)
        monkeypatch.setenv(
            f"{config.kind.upper()}_AGENT_CARD_URL", url + "/.well-known/agent-card.json"
        )
        app = create_app(
            config,
            executor_factory=lambda context: UsageExecutor(
                build_agent(config, model), config, context
            ),
        )
        card_endpoint = next(
            route.endpoint
            for route in app.routes
            if isinstance(route, Route) and route.path == "/.well-known/agent-card.json"
        )
        app.router.routes.append(Route("/gateway/agent-card.json", card_endpoint))
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            yield url, model, config
        finally:
            server.should_exit = True
            async with asyncio.timeout(10):
                await task


@pytest.mark.parametrize("kind", ["risk", "customer"])
def test_profiles_without_team(kind):
    profile = PROFILES[kind]
    assert not hasattr(profile, "team")
    assert "equipo" not in profile.instructions.lower()


async def test_real_http_streaming_and_usage(live_agent, caplog):
    url, model, config = live_agent
    caplog.set_level(logging.INFO, logger="agents.server")
    async with httpx.AsyncClient(timeout=10) as http:
        health = await http.get(url + "/healthz")
        assert health.json() == {"status": "ok", "agent": config.kind}
        card = (await http.get(url + "/.well-known/agent-card.json")).json()
        assert card["capabilities"]["streaming"]
        profile = PROFILES[config.kind]
        assert card["description"] == f"{profile.department}. {profile.description}"
        assert card["skills"][0]["tags"] == [profile.department]
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["JSONRPC"])
        ).create_from_url(url)
        chunks = []
        try:
            start = time.perf_counter()
            text = await stream_request(
                client, "Hola", lambda text: chunks.append((text, time.perf_counter() - start))
            )
            elapsed = time.perf_counter() - start
        finally:
            await client.close()
    assert text == "Respuesta: Hola"
    assert "".join(c[0] for c in chunks) == text
    assert len(chunks) >= 2
    assert chunks[0][1] < elapsed - 0.01
    usage = [json.loads(r.message) for r in caplog.records if '"model_usage"' in r.message]
    assert len(usage) == 1
    assert usage[0]["model_deployment"] == config.model
    assert usage[0]["department"] == PROFILES[config.kind].department
    assert "team" not in usage[0]
    assert usage[0]["usage"]["input_token_count"] == 123
    assert usage[0]["task_id"]
    assert "max_tokens" not in model.options[0]
    assert "max_output_tokens" not in model.options[0]
    assert model.options[0]["store"] is False


async def test_concurrent_requests_are_isolated(live_agent):
    url, model, _ = live_agent
    async with httpx.AsyncClient(timeout=10) as http:
        client = await ClientFactory(ClientConfig(httpx_client=http)).create_from_url(url)
        try:
            responses = await asyncio.gather(
                *(stream_request(client, f"Consulta {i}") for i in range(8))
            )
        finally:
            await client.close()
    assert responses == [f"Respuesta: Consulta {i}" for i in range(8)]
    assert sorted(model.prompts) == [f"Consulta {i}" for i in range(8)]
    for name in ("X-Invocation-Id", "X-Model-Call-Id"):
        identifiers = [kwargs["extra_headers"][name] for kwargs in model.request_kwargs.values()]
        assert len(set(identifiers)) == 8
        assert all(str(UUID(value)) == value for value in identifiers)


async def test_model_headers_are_isolated_per_request(live_agent, caplog):
    url, model, config = live_agent
    caplog.set_level(logging.INFO, logger="agents.server")

    async def send(index, model_headers):
        headers = {"Authorization": "Bearer inbound-only", "X-Unrelated": "not-forwarded"}
        headers.update({name.swapcase(): value for name, value in model_headers.items()})
        headers.update(
            {"X-Product-Id": "blocked-product", "X-Subscription-Id": "blocked-subscription-id"}
        )
        async with httpx.AsyncClient(timeout=10, headers=headers) as http:
            client = await ClientFactory(ClientConfig(httpx_client=http)).create_from_url(url)
            try:
                prompt = f"Header request {index}"
                assert await stream_request(client, prompt) == f"Respuesta: {prompt}"
            finally:
                await client.close()
        forwarded = model.request_kwargs[prompt].get("extra_headers", {})
        assert without_correlation(forwarded) == model_headers

    masks = (0, 1, 2, 4, 8, 16, 15, 31)
    await asyncio.gather(
        *(
            send(
                index,
                {
                    name: f"value-{index}-{name}"
                    for bit, name in enumerate(
                        (
                            "X-Agent",
                            "X-Department",
                            "X-Team",
                            "X-User",
                            "Ocp-Apim-Subscription-Key",
                        )
                    )
                    if index & (1 << bit)
                },
            )
            for index in masks
        )
    )
    await send("after-concurrent-requests", {})
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "agents.server" and '"request_headers"' in record.message
    ]
    assert len(records) == len(masks) + 1
    assert all(record["agent"] == config.kind for record in records)
    assert sorted(json.dumps(record["headers"], sort_keys=True) for record in records) == sorted(
        json.dumps(
            {
                name: value
                for name, value in without_correlation(kwargs.get("extra_headers", {})).items()
                if name != "Ocp-Apim-Subscription-Key"
            },
            sort_keys=True,
        )
        for kwargs in model.request_kwargs.values()
    )
    assert "Ocp-Apim-Subscription-Key" not in caplog.text
    assert "value-16-Ocp-Apim-Subscription-Key" not in caplog.text
    assert "value-31-Ocp-Apim-Subscription-Key" not in caplog.text
    assert "blocked-product" not in caplog.text
    assert "blocked-subscription-id" not in caplog.text
    assert records[-1]["headers"] == {}


class CollectingEventQueue(EventQueue):
    def __init__(self):
        self.events: list[Event] = []

    async def enqueue_event(self, event: Event) -> None:
        self.events.append(event)


def model_stream_response(model: str) -> httpx.Response:
    response = {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": model,
        "output": [],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }
    events = [
        {
            "type": "response.output_text.delta",
            "item_id": "msg_test",
            "output_index": 0,
            "content_index": 0,
            "delta": "Respuesta",
            "sequence_number": 1,
        },
        {"type": "response.completed", "response": response, "sequence_number": 2},
    ]
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        content="".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
        ),
    )


@pytest.mark.parametrize("kind", ["risk", "customer"])
@pytest.mark.parametrize("header_mask", range(32))
async def test_forwarded_headers_reach_model_http_request(kind, header_mask, caplog):
    caplog.set_level(logging.INFO, logger="agents.server")
    requests: list[httpx.Request] = []

    def respond(request):
        requests.append(request)
        return model_stream_response(config.model)

    config = settings(kind)
    header_names = (
        "X-Agent",
        "X-Department",
        "X-Team",
        "X-User",
        "Ocp-Apim-Subscription-Key",
    )
    model_headers = {
        name: f"apim-value-{name}"
        for bit, name in enumerate(header_names)
        if header_mask & (1 << bit)
    }
    incoming_headers = {"authorization": "Bearer inbound-only", "x-unrelated": "not-forwarded"}
    incoming_headers.update({name.lower(): value for name, value in model_headers.items()})
    incoming_headers.update(
        {"x-product-id": "blocked-product", "x-subscription-id": "blocked-subscription-id"}
    )
    trace = {
        "request-id": f"|trace-{kind}-{header_mask}.span.",
        "request-context": "appId=cid-v1:x",
    }
    incoming_headers.update(trace)
    context = RequestContext(
        call_context=ServerCallContext(state={"headers": incoming_headers}),
        request=SendMessageRequest(
            message=A2AMessage(
                message_id=str(uuid4()),
                role=Role.ROLE_USER,
                parts=[A2APart(text="Hola")],
            )
        ),
    )
    queue = CollectingEventQueue()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
        AsyncOpenAI(
            api_key="test-model-credential",
            base_url=config.openai_base_url,
            http_client=http,
        ) as sdk_client,
    ):
        client = OpenAIChatClient(model=config.model, async_client=sdk_client)
        await UsageExecutor(build_agent(config, client), config, context).execute(context, queue)
    assert len(requests) == 1
    for name in header_names:
        assert requests[0].headers.get(name) == model_headers.get(name)
    assert requests[0].headers["authorization"] == "Bearer test-model-credential"
    assert "x-unrelated" not in requests[0].headers
    assert "X-Product-Id" not in requests[0].headers
    assert "X-Subscription-Id" not in requests[0].headers
    assert requests[0].headers["Request-Id"] == trace["request-id"]
    assert requests[0].headers["Request-Context"] == trace["request-context"]
    assert trace["request-id"] not in caplog.text
    assert "Ocp-Apim-Subscription-Key" not in caplog.text
    assert "apim-value-Ocp-Apim-Subscription-Key" not in caplog.text
    assert "blocked-product" not in caplog.text
    assert "blocked-subscription-id" not in caplog.text
    body = json.loads(requests[0].content)
    assert body["model"] == config.model
    assert body["stream"] is True
    assert body["store"] is False
    statuses = [
        event.status.state for event in queue.events if isinstance(event, TaskStatusUpdateEvent)
    ]
    assert statuses[-1] == TaskState.TASK_STATE_COMPLETED


@pytest.mark.parametrize("kind", ["risk", "customer"])
@pytest.mark.parametrize(
    "base_url",
    [
        "https://finopsforai.openai.azure.com/openai/v1",
        "https://finops4ai.azure-api.net/finopsforai/openai/v1",
    ],
)
async def test_configured_model_endpoint_and_headers(kind, base_url, monkeypatch):
    config = settings(kind, auth_mode="cli", openai_base_url=base_url, public_url="http://agent")
    model_headers = {
        "X-Agent": "apim-onboarded-agent",
        "X-Department": "apim-department",
        "X-Team": "apim-team",
        "X-User": "apim-user",
        "Ocp-Apim-Subscription-Key": "test-subscription-key",
    }
    requests: list[httpx.Request] = []

    def respond(request):
        requests.append(request)
        return model_stream_response(config.model)

    class TestCredential(AsyncTokenCredential):
        def __init__(self):
            self.scopes: list[tuple[str, ...]] = []
            self.closed = False

        async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
            self.scopes.append(scopes)
            return AccessToken("test-entra-token", int(time.time()) + 3600)

        async def close(self) -> None:
            self.closed = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.close()

    credential = TestCredential()
    monkeypatch.setattr("agents.server.credential_for", lambda settings: credential)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model_http:

        def create_model_client(*, model, base_url, api_key):
            sdk_client = AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=model_http)
            return OpenAIChatClient(model=model, async_client=sdk_client)

        monkeypatch.setattr("agents.server.OpenAIChatClient", create_model_client)
        app = create_app(config)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                headers={
                    **model_headers,
                    "X-Product-Id": "blocked-product",
                    "X-Subscription-Id": "blocked-subscription-id",
                },
            ) as http,
        ):
            client = await ClientFactory(ClientConfig(httpx_client=http)).create_from_url(
                config.public_url
            )
            try:
                assert await stream_request(client, "Hola") == "Respuesta"
            finally:
                await client.close()
    assert len(requests) == 1
    assert str(requests[0].url) == f"{base_url}/responses"
    for name, value in model_headers.items():
        assert requests[0].headers[name] == value
    assert "X-Product-Id" not in requests[0].headers
    assert "X-Subscription-Id" not in requests[0].headers
    assert requests[0].headers["authorization"] == "Bearer test-entra-token"
    assert credential.scopes == [("https://ai.azure.com/.default",)]
    assert credential.closed


async def test_model_failure_is_not_success(live_agent):
    url, _, _ = live_agent
    async with httpx.AsyncClient(timeout=10) as http:
        client = await ClientFactory(ClientConfig(httpx_client=http)).create_from_url(url)
        try:
            with pytest.raises(RuntimeError, match="TASK_STATE_FAILED.*agent execution failed"):
                await stream_request(client, "FAIL")
        finally:
            await client.close()


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, None, "invalid_request"),
        (401, None, "authentication"),
        (403, None, "forbidden"),
        (403, "QuotaExceeded", "quota"),
        (429, None, "rate_limit"),
        (429, "insufficient_quota", "quota"),
        (500, None, "server_error"),
    ],
)
@pytest.mark.parametrize("kind", ["risk", "customer"])
@pytest.mark.parametrize("retries", [0, 2])
async def test_model_http_failures_are_safe_correlated_failed_tasks(
    kind, retries, status, code, expected, caplog
):
    caplog.set_level(logging.INFO)
    assert settings(kind).max_retries == 0
    config = settings(kind, max_retries=retries)
    invocation = str(uuid4())
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(
            status,
            headers={"Retry-After": "0.001"},
            json={"error": {"message": "private-upstream-body", "code": code}},
        )

    context = RequestContext(
        call_context=ServerCallContext(
            state={
                "headers": {
                    "X-Invocation-Id": invocation,
                    "X-Apim-Service-Id": "fixture-apim",
                    "X-Agent": "gateway-agent",
                    "Ocp-Apim-Subscription-Key": "private-subscription-key",
                }
            }
        ),
        request=SendMessageRequest(
            message=A2AMessage(
                message_id=str(uuid4()), role=Role.ROLE_USER, parts=[A2APart(text="Hola")]
            )
        ),
    )
    queue = CollectingEventQueue()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
        AsyncOpenAI(
            api_key="test-model-credential",
            base_url=config.openai_base_url,
            http_client=http,
            max_retries=config.max_retries,
        ) as sdk,
    ):
        model = OpenAIChatClient(model=config.model, async_client=sdk)
        await UsageExecutor(build_agent(config, model), config, context).execute(context, queue)
    attempts = retries + 1 if status == 429 or status >= 500 else 1
    assert len(captured) == attempts
    assert all(request.headers["X-Invocation-Id"] == invocation for request in captured)
    assert len({request.headers["X-Model-Call-Id"] for request in captured}) == 1
    statuses = [e for e in queue.events if isinstance(e, TaskStatusUpdateEvent)]
    failed = [e for e in statuses if e.status.state == TaskState.TASK_STATE_FAILED]
    assert len(failed) == 1
    assert statuses[-1] is failed[0]
    details = failed[0].metadata
    assert details["invocation_id"] == invocation
    assert details["model_call_id"] == captured[0].headers["X-Model-Call-Id"]
    assert details["error_origin"] == "model"
    assert details["error_kind"] == expected
    assert details["upstream_status"] == status
    assert details["partial_output"] is False
    records = [r for r in caplog.records if r.name == "agents.telemetry"]
    assert len(records) == 1
    assert records[0].invocation_id == invocation
    assert records[0].task_id == failed[0].task_id
    assert records[0].service == "fixture-apim"
    assert "private-subscription-key" not in caplog.text + str(queue.events)
    assert "private-upstream-body" not in caplog.text + str(queue.events)


async def test_failed_task_inside_http200_retains_error_metadata(live_agent):
    url, _, _ = live_agent
    responses = []

    async def capture_response(response):
        responses.append((response.request.method, response.status_code))

    invocation = str(uuid4())
    async with httpx.AsyncClient(
        timeout=10,
        headers={"X-Invocation-Id": invocation},
        event_hooks={"response": [capture_response]},
    ) as http:
        client = await ClientFactory(ClientConfig(httpx_client=http)).create_from_url(url)
        try:
            with pytest.raises(AgentTaskFailure) as result:
                await stream_request(client, "RATE_LIMIT")
            assert result.value.metadata["invocation_id"] == invocation
            assert result.value.metadata["error_origin"] == "model"
            assert result.value.metadata["upstream_status"] == 429
            assert "private upstream body" not in str(result.value)
        finally:
            await client.close()
    assert ("POST", 200) in responses


async def test_failed_client_result_records_structured_metadata(live_agent, tmp_path):
    _, _, config = live_agent
    output = tmp_path / "failed.jsonl"
    args = Namespace(
        seed=1,
        identities_file=None,
        identity_mode="sequence",
        document_sections=1,
        random=False,
        prompt="RATE_LIMIT",
        document=[],
        requests=1,
        concurrency=1,
        interval=0,
        timeout=10,
        output=output,
    )
    assert await run(args, config.kind) == 1
    result = json.loads(output.read_text())
    assert result["status"] == "failed"
    assert result["error_details"]["error_origin"] == "model"
    assert result["error_details"]["upstream_status"] == 429
    assert result["error_details"]["invocation_id"]


async def test_random_load_and_jsonl(live_agent, tmp_path, capsys):
    url, model, config = live_agent
    output = tmp_path / "results.jsonl"
    args = Namespace(
        seed=7,
        identities_file=None,
        identity_mode="sequence",
        document_sections=60,
        random=True,
        prompt=None,
        document=[],
        requests=4,
        concurrency=2,
        interval=0,
        timeout=10,
        output=output,
    )
    assert await run(args, config.kind) == 0
    assert all(
        not without_correlation(kwargs.get("extra_headers", {}))
        for kwargs in model.request_kwargs.values()
    )
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(records) == 4
    assert all(r["status"] == "completed" for r in records)
    assert all(r["first_text_seconds"] is not None for r in records)
    assert all(
        r["input_chars"] == len(p)
        for r, p in zip(sorted(records, key=lambda r: r["request_id"]), model.prompts, strict=True)
    )
    for record in records:
        assert record["output"] == f"Respuesta: {record['input']}"
        assert record["input_chars"] == len(record["input"])
        assert record["output_chars"] == len(record["output"])
    captured = capsys.readouterr()
    for record in records:
        label = f"[{config.kind} #{record['request_id']}]"
        request = f"{label} Peticion:\n{record['input']}\n"
        response = f"{label} Respuesta:\n{record['output']}\n"
        assert request in captured.out
        assert response in captured.out
        assert captured.out.index(request) < captured.out.index(response)
    assert '"request_id":' not in captured.out
    assert '"failures": 0' in captured.err


@pytest.mark.parametrize("identity_mode", ["random", "sequence"])
@pytest.mark.parametrize("seed", [42, 43])
@pytest.mark.parametrize("with_subscription_keys", [False, True])
async def test_client_csv_headers_reach_model(
    live_agent, tmp_path, monkeypatch, identity_mode, seed, with_subscription_keys, caplog
):
    url, model, config = live_agent
    path = tmp_path / "identities.csv"
    rows = (
        ("finance", "globalrisk", "user1"),
        ("retail", "cards", "user2"),
        ("technology", "platform", "user3"),
    )
    path.write_text(
        "".join(
            ",".join(row) + (f",test-primary-{row[2]}" if with_subscription_keys else "") + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    captured_requests = []
    original_send = httpx.AsyncClient.send

    async def capture_send(self, request, *args, **kwargs):
        if str(request.url).startswith(url):
            captured_requests.append((request.method, request.headers))
        return await original_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", capture_send)
    caplog.set_level(logging.INFO, logger="agents.server")
    identities = load_identities(path)
    rng = random.Random(seed)
    expected = [
        (rng.choice(identities) if identity_mode == "random" else identities[index % 3]).headers()
        for index in range(8)
    ]
    args = Namespace(
        seed=seed,
        identities_file=path,
        identity_mode=identity_mode,
        document_sections=1,
        random=True,
        prompt=None,
        document=[],
        requests=8,
        concurrency=4,
        interval=0,
        timeout=10,
        output=None,
    )
    for _ in range(2):
        captured_requests.clear()
        prompts = iter(f"CSV request {index}" for index in range(8))
        monkeypatch.setattr(
            "clients.common.random_prompt", lambda *args, prompts=prompts: next(prompts)
        )
        assert await run(args, config.kind) == 0
        assert [
            without_correlation(model.request_kwargs[f"CSV request {index}"]["extra_headers"])
            for index in range(8)
        ] == expected
        assert all("X-Agent" not in headers for headers in expected)
        card_headers = [headers for method, headers in captured_requests if method == "GET"]
        runtime_headers = [headers for method, headers in captured_requests if method == "POST"]
        assert len(card_headers) == 1 and len(runtime_headers) == 8
        if with_subscription_keys:
            assert (
                card_headers[0]["Ocp-Apim-Subscription-Key"]
                == expected[0]["Ocp-Apim-Subscription-Key"]
            )
            for headers in runtime_headers:
                assert headers["Ocp-Apim-Subscription-Key"] == f"test-primary-{headers['X-User']}"
            assert "test-primary-" not in caplog.text
        else:
            assert all(
                "Ocp-Apim-Subscription-Key" not in headers for _, headers in captured_requests
            )


@pytest.mark.parametrize("card_url_mode", ["well-known", "custom"])
async def test_single_request_prints_input_and_streamed_output(
    live_agent, tmp_path, capsys, monkeypatch, card_url_mode
):
    url, _, config = live_agent
    other_kind = "customer" if config.kind == "risk" else "risk"
    monkeypatch.setenv(f"{other_kind.upper()}_AGENT_CARD_URL", url + "/wrong-agent-card")
    if card_url_mode == "custom":
        monkeypatch.setenv(
            f"{config.kind.upper()}_AGENT_CARD_URL",
            url + "/gateway/agent-card.json?source=apim",
        )
    document = tmp_path / "informe.txt"
    document.write_text("Documento sintetico", encoding="utf-8")
    output = tmp_path / "single.jsonl"
    args = Namespace(
        seed=42,
        identities_file=None,
        identity_mode="sequence",
        document_sections=60,
        random=False,
        prompt="Analiza esta consulta",
        document=[document],
        requests=1,
        concurrency=1,
        interval=0,
        timeout=10,
        output=output,
    )
    assert await run(args, config.kind) == 0
    record = json.loads(output.read_text())
    assert record["input"] == with_documents(args.prompt, [document])
    assert record["output"] == f"Respuesta: {record['input']}"
    captured = capsys.readouterr()
    assert f"[{config.kind} #1] Peticion:\n{record['input']}\n" in captured.out
    assert f"[{config.kind} #1] Respuesta:\n{record['output']}" in captured.out
    assert record["output"] in captured.out


async def test_configured_card_url_failure_does_not_fall_back(live_agent, monkeypatch):
    url, model, config = live_agent
    monkeypatch.setenv(f"{config.kind.upper()}_AGENT_CARD_URL", url + "/missing-card.json")
    args = Namespace(
        seed=42,
        identities_file=None,
        identity_mode="sequence",
        document_sections=60,
        random=False,
        prompt="Hola",
        document=[],
        requests=1,
        concurrency=1,
        interval=0,
        timeout=10,
        output=None,
    )
    with pytest.raises(A2AClientError, match="404"):
        await run(args, config.kind)
    assert model.prompts == []


@pytest.mark.parametrize("card_url", [None, "", "   "])
async def test_card_url_is_required_without_legacy_discovery(live_agent, monkeypatch, card_url):
    url, model, config = live_agent
    variable = f"{config.kind.upper()}_AGENT_CARD_URL"
    monkeypatch.setenv(f"{config.kind.upper()}_AGENT_URL", url)
    other_kind = "customer" if config.kind == "risk" else "risk"
    monkeypatch.setenv(f"{other_kind.upper()}_AGENT_CARD_URL", url + "/gateway/agent-card.json")
    if card_url is None:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, card_url)
    with pytest.raises(ValueError, match=f"{variable} must contain the full Agent Card URL"):
        await run(Namespace(), config.kind)
    assert model.prompts == []


@pytest.mark.parametrize("kind", ["risk", "customer"])
def test_client_reports_missing_card_url(kind, monkeypatch, caplog):
    monkeypatch.setattr("clients.common.load_dotenv", lambda: None)
    monkeypatch.delenv(f"{kind.upper()}_AGENT_CARD_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["client", "--prompt", "Hola"])
    with pytest.raises(SystemExit) as exc:
        main(kind)
    assert exc.value.code == 1
    assert f"{kind.upper()}_AGENT_CARD_URL must contain the full Agent Card URL" in caplog.text


def test_client_rejects_legacy_url_option(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["client", "--prompt", "Hola", "--url", "http://localhost"])
    with pytest.raises(SystemExit) as exc:
        main("risk")
    assert exc.value.code == 2
    assert "unrecognized arguments: --url" in capsys.readouterr().err


async def test_failed_load_returns_nonzero(live_agent, tmp_path, capsys):
    url, _, config = live_agent
    output = tmp_path / "failed-load.jsonl"
    args = Namespace(
        seed=42,
        identities_file=None,
        identity_mode="sequence",
        document_sections=60,
        random=False,
        prompt="FAIL",
        document=[],
        requests=2,
        concurrency=1,
        interval=0,
        timeout=10,
        output=output,
    )
    assert await run(args, config.kind) == 1
    captured = capsys.readouterr()
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert all(r["input"] == "FAIL" and r["output"] == "" for r in records)
    assert all(r["status"] == "failed" and r["error"] for r in records)
    for index in (1, 2):
        assert f"[{config.kind} #{index}] Peticion:\nFAIL" in captured.out
        assert f"[{config.kind} #{index}] Respuesta parcial (error):" in captured.out
        assert f"[{config.kind} #{index}] Error:" in captured.out
    assert '"failures": 2' in captured.err


async def test_partial_output_is_kept_on_failure(monkeypatch, tmp_path, capsys):
    class FakeClient:
        async def close(self):
            pass

    async def get_card(*args, **kwargs):
        return object()

    def connect(*args, **kwargs):
        return FakeClient()

    async def fail_after_text(client, prompt, on_text, *, headers=None):
        assert "[customer #1] Peticion:\nConsulta" in capsys.readouterr().out
        on_text("Respuesta parcial")
        raise RuntimeError("Stream interrupted")

    monkeypatch.setenv("CUSTOMER_AGENT_CARD_URL", "http://localhost/agent-card.json")
    monkeypatch.setattr(A2ACardResolver, "get_agent_card", get_card)
    monkeypatch.setattr(ClientFactory, "create", connect)
    monkeypatch.setattr("clients.common.stream_request", fail_after_text)
    output = tmp_path / "failed.jsonl"
    args = Namespace(
        seed=42,
        identities_file=None,
        identity_mode="sequence",
        document_sections=60,
        random=False,
        prompt="Consulta",
        document=[],
        requests=1,
        concurrency=1,
        interval=0,
        timeout=10,
        output=output,
    )
    assert await run(args, "customer") == 1
    record = json.loads(output.read_text())
    assert record["input"] == "Consulta"
    assert record["output"] == "Respuesta parcial"
    assert record["output_chars"] == len("Respuesta parcial")
    assert record["status"] == "failed"
    assert "Stream interrupted" in record["error"]
    assert "Respuesta parcial" in capsys.readouterr().out


async def test_request_timeout_is_reported(live_agent, capsys):
    url, _, config = live_agent
    args = Namespace(
        seed=42,
        identities_file=None,
        identity_mode="sequence",
        document_sections=60,
        random=False,
        prompt="Hola",
        document=[],
        requests=1,
        concurrency=1,
        interval=0,
        timeout=0.05,
        output=None,
    )
    assert await run(args, config.kind) == 1
    assert '"failures": 1' in capsys.readouterr().err


async def test_reasoning_option_reaches_model():
    model = FakeChatClient()
    config = settings(reasoning_effort="high")
    await build_agent(config, model).run("Hola")
    assert model.options[0]["reasoning"] == {"effort": "high"}


async def test_real_openai_sdk_lifespan_without_inference():
    app = create_app(settings(auth_mode="cli"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as http:
            assert (await http.get("/healthz")).json()["status"] == "ok"


def test_workload_profiles_and_reproducibility():
    risk = random_prompt("risk", random.Random(42))
    customer = random_prompt("customer", random.Random(42))
    assert len(risk) > 30_000
    assert len(customer) < 150
    assert len(risk) > 200 * len(customer)
    assert risk == random_prompt("risk", random.Random(42))
    assert risk != random_prompt("risk", random.Random(43))
    assert risk.count("Seccion ") == 60


def test_documents(tmp_path):
    doc = tmp_path / "example.txt"
    doc.write_text("Synthetic risk document", encoding="utf-8")
    assert "Synthetic risk document" in with_documents("Analyze", [doc])
    with pytest.raises(FileNotFoundError):
        with_documents("Analyze", [tmp_path / "missing.txt"])
    with pytest.raises(ValueError, match="UTF-8"):
        with_documents("Analyze", [tmp_path / "unsupported.pdf"])


def test_environment_settings(monkeypatch):
    monkeypatch.setattr("agents.config.load_dotenv", lambda: None)
    for name in ("OPENAI_BASE_URL", "RISK_OPENAI_BASE_URL", "CUSTOMER_OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for name in (
        "FOUNDRY_PROJECT_ENDPOINT",
        "RISK_FOUNDRY_PROJECT_ENDPOINT",
        "CUSTOMER_FOUNDRY_PROJECT_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    apim_url = "https://finops4ai.azure-api.net/finopsforai/openai/v1"
    monkeypatch.setenv("OPENAI_BASE_URL", apim_url)
    monkeypatch.setenv("AUTH_MODE", "cli")
    risk = Settings.from_env("risk")
    customer = Settings.from_env("customer")
    assert risk.model == "gpt-5.6-sol"
    assert customer.model == "gpt-5.4-mini"
    assert "max_output_tokens" not in risk.model_dump()
    assert "max_output_tokens" not in customer.model_dump()
    assert risk.auth_mode == customer.auth_mode == "cli"
    assert Settings.from_env("risk").openai_base_url == apim_url
    assert Settings.from_env("customer").openai_base_url == apim_url
    monkeypatch.setenv("RISK_OPENAI_BASE_URL", "https://risk.example/openai/v1/")
    assert Settings.from_env("risk").openai_base_url == "https://risk.example/openai/v1"
    assert Settings.from_env("customer").openai_base_url == apim_url
    monkeypatch.setenv("CUSTOMER_OPENAI_BASE_URL", "")
    assert Settings.from_env("customer").openai_base_url == apim_url
    assert credential_for(risk).__class__.__name__ == "AzureCliCredential"
    monkeypatch.setenv("RISK_MODEL_DEPLOYMENT", "custom-premium")
    monkeypatch.setenv("RISK_MAX_OUTPUT_TOKENS", "900")
    monkeypatch.setenv("CUSTOMER_MAX_OUTPUT_TOKENS", "300")
    monkeypatch.setenv("RISK_AUTH_MODE", "managed_identity")
    monkeypatch.setenv("RISK_MANAGED_IDENTITY_CLIENT_ID", "example-client-id")
    risk = Settings.from_env("risk")
    assert risk.model == "custom-premium"
    assert "max_output_tokens" not in risk.model_dump()
    assert "max_output_tokens" not in Settings.from_env("customer").model_dump()
    assert risk.openai_base_url == "https://risk.example/openai/v1"
    assert risk.auth_mode == "managed_identity"
    assert credential_for(risk).__class__.__name__ == "ManagedIdentityCredential"
    assert Settings.from_env("customer").model == "gpt-5.4-mini"
    monkeypatch.setenv("RISK_PORT", "invalid")
    with pytest.raises(ValidationError):
        Settings.from_env("risk")


def test_model_endpoint_is_required(monkeypatch):
    monkeypatch.setattr("agents.config.load_dotenv", lambda: None)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    for kind in ("risk", "customer"):
        monkeypatch.delenv(f"{kind.upper()}_OPENAI_BASE_URL", raising=False)
        with pytest.raises(ValidationError, match="OpenAI base URL"):
            Settings.from_env(kind)
        monkeypatch.setenv(f"{kind.upper()}_OPENAI_BASE_URL", "https://agent.example/openai/v1")
        assert Settings.from_env(kind).openai_base_url == "https://agent.example/openai/v1"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://apim.example/openai/v1",
        "/openai/v1",
        "https://user:password@apim.example/openai/v1",
        "https://apim.example/openai/v1?api-key=secret",
        "https://apim.example/openai/v1#fragment",
    ],
)
def test_invalid_openai_base_url(url):
    with pytest.raises(ValidationError, match="OpenAI base URL"):
        settings(openai_base_url=url)


class ScriptedClient:
    def __init__(self, events):
        self.events = events

    async def send_message(self, request, *, context=None):
        for event in self.events:
            yield event


def artifact(text, append=False):
    return StreamResponse(
        artifact_update=TaskArtifactUpdateEvent(
            artifact=Artifact(artifact_id="one", parts=[A2APart(text=text)]), append=append
        )
    )


def status(state):
    return StreamResponse(status_update=TaskStatusUpdateEvent(status=TaskStatus(state=state)))


async def test_artifact_replacement_and_append():
    client = ScriptedClient(
        [
            artifact("old"),
            artifact("new"),
            artifact(" tail", append=True),
            status(TaskState.TASK_STATE_COMPLETED),
        ]
    )
    assert await stream_request(client, "hello") == "new tail"


@pytest.mark.parametrize(
    "events,match",
    [
        ([artifact("partial")], "without a completed"),
        ([status(TaskState.TASK_STATE_COMPLETED)], "without text"),
        ([status(TaskState.TASK_STATE_REJECTED)], "TASK_STATE_REJECTED"),
        ([status(TaskState.TASK_STATE_CANCELED)], "TASK_STATE_CANCELED"),
    ],
)
async def test_incomplete_or_rejected_stream(events, match):
    with pytest.raises(RuntimeError, match=match):
        await stream_request(ScriptedClient(events), "hello")
