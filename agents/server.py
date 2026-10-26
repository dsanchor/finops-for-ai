import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import Event, EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskStatusUpdateEvent,
)
from agent_framework import (
    Agent,
    AgentResponseUpdate,
    AgentSession,
    Message,
    SupportsChatGetResponse,
)
from agent_framework.a2a import A2AExecutor
from agent_framework.openai import OpenAIChatClient, OpenAIChatOptions
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import (
    AzureCliCredential,
    DefaultAzureCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agents.config import AgentKind, Settings
from agents.errors import InvocationFailure, Metadata, failure_details, failure_message
from agents.profiles import PROFILES
from agents.telemetry import ExceptionTelemetry, record_failure

logger = logging.getLogger(__name__)


class UsageExecutor(A2AExecutor):
    def __init__(
        self,
        agent: Agent,
        settings: Settings,
        context: RequestContext,
        telemetry: ExceptionTelemetry | None = None,
    ):
        headers = Headers(context.call_context.state.get("headers", {}))
        invocation = headers.get("X-Invocation-Id")
        if invocation:
            try:
                invocation = str(UUID(invocation))
            except ValueError as exc:
                raise ValueError("X-Invocation-Id must be a UUID") from exc
        self.correlation: Metadata = {
            "invocation_id": invocation or str(uuid4()),
            "correlation_source": "header" if invocation else "generated",
            "model_call_id": str(uuid4()),
        }
        self.metadata: Metadata = {
            "event": "agent_invocation_failed",
            "schema_version": "1",
            **self.correlation,
            "service": headers.get("X-Apim-Service-Id")
            or settings.apim_service_id
            or "(sin dimension)",
            "agent": headers.get("X-Agent") or settings.kind,
            "model": settings.model,
            **{
                d: headers.get(f"X-{d}") or "(sin dimension)"
                for d in ("department", "team", "user")
            },
        }
        self.failure: Metadata = {}
        self.output_started = False
        self.telemetry = telemetry
        model_headers = {
            name: value
            for name in (
                "X-Agent",
                "X-Department",
                "X-Team",
                "X-User",
                "Ocp-Apim-Subscription-Key",
            )
            if (value := headers.get(name)) is not None
        }
        logger.info(
            json.dumps(
                {
                    "event": "request_headers",
                    "agent": settings.kind,
                    "headers": {
                        name: value
                        for name, value in model_headers.items()
                        if name != "Ocp-Apim-Subscription-Key"
                    },
                }
            )
        )
        model_headers["X-Invocation-Id"] = str(self.correlation["invocation_id"])
        model_headers["X-Model-Call-Id"] = str(self.correlation["model_call_id"])
        run_kwargs = {"client_kwargs": {"extra_headers": model_headers}} if model_headers else {}
        super().__init__(agent, stream=True, run_kwargs=run_kwargs)
        self.settings = settings

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        await super().execute(context, CorrelatedEvents(event_queue, self))

    async def _run_stream(self, query: Any, session: AgentSession, updater: TaskUpdater) -> None:
        try:
            await super()._run_stream(query, session, updater)
        except Exception as exc:
            self.failure = failure_details(exc)
            self.failure["partial_output"] = self.output_started
            metadata = {**self.metadata, **self.failure, "task_id": updater.task_id}
            if self.telemetry is not None:
                self.telemetry.record_failure(metadata)
            else:
                record_failure(metadata)
            raise InvocationFailure(failure_message(self.failure)) from None

    async def handle_events(
        self,
        item: Message | AgentResponseUpdate,
        updater: TaskUpdater,
        streamed_artifact_ids: set[str] | None = None,
        default_artifact_id: str | None = None,
    ) -> None:
        for content in item.contents:
            if content.type == "text" and content.text:
                self.output_started = True
            if content.type == "usage":
                profile = PROFILES[self.settings.kind]
                logger.info(
                    json.dumps(
                        {
                            "event": "model_usage",
                            "agent": self.settings.kind,
                            "department": profile.department,
                            "model_deployment": self.settings.model,
                            "task_id": updater.task_id,
                            "usage": content.usage_details,
                        }
                    )
                )
        await super().handle_events(item, updater, streamed_artifact_ids, default_artifact_id)


class CorrelatedEvents(EventQueue):
    def __init__(self, target: EventQueue, executor: UsageExecutor):
        self.target = target
        self.executor = executor

    async def enqueue_event(self, event: Event) -> None:
        if isinstance(event, TaskStatusUpdateEvent):
            event.metadata.update(self.executor.correlation)
            if self.executor.failure:
                event.metadata.update(self.executor.failure)
        await self.target.enqueue_event(event)


class PerRequestExecutor(AgentExecutor):
    """Each request gets an isolated Agent/session; only the model client is shared."""

    def __init__(self, factory: Callable[[RequestContext], AgentExecutor]):
        self.factory = factory

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        await self.factory(context).execute(context, event_queue)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await self.factory(context).cancel(context, event_queue)


def credential_for(settings: Settings) -> AsyncTokenCredential:
    if settings.auth_mode == "cli":
        return AzureCliCredential()
    if settings.auth_mode == "managed_identity":
        return ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
    return DefaultAzureCredential(
        managed_identity_client_id=settings.managed_identity_client_id,
        exclude_interactive_browser_credential=True,
    )


def build_agent(settings: Settings, client: SupportsChatGetResponse) -> Agent:
    profile = PROFILES[settings.kind]
    options: OpenAIChatOptions = {"store": False}
    if settings.reasoning_effort is not None:
        options["reasoning"] = {"effort": settings.reasoning_effort}
    return Agent(
        client=client,
        name=profile.name,
        instructions=profile.instructions,
        default_options=options,
    )


def create_app(
    settings: Settings, executor_factory: Callable[[RequestContext], AgentExecutor] | None = None
) -> Starlette:
    profile = PROFILES[settings.kind]
    card = AgentCard(
        name=profile.name,
        description=f"{profile.department}. {profile.description}",
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                url=f"{settings.public_url}/",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        skills=[
            AgentSkill(
                id=settings.kind,
                name=profile.name,
                description=profile.description,
                tags=[profile.department],
            )
        ],
    )
    client: OpenAIChatClient | None = None
    telemetry: ExceptionTelemetry | None = None

    def make_executor(context: RequestContext) -> AgentExecutor:
        if executor_factory is not None:
            return executor_factory(context)
        if client is None:
            raise RuntimeError("Model client has not started")
        return UsageExecutor(build_agent(settings, client), settings, context, telemetry)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        nonlocal client, telemetry
        if executor_factory is not None:
            yield
            return
        connection = settings.applicationinsights_connection_string
        telemetry = ExceptionTelemetry(
            connection.get_secret_value() if connection else None, settings.kind
        )
        try:
            async with credential_for(settings) as credential:
                client = OpenAIChatClient(
                    model=settings.model,
                    base_url=settings.openai_base_url,
                    api_key=get_bearer_token_provider(credential, "https://ai.azure.com/.default"),
                )
                client.client.timeout = httpx.Timeout(settings.request_timeout)
                client.client.max_retries = settings.max_retries
                async with client.client:
                    yield
                client = None
        finally:
            await asyncio.to_thread(telemetry.close)
            telemetry = None

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "agent": settings.kind})

    handler = DefaultRequestHandler(
        agent_executor=PerRequestExecutor(make_executor),
        task_store=InMemoryTaskStore(),
        agent_card=card,
        validate_input_modes=True,
    )
    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/healthz", health),
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, "/"),
        ],
    )


def main(kind: AgentKind) -> None:
    settings = Settings.from_env(kind)
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
