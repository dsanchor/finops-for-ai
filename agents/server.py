import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager

import httpx
import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from agent_framework import Agent, AgentResponseUpdate, Message, SupportsChatGetResponse
from agent_framework.a2a import A2AExecutor
from agent_framework.foundry import FoundryChatClient, FoundryChatOptions
from azure.ai.projects.aio import AIProjectClient
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import (
    AzureCliCredential,
    DefaultAzureCredential,
    ManagedIdentityCredential,
)
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agents.config import AgentKind, Settings
from agents.profiles import PROFILES

logger = logging.getLogger(__name__)


class UsageExecutor(A2AExecutor):
    def __init__(self, agent: Agent, settings: Settings, context: RequestContext):
        headers = Headers(context.call_context.state.get("headers", {}))
        agent_header = headers.get("x-agent")
        run_kwargs = (
            {"client_kwargs": {"extra_headers": {"X-Agent": agent_header}}}
            if agent_header is not None
            else {}
        )
        super().__init__(agent, stream=True, run_kwargs=run_kwargs)
        self.settings = settings

    async def handle_events(
        self,
        item: Message | AgentResponseUpdate,
        updater: TaskUpdater,
        streamed_artifact_ids: set[str] | None = None,
        default_artifact_id: str | None = None,
    ) -> None:
        for content in item.contents:
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
    options: FoundryChatOptions = {"store": False}
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
    client: FoundryChatClient | None = None

    def make_executor(context: RequestContext) -> AgentExecutor:
        if executor_factory is not None:
            return executor_factory(context)
        if client is None:
            raise RuntimeError("Foundry client has not started")
        return UsageExecutor(build_agent(settings, client), settings, context)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        nonlocal client
        if executor_factory is not None:
            yield
            return
        async with credential_for(settings) as credential:
            project_options = {"api_version": settings.api_version} if settings.api_version else {}
            async with AIProjectClient(
                endpoint=settings.project_endpoint,
                credential=credential,
                allow_preview=settings.allow_preview,
                **project_options,
            ) as project:
                client = FoundryChatClient(project_client=project, model=settings.model)
                if settings.openai_base_url is not None:
                    client.client.base_url = settings.openai_base_url
                client.client.timeout = httpx.Timeout(settings.request_timeout)
                client.client.max_retries = settings.max_retries
                async with client.client:
                    yield
                client = None

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
