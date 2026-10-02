"""Shared fixtures: a fake upstream that impersonates three kinds of servers, and a client in front of it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from llmswitch.config import Config, parse_config
from llmswitch.gateway import create_app

ANTHROPIC_SSE = (
    b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","usage":{"input_tokens":3,"output_tokens":0}}}\n\n'
    b'event: ping\ndata: {"type":"ping"}\n\n'
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


class Recorder:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]


def make_upstream(rec: Recorder) -> FastAPI:
    up = FastAPI()

    async def record(request: Request) -> dict[str, Any]:
        body = await request.body()
        entry = {
            "host": request.headers.get("host"),
            "path": request.url.path,
            "query": request.url.query,
            "headers": dict(request.headers),
            "raw": body,
            "json": json.loads(body) if body else None,
        }
        rec.requests.append(entry)
        return entry

    @up.post("/v1/messages")
    async def messages(request: Request) -> Response:
        e = await record(request)
        model = e["json"].get("model")
        if model == "bad":
            return JSONResponse(
                {"type": "error", "error": {"type": "invalid_request_error", "message": "upstream says no"}},
                status_code=400,
            )
        if e["json"].get("stream"):

            async def gen():
                for i in range(0, len(ANTHROPIC_SSE), 40):
                    yield ANTHROPIC_SSE[i : i + 40]

            return StreamingResponse(
                gen(),
                media_type="text/event-stream",
                headers={"request-id": "req_x", "anthropic-ratelimit-unified-status": "allowed"},
            )
        return JSONResponse(
            {"id": "msg_1", "model": model, "content": [{"type": "text", "text": "hello"}]},
            headers={"request-id": "req_y"},
        )

    @up.post("/v1/messages/count_tokens")
    async def count(request: Request) -> Response:
        e = await record(request)
        if e["host"].startswith("ollama"):
            return PlainTextResponse("404 page not found", status_code=404)
        return JSONResponse({"input_tokens": 42})

    @up.get("/api/tags")
    async def tags(request: Request) -> Response:
        await record(request)
        return JSONResponse(
            {
                "models": [
                    {
                        "name": "qwen3:14b",
                        "details": {"parameter_size": "14.8B", "family": "qwen3", "context_length": 40960},
                        "capabilities": ["completion", "tools"],
                    },
                    {"name": "shared-model", "details": {}},
                ]
            }
        )

    @up.get("/v1/models")
    async def models(request: Request) -> Response:
        e = await record(request)
        if e["host"].startswith("openai"):
            return JSONResponse(
                {
                    "object": "list",
                    "data": [
                        {"id": "gpt-x", "object": "model", "owned_by": "acme"},
                        {"id": "shared-model", "object": "model"},
                    ],
                }
            )
        return JSONResponse(
            {"data": [{"id": "claude-real", "display_name": "Claude Real", "created_at": "2026-01-01T00:00:00Z"}]}
        )

    @up.post("/v1/chat/completions")
    async def chat(request: Request) -> Response:
        e = await record(request)
        if e["json"].get("stream"):
            chunks = [
                {"id": "c1", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]},
                {"id": "c1", "choices": [{"index": 0, "delta": {"content": "lo"}}]},
                {
                    "id": "c1",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "id": "call_1", "function": {"name": "read", "arguments": ""}}
                                ]
                            },
                        }
                    ],
                },
                {
                    "id": "c1",
                    "choices": [
                        {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path":'}}]}}
                    ],
                },
                {
                    "id": "c1",
                    "choices": [
                        {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]}}
                    ],
                },
                {"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                {"id": "c1", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            ]

            async def gen():
                for c in chunks:
                    yield f"data: {json.dumps(c)}\n\n".encode()
                yield b"data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")
        return JSONResponse(
            {
                "id": "chatcmpl-1",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Hello there"}}
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            }
        )

    return up


HUB_YAML = """
gateway: {port: 11436}
tunnels:
  box: {ssh: box, forward: "11435:localhost:11434"}
providers:
  anthropic:
    protocol: anthropic
    base_url: http://anthropic.test
    auth: passthrough
    match: ["claude-*"]
    models:
      - {id: opus, bare: true, description: alias}
  ollama:
    protocol: anthropic
    base_url: http://ollama.test
    models: ollama
    tunnel: box
    launch:
      env: {HUB_MODEL: "{model}", HUB_CTX: "{details.context_length}"}
  strict:
    protocol: anthropic
    base_url: http://strict.test
    headers: {x-extra: "1"}
    compat:
      drop_fields: [context_management]
      drop_tool_fields: [strict]
      drop_headers: [anthropic-beta]
  openai:
    protocol: openai
    base_url: http://openai.test/v1
    api_key: sk-test
    models: openai
claude:
  env: {HUB_BASE: "always", HUB_PROVIDER: "{provider}"}
"""


@pytest.fixture
def config(tmp_path: Path) -> Config:
    import yaml

    return parse_config(yaml.safe_load(HUB_YAML), tmp_path / "config.yaml")


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
async def client(config: Config, recorder: Recorder):
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=make_upstream(recorder)))
    app = create_app(config, http=upstream)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://client") as client,
    ):
        yield client
    await upstream.aclose()
