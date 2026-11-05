"""The native Gemini provider: translation both ways, thought signatures, and the gateway path.

Upstream shapes here are copied from real Gemini API responses (gemini-3.8-flash).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from llmswitch.config import parse_config
from llmswitch.gateway import create_app
from llmswitch.providers.base import TranslationError
from llmswitch.providers.events import MessageBuilder, MessageStream
from llmswitch.providers.gemini import (
    PLACEHOLDER_SIGNATURE,
    ResponseTranslator,
    anthropic_to_gemini,
    count_tokens_request,
    gemini_to_anthropic,
    model_resource,
    thinking_config,
    unwrap_signature,
    wrap_signature,
)

SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "additionalProperties": False,
    "properties": {"path": {"type": "string"}, "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
    "required": ["path"],
}

REQUEST: dict[str, Any] = {
    "model": "gemini-key/gemini-x",
    "max_tokens": 32000,
    "temperature": 0.5,
    "top_p": 0.9,
    "top_k": 40,
    "stop_sequences": ["END"],
    "system": [
        {"type": "text", "text": "sys A", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "sys B"},
    ],
    "thinking": {"type": "adaptive", "display": "omitted"},
    "output_config": {"effort": "xhigh"},
    "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
    "metadata": {"user_id": "u1"},
    "tools": [
        {"name": "read", "description": "read a file", "input_schema": SCHEMA, "strict": True},
        {"type": "web_search_20260209", "name": "web_search"},
    ],
    "tool_choice": {"type": "auto"},
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "IMG"}},
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "PDF"}},
                {"type": "image", "source": {"type": "url", "url": "https://x.test/logo.png"}},
            ],
        },
        {"role": "system", "content": "# Environment\nlinux"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "anthropic-signature"},  # from a Claude model
                {"type": "thinking", "thinking": "summary", "signature": wrap_signature("SIG1")},
                {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": "a.py"}},
                {"type": "tool_use", "id": "toolu_2", "name": "read", "input": {"path": "b.png"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "print(1)"},
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_2",
                    "content": [
                        {"type": "text", "text": "an image"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "PNG"}},
                    ],
                },
                {"type": "text", "text": "and now?"},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Done."},
                {"type": "thinking", "thinking": "", "signature": wrap_signature("SIG2")},
            ],
        },
        {"role": "user", "content": "thanks"},
    ],
}


# ------------------------------------------------------------------ request side


def test_request_translation() -> None:
    out = anthropic_to_gemini(REQUEST)
    assert set(out) == {"contents", "systemInstruction", "tools", "toolConfig", "generationConfig"}
    assert out["systemInstruction"] == {
        "parts": [{"text": "sys A"}, {"text": "sys B"}, {"text": "# Environment\nlinux"}]
    }
    assert out["generationConfig"] == {
        "maxOutputTokens": 32000,
        "temperature": 0.5,
        "topP": 0.9,
        "topK": 40,
        "stopSequences": ["END"],
        "thinkingConfig": {"thinkingLevel": "high"},
    }
    assert out["tools"] == [
        {"functionDeclarations": [{"name": "read", "description": "read a file", "parametersJsonSchema": SCHEMA}]}
    ], "client tools pass with their schema untouched; Anthropic server tools are left out"
    assert out["toolConfig"] == {"functionCallingConfig": {"mode": "AUTO"}}

    user, model, results, answer, thanks = out["contents"]
    assert user == {
        "role": "user",
        "parts": [
            {"text": "look"},
            {"inlineData": {"mimeType": "image/jpeg", "data": "IMG"}},
            {"inlineData": {"mimeType": "application/pdf", "data": "PDF"}},
            {"fileData": {"fileUri": "https://x.test/logo.png", "mimeType": "image/png"}},
        ],
    }
    assert model == {
        "role": "model",
        "parts": [
            {"functionCall": {"id": "toolu_1", "name": "read", "args": {"path": "a.py"}}, "thoughtSignature": "SIG1"},
            {"functionCall": {"id": "toolu_2", "name": "read", "args": {"path": "b.png"}}},
        ],
    }, "the foreign signature and the thought summary are dropped; only the first parallel call is signed"
    assert results == {
        "role": "user",
        "parts": [
            {"functionResponse": {"id": "toolu_1", "name": "read", "response": {"output": "print(1)"}}},
            {"functionResponse": {"id": "toolu_2", "name": "read", "response": {"output": "an image"}}},
            {"inlineData": {"mimeType": "image/png", "data": "PNG"}},
            {"text": "and now?"},
        ],
    }
    assert answer == {"role": "model", "parts": [{"text": "Done."}, {"text": "", "thoughtSignature": "SIG2"}]}
    assert thanks == {"role": "user", "parts": [{"text": "thanks"}]}


def test_unsigned_call_gets_the_placeholder() -> None:
    body = {
        "messages": [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "id": "t1", "name": "ls", "input": {}},
                    {"type": "tool_use", "id": "t2", "name": "ls", "input": {}},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]},
        ]
    }
    parts = anthropic_to_gemini(body)["contents"][1]["parts"]
    assert [p.get("thoughtSignature") for p in parts] == [None, PLACEHOLDER_SIGNATURE, None]


def test_error_results_and_unknown_tool_ids() -> None:
    body = {
        "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "ls", "input": {}}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True}],
            },
        ]
    }
    assert anthropic_to_gemini(body)["contents"][1]["parts"][0]["functionResponse"]["response"] == {"error": "boom"}
    body["messages"][1]["content"][0]["tool_use_id"] = "t9"
    with pytest.raises(TranslationError, match="t9"):
        anthropic_to_gemini(body)
    with pytest.raises(TranslationError, match="role"):
        anthropic_to_gemini({"messages": [{"role": "tool", "content": "x"}]})


@pytest.mark.parametrize(
    ("thinking", "effort", "expected"),
    [
        ({"type": "adaptive", "display": "omitted"}, "low", {"thinkingLevel": "low"}),
        ({"type": "adaptive"}, "medium", {"thinkingLevel": "medium", "includeThoughts": True}),
        ({"type": "adaptive"}, "max", {"thinkingLevel": "high", "includeThoughts": True}),
        ({"type": "adaptive"}, None, {"includeThoughts": True}),
        ({"type": "enabled", "budget_tokens": 2048}, None, {"thinkingBudget": 2048, "includeThoughts": True}),
        ({"type": "disabled"}, None, {"thinkingLevel": "low"}),
        (None, None, None),
    ],
)
def test_thinking_config(thinking, effort, expected) -> None:
    body: dict[str, Any] = {"messages": []}
    if thinking:
        body["thinking"] = thinking
    if effort:
        body["output_config"] = {"effort": effort}
    assert thinking_config(body) == expected


def test_models_without_thinking_get_no_thinking_config() -> None:
    body = {"max_tokens": 10, "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}, "messages": []}
    assert anthropic_to_gemini(body, thinking=False)["generationConfig"] == {"maxOutputTokens": 10}


def test_tool_choice_variants() -> None:
    base = {"messages": [], "tools": [{"name": "t", "input_schema": {}}]}

    def cfg(tc: dict[str, Any]) -> Any:
        return anthropic_to_gemini({**base, "tool_choice": tc})["toolConfig"]["functionCallingConfig"]

    assert cfg({"type": "any"}) == {"mode": "ANY"}
    assert cfg({"type": "tool", "name": "t"}) == {"mode": "ANY", "allowedFunctionNames": ["t"]}
    assert cfg({"type": "none"}) == {"mode": "NONE"}
    assert "toolConfig" not in anthropic_to_gemini({"messages": [], "tool_choice": {"type": "any"}})


def test_count_tokens_request_and_resources() -> None:
    req = count_tokens_request(REQUEST, "gemini-x")["generateContentRequest"]
    assert req["model"] == "models/gemini-x" and "generationConfig" not in req and req["contents"]
    assert model_resource("tunedModels/mine") == "tunedModels/mine"


def test_signature_envelope() -> None:
    assert unwrap_signature(wrap_signature("abc")) == "abc"
    assert unwrap_signature("EqQBCkgIARAB") is None and unwrap_signature(None) is None


# ----------------------------------------------------------------- response side

TOOL_RESPONSE = {
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [
                    {"text": "Let me check.", "thought": True},
                    {
                        "functionCall": {"name": "read", "args": {"path": "a.py"}, "id": "call_1"},
                        "thoughtSignature": "SIG",
                    },
                    {"functionCall": {"name": "read", "args": {"path": "b.py"}, "id": "call_2"}},
                ],
            },
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 128,
        "cachedContentTokenCount": 100,
        "candidatesTokenCount": 16,
        "thoughtsTokenCount": 129,
    },
    "responseId": "abc",
}

# A real streamed answer: thought summaries, text, then the signature on an empty final part.
TEXT_STREAM = [
    {"candidates": [{"content": {"role": "model", "parts": [{"text": "Hmm, 1001 = 7*11*13", "thought": True}]}}]},
    {"candidates": [{"content": {"role": "model", "parts": [{"text": "No, 1001 is "}]}}]},
    {"candidates": [{"content": {"role": "model", "parts": [{"text": "not prime."}]}}]},
    {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "", "thoughtSignature": "TSIG"}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 8, "thoughtsTokenCount": 50},
    },
]


def test_response_translation() -> None:
    msg = gemini_to_anthropic(TOOL_RESPONSE, "gemini-key/gemini-x")
    assert msg["id"] == "msg_abc" and msg["model"] == "gemini-key/gemini-x" and msg["stop_reason"] == "tool_use"
    thinking, first, second = msg["content"]
    assert thinking == {"type": "thinking", "thinking": "Let me check.", "signature": wrap_signature("SIG")}
    assert first["type"] == "tool_use" and first["name"] == "read" and first["input"] == {"path": "a.py"}
    assert second["input"] == {"path": "b.py"} and first["id"].startswith("toolu_") and first["id"] != second["id"]
    assert msg["usage"] == {"input_tokens": 28, "cache_read_input_tokens": 100, "output_tokens": 145}


@pytest.mark.parametrize(
    ("data", "stop"),
    [
        ({"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "MAX_TOKENS"}]}, "max_tokens"),
        ({"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "STOP"}]}, "end_turn"),
        ({"candidates": [{"finishReason": "SAFETY"}]}, "refusal"),
        ({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}, "refusal"),
    ],
)
def test_stop_reasons(data, stop) -> None:
    assert gemini_to_anthropic(data, "m")["stop_reason"] == stop


def test_round_trip_puts_every_signature_back_where_gemini_put_it() -> None:
    msg = gemini_to_anthropic(TOOL_RESPONSE, "m")
    ids = [b["id"] for b in msg["content"] if b["type"] == "tool_use"]
    body = {
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": msg["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": "ok"} for i in ids]},
        ]
    }
    replayed = anthropic_to_gemini(body)["contents"][1]["parts"]
    assert [p.get("thoughtSignature") for p in replayed] == ["SIG", None]
    assert [p["functionCall"]["args"] for p in replayed] == [{"path": "a.py"}, {"path": "b.py"}]


def events_of(chunks: list[bytes]) -> list[dict]:
    return [
        json.loads(line[5:]) for chunk in chunks for line in chunk.decode().splitlines() if line.startswith("data:")
    ]


def test_stream_translation() -> None:
    out = MessageStream("m")
    tr = ResponseTranslator(out)
    chunks = [out.start()]
    for c in TEXT_STREAM:
        chunks += tr.feed(c)
    chunks += out.finish(tr.stop_reason, tr.usage)
    ev = events_of(chunks)
    assert [(e["type"], e.get("index"), (e.get("delta") or e.get("content_block") or {}).get("type")) for e in ev] == [
        ("message_start", None, None),
        ("content_block_start", 0, "thinking"),
        ("content_block_delta", 0, "thinking_delta"),
        ("content_block_stop", 0, None),
        ("content_block_start", 1, "text"),
        ("content_block_delta", 1, "text_delta"),
        ("content_block_delta", 1, "text_delta"),
        ("content_block_stop", 1, None),
        ("message_delta", None, None),
        ("message_stop", None, None),
    ], "the signature on the empty final part has nothing to precede, so the answer stays last"
    assert ev[8]["delta"]["stop_reason"] == "end_turn"
    assert ev[8]["usage"] == {"input_tokens": 20, "cache_read_input_tokens": 0, "output_tokens": 58}
    for chunk in chunks:
        assert chunk.startswith(b"event: ") and chunk.endswith(b"\n\n")


def test_streamed_and_whole_answers_are_the_same_blocks() -> None:
    whole = MessageBuilder()
    tr = ResponseTranslator(whole)
    for c in TEXT_STREAM:
        tr.feed(c)
    whole.close()
    assert whole.content == [
        {"type": "thinking", "thinking": "Hmm, 1001 = 7*11*13", "signature": ""},
        {"type": "text", "text": "No, 1001 is not prime."},
    ]


def test_signature_on_a_text_part_goes_before_it() -> None:
    data = {"candidates": [{"content": {"parts": [{"text": "Hi.", "thoughtSignature": "S"}]}, "finishReason": "STOP"}]}
    content = gemini_to_anthropic(data, "m")["content"]
    assert content == [
        {"type": "thinking", "thinking": "", "signature": wrap_signature("S")},
        {"type": "text", "text": "Hi."},
    ]
    replayed = anthropic_to_gemini({"messages": [{"role": "assistant", "content": content}]})
    assert replayed["contents"][0]["parts"] == [{"text": "Hi.", "thoughtSignature": "S"}]


# ---------------------------------------------------------------- through the gateway


class Upstream:
    """A fake Gemini API: two pages of models, and the three model methods."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.app = FastAPI()
        app = self.app

        @app.get("/v1beta/models")
        async def models(request: Request) -> Response:
            self.requests.append(
                {"path": request.url.path, "params": dict(request.query_params), "headers": dict(request.headers)}
            )
            if request.query_params.get("pageToken") != "p2":
                return JSONResponse(
                    {
                        "models": [
                            {
                                "name": "models/gemini-x",
                                "displayName": "Gemini X",
                                "inputTokenLimit": 1048576,
                                "outputTokenLimit": 65536,
                                "supportedGenerationMethods": ["generateContent", "countTokens"],
                                "thinking": True,
                            },
                            {"name": "models/embedder", "supportedGenerationMethods": ["embedContent"]},
                        ],
                        "nextPageToken": "p2",
                    }
                )
            return JSONResponse(
                {
                    "models": [
                        {
                            "name": "models/plain",
                            "displayName": "Plain",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                }
            )

        @app.post("/v1beta/models/{call}")
        async def call(call: str, request: Request) -> Response:
            body = await request.body()
            entry = {
                "call": call,
                "query": request.url.query,
                "headers": dict(request.headers),
                "json": json.loads(body),
            }
            self.requests.append(entry)
            model, method = call.split(":")
            if model == "missing":
                return JSONResponse(
                    {"error": {"code": 404, "message": "models/missing is not found", "status": "NOT_FOUND"}},
                    status_code=404,
                )
            if method == "countTokens":
                return JSONResponse({"totalTokens": 128})
            if method == "streamGenerateContent":

                async def gen():
                    for c in TEXT_STREAM:
                        yield f"data: {json.dumps(c)}\r\n\r\n".encode()

                return StreamingResponse(gen(), media_type="text/event-stream")
            return JSONResponse(TOOL_RESPONSE)


@pytest.fixture
async def gemini():
    up = Upstream()
    cfg = parse_config(
        {
            "providers": {
                "gemini-key": {
                    "protocol": "gemini",
                    "base_url": "https://gemini.test/v1beta",
                    "api_key": "AIza-test",
                    "models": "gemini",
                }
            }
        },
        Path("/tmp/test_gemini.yaml"),
    )
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=up.app))
    app = create_app(cfg, http=upstream)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://client") as client,
    ):
        yield client, up
    await upstream.aclose()


