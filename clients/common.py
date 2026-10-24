import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.client.client import Client, ClientCallContext
from a2a.client.errors import A2AClientError
from a2a.types import Message, Part, Role, SendMessageRequest, TaskState
from dotenv import load_dotenv

from agents.config import AgentKind
from clients.identities import load_identities
from clients.workloads import random_prompt, with_documents

logger = logging.getLogger(__name__)


@dataclass
class Result:
    request_id: str
    agent: str
    status: str
    input: str
    output: str
    input_chars: int
    output_chars: int
    latency_seconds: float
    first_text_seconds: float | None
    error: str | None = None


async def stream_request(
    client: Client,
    prompt: str,
    on_text: Callable[[str], None] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> str:
    request = SendMessageRequest(
        message=Message(
            message_id=str(uuid4()),
            role=Role.ROLE_USER,
            parts=[Part(text=prompt)],
        )
    )
    artifacts: dict[str, str] = {}
    completed = False
    context = ClientCallContext(service_parameters=headers) if headers is not None else None
    async for event in client.send_message(request, context=context):
        event_type = event.WhichOneof("payload")
        if event_type == "artifact_update":
            update = event.artifact_update
            text = "".join(p.text for p in update.artifact.parts if p.HasField("text"))
            artifact_id = update.artifact.artifact_id
            artifacts[artifact_id] = (
                artifacts.get(artifact_id, "") + text if update.append else text
            )
            if text and on_text is not None:
                on_text(text)
        elif event_type == "message":
            text = "".join(p.text for p in event.message.parts if p.HasField("text"))
            if not text:
                raise RuntimeError("A2A message completed without text")
            if text and on_text is not None:
                on_text(text)
            return text
        elif event_type in {"status_update", "task"}:
            status = (
                event.status_update.status if event_type == "status_update" else event.task.status
            )
            if status.state == TaskState.TASK_STATE_COMPLETED:
                completed = True
                if event_type == "task" and not artifacts:
                    for artifact in event.task.artifacts:
                        text = "".join(p.text for p in artifact.parts if p.HasField("text"))
                        artifacts[artifact.artifact_id] = text
                        if text and on_text is not None:
                            on_text(text)
            elif status.state not in {
                TaskState.TASK_STATE_SUBMITTED,
                TaskState.TASK_STATE_WORKING,
            }:
                detail = " ".join(p.text for p in status.message.parts if p.HasField("text"))
                raise RuntimeError(f"A2A task ended as {TaskState.Name(status.state)}: {detail}")
    if not completed:
        raise RuntimeError("A2A stream ended without a completed task")
    text = "".join(artifacts.values())
    if not text:
        raise RuntimeError("A2A task completed without text")
    return text


async def run(args: argparse.Namespace, kind: AgentKind) -> int:
    identities = load_identities(args.identities_file) if args.identities_file else []
    identity_rng = random.Random(args.seed)
    request_headers = [
        (
            identity_rng.choice(identities)
            if args.identity_mode == "random"
            else identities[index % len(identities)]
        ).headers()
        if identities
        else None
        for index in range(args.requests)
    ]
    rng = random.Random(args.seed)
    prompts = [
        with_documents(
            random_prompt(kind, rng, args.document_sections) if args.random else args.prompt,
            args.document,
        )
        for _ in range(args.requests)
    ]
    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    semaphore = asyncio.Semaphore(args.concurrency)
    results: list[Result] = []
    output = args.output.open("w", encoding="utf-8") if args.output else None
    started = time.perf_counter()

    async def one(index: int, prompt: str, client: Client) -> None:
        async with semaphore:
            if args.interval:
                await asyncio.sleep(args.interval)
            start = time.perf_counter()
            first_text: float | None = None
            chunks: list[str] = []
            print(f"\n[{kind} #{index}] Peticion:\n{prompt}\n", flush=True)
            if not args.random and args.requests == 1:
                print(f"[{kind} #{index}] Respuesta:", flush=True)

            def on_text(text: str) -> None:
                nonlocal first_text
                chunks.append(text)
                if first_text is None:
                    first_text = time.perf_counter() - start
                if not args.random and args.requests == 1:
                    print(text, end="", flush=True)

            error = None
            text = ""
            try:
                async with asyncio.timeout(args.timeout):
                    text = await stream_request(
                        client, prompt, on_text, headers=request_headers[index - 1]
                    )
            except (A2AClientError, httpx.HTTPError, TimeoutError, RuntimeError) as exc:
                logger.error("Request %s failed: %s", index, exc)
                error = f"{type(exc).__name__}: {exc}"
                text = "".join(chunks)
            if args.random or args.requests > 1:
                label = "Respuesta parcial (error)" if error else "Respuesta"
                print(f"\n[{kind} #{index}] {label}:\n{text}\n", flush=True)
            else:
                print(flush=True)
            if error:
                print(f"[{kind} #{index}] Error: {error}", flush=True)
            result = Result(
                request_id=str(index),
                agent=kind,
                status="failed" if error else "completed",
                input=prompt,
                output=text,
                input_chars=len(prompt),
                output_chars=len(text),
                latency_seconds=round(time.perf_counter() - start, 4),
                first_text_seconds=round(first_text, 4) if first_text is not None else None,
                error=error,
            )
            results.append(result)
            record = json.dumps(asdict(result))
            if output:
                output.write(record + "\n")
                output.flush()

    try:
        async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as http:
            factory = ClientFactory(
                ClientConfig(
                    streaming=True, httpx_client=http, supported_protocol_bindings=["JSONRPC"]
                )
            )
            card_url = os.getenv(f"{kind.upper()}_AGENT_CARD_URL", "").strip()
            if card_url:
                card = await A2ACardResolver(http, card_url).get_agent_card(relative_card_path="/")
                client = factory.create(card)
            else:
                client = await factory.create_from_url(args.url)
            try:
                await asyncio.gather(*(one(i + 1, p, client) for i, p in enumerate(prompts)))
            finally:
                await client.close()
    finally:
        if output:
            output.close()
    failures = sum(r.status == "failed" for r in results)
    print(
        json.dumps(
            {
                "summary": {
                    "agent": kind,
                    "requests": len(results),
                    "failures": failures,
                    "elapsed_seconds": round(time.perf_counter() - started, 4),
                    "seed": args.seed,
                }
            }
        ),
        file=sys.stderr,
    )
    return 1 if failures else 0


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def main(kind: AgentKind) -> None:
    load_dotenv()
    prefix = kind.upper()
    parser = argparse.ArgumentParser(description=f"Streaming A2A client: {kind}")
    parser.add_argument(
        "--url",
        default=os.getenv(
            f"{prefix}_AGENT_URL",
            "http://localhost:8001" if kind == "risk" else "http://localhost:8002",
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt", help="Single prompt; response is printed as it streams")
    source.add_argument("--random", action="store_true", help="Generate synthetic load (billable)")
    parser.add_argument("--requests", type=positive_int)
    parser.add_argument("--concurrency", type=positive_int)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--identities-file", type=Path, help="CSV without header: department,team,user"
    )
    parser.add_argument(
        "--identity-mode",
        choices=("random", "sequence"),
        default="sequence",
        help="Select a CSV row per request (default: sequence, cycles at end)",
    )
    parser.add_argument("--document-sections", type=positive_int, default=60)
    parser.add_argument("--document", type=Path, action="append", default=[])
    parser.add_argument("--timeout", type=positive_float, default=240)
    parser.add_argument("--interval", type=positive_float, default=0)
    parser.add_argument(
        "--output", type=Path, help="Existing parent directory; JSONL inputs, outputs and metrics"
    )
    args = parser.parse_args()
    if args.identity_mode == "random" and args.identities_file is None:
        parser.error("--identity-mode random requires --identities-file")
    if args.requests is None:
        args.requests = (3 if kind == "risk" else 100) if args.random else 1
    args.concurrency = args.concurrency or (1 if kind == "risk" else 8)
    if args.document and kind != "risk":
        parser.error("--document is only available for the risk agent")
    if args.prompt is not None and not args.prompt.strip():
        parser.error("--prompt must not be empty")
    logging.basicConfig(level=logging.WARNING)
    try:
        exit_code = asyncio.run(run(args, kind))
    except (A2AClientError, httpx.HTTPError, OSError, ValueError) as exc:
        logger.error("Client failed: %s", exc)
        exit_code = 1
    raise SystemExit(exit_code)
