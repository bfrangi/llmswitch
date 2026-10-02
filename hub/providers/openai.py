"""Adapter for upstreams that only speak the OpenAI Chat Completions API.

Requests arrive in Anthropic Messages shape and are translated field by field;
responses, streamed or not, are translated back into Anthropic events so the
client cannot tell the difference. Anything the OpenAI schema has no place for
(cache markers, thinking configuration, context management) is dropped.
"""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse, Response, StreamingResponse

from .base import Provider, anthropic_error, dumps, error_type_for_status
from .discovery import discover_models

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

    from ..catalog import ModelEntry
    from .base import Incoming


class TranslationError(ValueError):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


# ------------------------------------------------------------------ request side


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return str(content)


def _image_part(block: dict[str, Any]) -> dict[str, Any] | None:
    src = block.get("source") or {}
    if src.get("type") == "base64" and src.get("data"):
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"},
        }
    if src.get("type") == "url" and src.get("url"):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


def _tool_result_content(content: Any) -> str:
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "image":
                parts.append("[image]")
        return "\n".join(parts)
    return _text_of(content)


def _user_messages(content: Any) -> list[dict[str, Any]]:
    """One Anthropic user turn may hold tool results, which OpenAI wants as separate messages."""
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    tool_msgs: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append({"type": "text", "text": b.get("text", "")})
        elif t == "image":
            part = _image_part(b)
            if part:
                parts.append(part)
        elif t == "tool_result":
            tool_msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id", ""),
                    "content": _tool_result_content(b.get("content")),
                }
            )
        elif t == "document":
            parts.append({"type": "text", "text": "[document omitted: not supported by this provider]"})
    out = tool_msgs
    if parts:
        if all(p["type"] == "text" for p in parts):
            out.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts)})
        else:
            out.append({"role": "user", "content": parts})
    return out