CLIENT_HEADERS = {
    "authorization": "Bearer oauth-token",
    "x-api-key": "sk-client",
    "anthropic-version": "2023-06-01",
    "accept": "application/json",
    "accept-encoding": "br, zstd",
}


@pytest.mark.anyio
async def test_discovery_pages_and_filters(gemini) -> None:
    client, up = gemini
    models = (await client.get("/llmswitch/models")).json()["models"]
    assert [m["id"] for m in models] == ["gemini-key/gemini-x", "gemini-key/plain"]
    assert models[0]["display_name"] == "Gemini X" and models[0]["description"] == "ctx 1024k · thinking"
    assert models[0]["details"] == {"context_length": 1048576, "max_output_tokens": 65536, "thinking": True}
    assert up.requests[0]["headers"]["x-goog-api-key"] == "AIza-test"


@pytest.mark.anyio
async def test_messages_use_the_key_and_translate(gemini) -> None:
    client, up = gemini
    body = {**REQUEST, "model": "gemini-key/gemini-x"}
    r = await client.post("/v1/messages", json=body, headers=CLIENT_HEADERS)
    assert r.status_code == 200, r.text
    msg = r.json()
    assert msg["model"] == "gemini-key/gemini-x" and msg["stop_reason"] == "tool_use"
    sent = up.requests[-1]
    assert sent["call"] == "gemini-x:generateContent" and sent["query"] == ""
    h = sent["headers"]
    assert h["x-goog-api-key"] == "AIza-test" and "authorization" not in h and "x-api-key" not in h
    assert h["content-type"] == "application/json" and "br" not in h.get("accept-encoding", "")
    assert sent["json"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}


