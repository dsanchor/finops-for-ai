import asyncio
import json
import logging
import random
import socket
import time
from argparse import Namespace
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from typing import Any

import httpx
import pytest
import pytest_asyncio
import uvicorn
from a2a.client import ClientConfig, ClientFactory
from a2a.types import (
    Artifact,
    StreamResponse,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.types import Part as A2APart
from agent_framework import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
)
from pydantic import ValidationError

from agents.config import Settings
from agents.profiles import PROFILES
from agents.server import UsageExecutor, build_agent, create_app, credential_for
from clients.common import run, stream_request
from clients.workloads import random_prompt, with_documents


def settings(kind="risk", **overrides) -> Settings:
    return Settings(
        kind=kind,
        project_endpoint="https://demo.services.ai.azure.com/api/projects/demo",
        model="gpt-5.6-sol" if kind == "risk" else "gpt-5.4-mini",
        port=8001 if kind == "risk" else 8002,
        public_url=overrides.pop("public_url", "http://localhost:8001"),
        **overrides,
    )


class FakeChatClient(BaseChatClient):
    def __init__(self):
        super().__init__()
        self.prompts: list[str] = []
        self.options: list[Mapping[str, Any]] = []

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

        async def updates() -> AsyncIterator[ChatResponseUpdate]:
            if prompt == "FAIL":
                raise RuntimeError("Synthetic model failure")
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
async def live_agent(request):
    model = FakeChatClient()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        config = settings(request.param, public_url=url)
        app = create_app(
            config,
            executor_factory=lambda: UsageExecutor(build_agent(config, model), config),
        )
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


async def test_model_failure_is_not_success(live_agent):
    url, _, _ = live_agent
    async with httpx.AsyncClient(timeout=10) as http:
        client = await ClientFactory(ClientConfig(httpx_client=http)).create_from_url(url)
        try:
            with pytest.raises(RuntimeError, match="TASK_STATE_FAILED.*Synthetic model failure"):
                await stream_request(client, "FAIL")
        finally:
            await client.close()


async def test_random_load_and_jsonl(live_agent, tmp_path, capsys):
    url, model, config = live_agent
    output = tmp_path / "results.jsonl"
    args = Namespace(
        seed=7,
        document_sections=60,
        random=True,
        prompt=None,
        document=[],
        requests=4,
        concurrency=2,
        interval=0,
        timeout=10,
        url=url,
        output=output,
    )
    assert await run(args, config.kind) == 0
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


async def test_single_request_prints_input_and_streamed_output(live_agent, tmp_path, capsys):
    url, _, config = live_agent
    document = tmp_path / "informe.txt"
    document.write_text("Documento sintetico", encoding="utf-8")
    output = tmp_path / "single.jsonl"
    args = Namespace(
        seed=42,
        document_sections=60,
        random=False,
        prompt="Analiza esta consulta",
        document=[document],
        requests=1,
        concurrency=1,
        interval=0,
        timeout=10,
        url=url,
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


async def test_failed_load_returns_nonzero(live_agent, tmp_path, capsys):
    url, _, config = live_agent
    output = tmp_path / "failed-load.jsonl"
    args = Namespace(
        seed=42,
        document_sections=60,
        random=False,
        prompt="FAIL",
        document=[],
        requests=2,
        concurrency=1,
        interval=0,
        timeout=10,
        url=url,
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

    async def connect(*args, **kwargs):
        return FakeClient()

    async def fail_after_text(client, prompt, on_text):
        assert "[customer #1] Peticion:\nConsulta" in capsys.readouterr().out
        on_text("Respuesta parcial")
        raise RuntimeError("Stream interrupted")

    monkeypatch.setattr(ClientFactory, "create_from_url", connect)
    monkeypatch.setattr("clients.common.stream_request", fail_after_text)
    output = tmp_path / "failed.jsonl"
    args = Namespace(
        seed=42,
        document_sections=60,
        random=False,
        prompt="Consulta",
        document=[],
        requests=1,
        concurrency=1,
        interval=0,
        timeout=10,
        url="http://localhost",
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
        document_sections=60,
        random=False,
        prompt="Hola",
        document=[],
        requests=1,
        concurrency=1,
        interval=0,
        timeout=0.05,
        url=url,
        output=None,
    )
    assert await run(args, config.kind) == 1
    assert '"failures": 1' in capsys.readouterr().err


async def test_reasoning_option_reaches_model():
    model = FakeChatClient()
    config = settings(reasoning_effort="high")
    await build_agent(config, model).run("Hola")
    assert model.options[0]["reasoning"] == {"effort": "high"}


async def test_real_foundry_sdk_lifespan_without_inference():
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
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT", "https://shared.services.ai.azure.com/api/projects/demo"
    )
    monkeypatch.setenv("AUTH_MODE", "cli")
    risk = Settings.from_env("risk")
    customer = Settings.from_env("customer")
    assert risk.model == "gpt-5.6-sol"
    assert customer.model == "gpt-5.4-mini"
    assert "max_output_tokens" not in risk.model_dump()
    assert "max_output_tokens" not in customer.model_dump()
    assert risk.auth_mode == customer.auth_mode == "cli"
    assert credential_for(risk).__class__.__name__ == "AzureCliCredential"
    monkeypatch.setenv("RISK_MODEL_DEPLOYMENT", "custom-premium")
    monkeypatch.setenv(
        "RISK_FOUNDRY_PROJECT_ENDPOINT", "https://other.services.ai.azure.com/api/projects/other"
    )
    monkeypatch.setenv("RISK_MAX_OUTPUT_TOKENS", "900")
    monkeypatch.setenv("CUSTOMER_MAX_OUTPUT_TOKENS", "300")
    monkeypatch.setenv("RISK_AUTH_MODE", "managed_identity")
    monkeypatch.setenv("RISK_MANAGED_IDENTITY_CLIENT_ID", "example-client-id")
    risk = Settings.from_env("risk")
    assert risk.model == "custom-premium"
    assert "max_output_tokens" not in risk.model_dump()
    assert "max_output_tokens" not in Settings.from_env("customer").model_dump()
    assert risk.project_endpoint.startswith("https://other.")
    assert risk.auth_mode == "managed_identity"
    assert credential_for(risk).__class__.__name__ == "ManagedIdentityCredential"
    assert Settings.from_env("customer").model == "gpt-5.4-mini"
    monkeypatch.setenv("RISK_PORT", "invalid")
    with pytest.raises(ValidationError):
        Settings.from_env("risk")


@pytest.mark.parametrize("url", ["", "http://bad/api/projects/x", "https://bad/openai/v1"])
def test_invalid_foundry_endpoint(url):
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                **settings().model_dump(),
                "project_endpoint": url,
            }
        )


class ScriptedClient:
    def __init__(self, events):
        self.events = events

    async def send_message(self, request):
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