def _assistant_message(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            texts.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            calls.append(
                {
                    "id": b.get("id") or new_id("call"),
                    "type": "function",
                    "function": {
                        "name": b.get("name", ""),
                        "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
                    },
                }
            )
    msg: dict[str, Any] = {"role": "assistant", "content": "\n\n".join(texts) if texts else None}
    if calls:
        msg["tool_calls"] = calls
    if msg["content"] is None and not calls:
        msg["content"] = ""
    return msg


def _is_custom_tool(t: Any) -> bool:
    return isinstance(t, dict) and t.get("type") in (None, "custom") and "name" in t and "input_schema" in t


def _tool(t: dict[str, Any]) -> dict[str, Any]:
    fn: dict[str, Any] = {
        "name": t["name"],
        "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
    }
    if t.get("description"):
        fn["description"] = t["description"]
    return {"type": "function", "function": fn}


def _tool_choice(tc: dict[str, Any]) -> Any:
    kind = tc.get("type")
    if kind == "any":
        return "required"
    if kind == "tool":
        return {"type": "function", "function": {"name": tc.get("name", "")}}
    if kind == "none":
        return "none"
    return "auto"


def anthropic_to_openai(body: dict[str, Any], upstream_model: str) -> dict[str, Any]:
    out: dict[str, Any] = {"model": upstream_model, "messages": []}
    msgs: list[dict[str, Any]] = out["messages"]
    system = _text_of(body.get("system"))
    if system:
        msgs.append({"role": "system", "content": system})
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            raise TranslationError("messages entries must be objects")
        role, content = m.get("role"), m.get("content")
        if role == "user":
            msgs.extend(_user_messages(content))
        elif role == "assistant":
            msgs.append(_assistant_message(content))
        elif role == "system":
            msgs.append({"role": "system", "content": _text_of(content)})
        else:
            raise TranslationError(f"unsupported message role {role!r}")
    if "max_tokens" in body:
        out["max_tokens"] = body["max_tokens"]
    for key in ("temperature", "top_p"):
        if key in body:
            out[key] = body[key]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if body.get("stream"):
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    tools = [_tool(t) for t in body.get("tools") or [] if _is_custom_tool(t)]
    if tools:
        out["tools"] = tools
        tc = body.get("tool_choice")
        if isinstance(tc, dict):
            out["tool_choice"] = _tool_choice(tc)
            if tc.get("disable_parallel_tool_use"):
                out["parallel_tool_calls"] = False
    user_id = (body.get("metadata") or {}).get("user_id")
    if user_id:
        out["user"] = str(user_id)
    return out


# ----------------------------------------------------------------- response side


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"_raw_arguments": str(raw)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _stop_reason(finish_reason: Any, has_tool_calls: bool) -> str:
    if has_tool_calls or finish_reason in ("tool_calls", "function_call"):
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    return "end_turn"


def _usage(u: Any) -> dict[str, int]:
    u = u or {}
    return {"input_tokens": int(u.get("prompt_tokens") or 0), "output_tokens": int(u.get("completion_tokens") or 0)}


def openai_to_anthropic(data: dict[str, Any], model: str) -> dict[str, Any]:
    choice = (data.get("choices") or [{}])[0] or {}
    msg = choice.get("message") or {}
    content: list[dict[str, Any]] = []
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if reasoning:
        content.append({"type": "thinking", "thinking": str(reasoning), "signature": ""})
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    calls = msg.get("tool_calls") or []
    for tc in calls:
        fn = tc.get("function") or {}
        content.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or new_id("toolu"),
                "name": fn.get("name", ""),
                "input": _parse_arguments(fn.get("arguments")),
            }
        )
    return {
        "id": data.get("id") or new_id("msg"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _stop_reason(choice.get("finish_reason"), bool(calls)),
        "stop_sequence": None,
        "usage": _usage(data.get("usage")),
    }


def sse(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


class StreamTranslator:
    """Turns OpenAI chat-completion chunks into the Anthropic event sequence."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.message_id = new_id("msg")
        self.next_index = 0
        self.open: tuple[str, int] | None = None  # (kind, block index)
        self.tool_blocks: dict[Any, dict[str, Any]] = {}
        self.finish_reason: Any = None
        self.usage: dict[str, int] | None = None

    def start(self) -> bytes:
        return sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )

    def _open_block(self, kind: str, block: dict[str, Any]) -> list[bytes]:
        events = self._close_block()
        idx = self.next_index
        self.next_index += 1
        self.open = (kind, idx)
        events.append(sse("content_block_start", {"type": "content_block_start", "index": idx, "content_block": block}))
        return events

    def _close_block(self) -> list[bytes]:
        if not self.open:
            return []
        kind, idx = self.open
        events = []
        if kind == "tool":
            tb = next(t for t in self.tool_blocks.values() if t["block"] == idx)
            if not tb["args"]:
                events.append(
                    sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": idx,
                            "delta": {"type": "input_json_delta", "partial_json": "{}"},
                        },
                    )
                )
        events.append(sse("content_block_stop", {"type": "content_block_stop", "index": idx}))
        self.open = None
        return events

    def _ensure(self, kind: str) -> list[bytes]:
        if self.open and self.open[0] == kind:
            return []
        block = {"type": "text", "text": ""} if kind == "text" else {"type": "thinking", "thinking": ""}
        return self._open_block(kind, block)

    def feed(self, chunk: dict[str, Any]) -> list[bytes]:
        events: list[bytes] = []
        if chunk.get("usage"):
            self.usage = _usage(chunk["usage"])
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                events += self._ensure("thinking")
                events.append(
                    sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": self.open[1],
                            "delta": {"type": "thinking_delta", "thinking": reasoning},
                        },
                    )
                )  # type: ignore[index]
            text = delta.get("content")
            if text:
                events += self._ensure("text")
                events.append(
                    sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": self.open[1],
                            "delta": {"type": "text_delta", "text": text},
                        },
                    )
                )  # type: ignore[index]
            for tc in delta.get("tool_calls") or []:
                key = tc.get("index", 0)
                fn = tc.get("function") or {}
                if key not in self.tool_blocks:
                    tb = {"block": self.next_index, "args": False}
                    block = {
                        "type": "tool_use",
                        "id": tc.get("id") or new_id("toolu"),
                        "name": fn.get("name") or "",
                        "input": {},
                    }
                    events += self._open_block("tool", block)
                    self.tool_blocks[key] = tb
                tb = self.tool_blocks[key]
                args = fn.get("arguments")
                if args:
                    tb["args"] = True
                    events.append(
                        sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": tb["block"],
                                "delta": {"type": "input_json_delta", "partial_json": args},
                            },
                        )
                    )
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        return events

    def finish(self) -> list[bytes]:
        events = self._close_block()
        usage = self.usage or {"input_tokens": 0, "output_tokens": 0}
        events.append(
            sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": _stop_reason(self.finish_reason, bool(self.tool_blocks)),
                        "stop_sequence": None,
                    },
                    "usage": usage,
                },
            )
        )
        events.append(sse("message_stop", {"type": "message_stop"}))
        return events


async def translate_stream(resp: httpx.Response, model: str) -> AsyncIterator[bytes]:
    tr = StreamTranslator(model)
    yield tr.start()
    try:
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if isinstance(chunk, dict) and chunk.get("error"):
                err = chunk["error"]
                message = err.get("message") if isinstance(err, dict) else str(err)
                yield sse("error", {"type": "error", "error": {"type": "api_error", "message": message}})
                return
            for ev in tr.feed(chunk):
                yield ev
    finally:
        await resp.aclose()
    for ev in tr.finish():
        yield ev


def translate_error(status: int, body: bytes, provider: str) -> JSONResponse:
    message = body.decode(errors="replace")[:2000]
    try:
        data = json.loads(body)
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            message = str(err.get("message") or message)
        elif isinstance(err, str):
            message = err
    except ValueError:
        pass
    return anthropic_error(status, f"{provider}: {message}", error_type_for_status(status))


class OpenAIProvider(Provider):
    protocol = "openai"

    async def discover(self) -> list[ModelEntry]:
        return await discover_models(self.spec, self.http, self.auth_headers("authorization"), self.static_entries())

    async def count_tokens(self, inc: Incoming, upstream_model: str) -> Response:
        return anthropic_error(
            404, f"ai-hub: provider '{self.name}' speaks the OpenAI protocol, which has no token counting endpoint"
        )

    async def messages(self, inc: Incoming, upstream_model: str) -> Response:
        if inc.json is None:
            return anthropic_error(400, "ai-hub: request body must be a JSON object")
        try:
            payload = anthropic_to_openai(inc.json, upstream_model)
        except TranslationError as e:
            return anthropic_error(400, f"ai-hub: {e}")
        payload = self.apply_compat(payload)
        headers = [
            (k, v) for k, v in self.upstream_headers(inc, default_auth_header="authorization") if k != "content-type"
        ]
        headers.append(("content-type", "application/json"))
        resp = await self.send("POST", self.url("/chat/completions"), headers, dumps(payload))
        if isinstance(resp, JSONResponse):
            return resp
        if resp.status_code >= 400:
            body = await resp.aread()
            await resp.aclose()
            return translate_error(resp.status_code, body, self.name)
        requested = str(inc.json.get("model") or upstream_model)
        if payload.get("stream"):
            return StreamingResponse(
                translate_stream(resp, requested),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
            )
        body = await resp.aread()
        await resp.aclose()
        try:
            data = json.loads(body)
        except ValueError:
            return anthropic_error(502, f"ai-hub: provider '{self.name}' returned a non-JSON response")
        return JSONResponse(openai_to_anthropic(data, requested))