@pytest.mark.anyio
async def test_models_reporting_no_thinking_get_no_thinking_config(gemini) -> None:
    client, up = gemini
    body = {
        "model": "gemini-key/plain",
        "max_tokens": 5,
        "thinking": {"type": "adaptive"},
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert (await client.post("/v1/messages", json=body, headers=CLIENT_HEADERS)).status_code == 200
    assert up.requests[-1]["json"]["generationConfig"] == {"maxOutputTokens": 5}


@pytest.mark.anyio
async def test_stream_through_the_gateway(gemini) -> None:
    client, up = gemini
    body = {
        "model": "gemini-key/gemini-x",
        "max_tokens": 9,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await client.post("/v1/messages", json=body, headers=CLIENT_HEADERS)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert up.requests[-1]["call"] == "gemini-x:streamGenerateContent" and up.requests[-1]["query"] == "alt=sse"
    events = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    assert events[0]["type"] == "message_start" and events[-1]["type"] == "message_stop"
    text = "".join(e["delta"].get("text", "") for e in events if e["type"] == "content_block_delta")
    assert text == "No, 1001 is not prime."


@pytest.mark.anyio
async def test_count_tokens_and_errors(gemini) -> None:
    client, up = gemini
    r = await client.post(
        "/v1/messages/count_tokens",
        json={"model": "gemini-key/gemini-x", "messages": [{"role": "user", "content": "hi"}]},
        headers=CLIENT_HEADERS,
    )
    assert r.status_code == 200 and r.json() == {"input_tokens": 128}
    sent = up.requests[-1]
    assert (
        sent["call"] == "gemini-x:countTokens" and sent["json"]["generateContentRequest"]["model"] == "models/gemini-x"
    )

    r = await client.post("/v1/messages", json={"model": "gemini-key/missing", "messages": []}, headers=CLIENT_HEADERS)
    assert r.status_code == 404
    assert r.json()["error"] == {"type": "not_found_error", "message": "gemini-key: models/missing is not found"}

    bad = {
        "model": "gemini-key/gemini-x",
        "messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t9", "content": "x"}]}],
    }
    r = await client.post("/v1/messages", json=bad, headers=CLIENT_HEADERS)
    assert r.status_code == 400 and "t9" in r.json()["error"]["message"]
