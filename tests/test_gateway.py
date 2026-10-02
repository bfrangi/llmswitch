from __future__ import annotations

import json

import pytest

from tests.conftest import ANTHROPIC_SSE

pytestmark = pytest.mark.anyio

CLIENT_HEADERS = {
    "authorization": "Bearer oauth-token",
    "x-api-key": "sk-client",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20,interleaved-thinking",
    "x-claude-code-session-id": "s1",
    "content-type": "application/json",
}


async def test_prefixed_model_is_rewritten_and_streamed_verbatim(client, recorder) -> None:
    body = {
        "model": "ollama/qwen3:14b",
        "max_tokens": 5,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await client.post("/v1/messages?beta=true", json=body, headers=CLIENT_HEADERS)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["request-id"] == "req_x"
    assert r.headers["anthropic-ratelimit-unified-status"] == "allowed"
    assert r.content == ANTHROPIC_SSE
    up = recorder.last
    assert up["host"] == "ollama.test" and up["path"] == "/v1/messages" and up["query"] == "beta=true"
    assert up["json"]["model"] == "qwen3:14b"
    # credentials never leak to a provider without passthrough; everything else is forwarded as an open list
    assert "authorization" not in up["headers"] and "x-api-key" not in up["headers"]
    assert up["headers"]["anthropic-beta"] == CLIENT_HEADERS["anthropic-beta"]
    assert up["headers"]["x-claude-code-session-id"] == "s1"


async def test_passthrough_keeps_bytes_and_credentials(client, recorder) -> None:
    raw = b'{"model": "claude-opus-5-5",  "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}'
    r = await client.post("/v1/messages", content=raw, headers=CLIENT_HEADERS)
    assert r.status_code == 200
    assert r.json()["content"][0]["text"] == "hello"
    up = recorder.last
    assert up["host"] == "anthropic.test"
    assert up["raw"] == raw, "unchanged model must forward the client's exact bytes"
    assert up["headers"]["authorization"] == "Bearer oauth-token"
    assert up["headers"]["x-api-key"] == "sk-client"
    assert "host" in up["headers"] and up["headers"]["host"] == "anthropic.test"


async def test_upstream_error_body_is_forwarded_unmodified(client) -> None:
    r = await client.post("/v1/messages", json={"model": "anthropic/bad", "messages": []}, headers=CLIENT_HEADERS)
    assert r.status_code == 400
    assert r.json() == {"type": "error", "error": {"type": "invalid_request_error", "message": "upstream says no"}}


async def test_compat_surgery_and_configured_headers(client, recorder) -> None:
    body = {
        "model": "strict/m",
        "max_tokens": 1,
        "messages": [],
        "context_management": {"edits": []},
        "tools": [{"name": "t", "input_schema": {}, "strict": True}],
    }
    r = await client.post("/v1/messages", json=body, headers=CLIENT_HEADERS)
    assert r.status_code == 200
    up = recorder.last
    assert "context_management" not in up["json"]
    assert up["json"]["tools"] == [{"name": "t", "input_schema": {}}]
    assert "anthropic-beta" not in up["headers"]
    assert up["headers"]["x-extra"] == "1"


async def test_openai_key_is_injected_and_translated(client, recorder) -> None:
    body = {
        "model": "openai/gpt-x",
        "max_tokens": 9,
        "system": "be brief",
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await client.post("/v1/messages", json=body, headers=CLIENT_HEADERS)
    assert r.status_code == 200
    msg = r.json()
    assert msg["type"] == "message" and msg["model"] == "openai/gpt-x"
    assert msg["content"] == [{"type": "text", "text": "Hello there"}]
    assert msg["stop_reason"] == "end_turn" and msg["usage"] == {"input_tokens": 7, "output_tokens": 2}
    up = recorder.last
    assert up["path"] == "/v1/chat/completions"
    assert up["headers"]["authorization"] == "Bearer sk-test" and "x-api-key" not in up["headers"]
    assert up["json"]["messages"][0] == {"role": "system", "content": "be brief"}
    assert up["json"]["model"] == "gpt-x" and up["json"]["max_tokens"] == 9


async def test_openai_stream_becomes_anthropic_events(client) -> None:
    body = {"model": "openai/gpt-x", "max_tokens": 9, "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    r = await client.post("/v1/messages", json=body, headers=CLIENT_HEADERS)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    types = [e["type"] for e in events]
    assert types == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[1]["content_block"]["type"] == "text" and events[5]["content_block"] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "read",
        "input": {},
    }
    assert "".join(e["delta"]["partial_json"] for e in events[6:8]) == '{"path":"a.py"}'
    assert events[9]["delta"]["stop_reason"] == "tool_use" and events[9]["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
    }


async def test_count_tokens(client) -> None:
    r = await client.post(
        "/v1/messages/count_tokens", json={"model": "claude-x", "messages": []}, headers=CLIENT_HEADERS
    )
    assert r.status_code == 200 and r.json() == {"input_tokens": 42}
    r = await client.post(
        "/v1/messages/count_tokens", json={"model": "ollama/qwen3:14b", "messages": []}, headers=CLIENT_HEADERS
    )
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found_error"
    r = await client.post(
        "/v1/messages/count_tokens", json={"model": "openai/gpt-x", "messages": []}, headers=CLIENT_HEADERS
    )
    assert r.status_code == 404 and "OpenAI" in r.json()["error"]["message"]


async def test_model_listing(client) -> None:
    r = await client.get("/v1/models")
    data = r.json()["data"]
    ids = [m["id"] for m in data]
    assert "opus" in ids  # bare alias launches as-is
    assert "ollama/qwen3:14b" in ids and "openai/gpt-x" in ids
    qwen = next(m for m in data if m["id"] == "ollama/qwen3:14b")
    assert "14.8B" in qwen["description"] and "ctx 40k" in qwen["description"] and "tools" in qwen["description"]
    rich = (await client.get("/llmswitch/models")).json()
    assert rich["providers"]["ollama"]["ok"] is True and rich["providers"]["strict"]["ok"] is True
    entry = next(m for m in rich["models"] if m["id"] == "ollama/qwen3:14b")
    assert entry["launch_id"] == "ollama/qwen3:14b" and entry["details"]["context_length"] == 40960


async def test_bare_name_on_two_providers_is_ambiguous(client) -> None:
    r = await client.post("/v1/messages", json={"model": "shared-model", "messages": []}, headers=CLIENT_HEADERS)
    assert r.status_code == 404
    msg = r.json()["error"]["message"]
    assert "ollama/shared-model" in msg and "openai/shared-model" in msg


async def test_errors_and_probes(client) -> None:
    assert (await client.head("/api/hello")).status_code == 200
    assert (await client.get("/healthz")).json()["service"] == "llmswitch"
    r = await client.post("/v1/messages", json={"messages": []})
    assert r.status_code == 400 and "'model' is required" in r.json()["error"]["message"]
    r = await client.post("/v1/messages", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    r = await client.post("/v1/messages", json={"model": "nowhere/x", "messages": []})
    assert r.status_code == 404 and "nowhere/x" in r.json()["error"]["message"]
    r = await client.get("/v1/complete")
    assert r.status_code == 404 and r.json()["type"] == "error"
