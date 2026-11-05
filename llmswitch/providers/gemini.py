"""Adapter for Google's Gemini API (``generateContent``), spoken natively.

Requests arrive in Anthropic Messages shape and are translated straight into Gemini's
own format, so nothing has to pass through a third protocol on the way: thinking
levels, tool calls and their thought signatures, PDFs and images, and token counting
all have a direct equivalent. Responses, streamed or not, come back as Anthropic
content blocks.

Thought signatures
------------------
Gemini attaches an opaque ``thoughtSignature`` to some response parts, and refuses the
next request if the signature on a function call is missing ("Function call is missing
a thought_signature") or was not issued by Gemini ("Corrupted thought signature").
Anthropic has a field meant for exactly this kind of value, the ``signature`` of a
thinking block, which clients must send back unchanged. So signatures travel inside
thinking blocks and the gateway keeps no state:

* Gemini to client: a part that carries a signature is preceded by a thinking block
  holding it (the thinking block already open, if there is one). Anthropic messages
  never end in thinking after the answer, so a signature on an empty part after the
  answer (how Gemini ends a streamed text reply) is dropped; Gemini only enforces
  signatures on function calls, and those arrive on the call itself.
* Client to Gemini: a thinking block's signature goes onto the part that follows it,
  or onto an empty text part when nothing follows.

Signatures are tagged with ``SIGNATURE_PREFIX`` so that thinking blocks other models
left in the conversation (a Claude model before a ``/model`` switch, say) are
recognised and left out rather than sent to Gemini. When a model turn's first
function call still ends up without a signature, it gets the placeholder Google
documents for calls Gemini did not produce, which passes validation at some cost in
reasoning continuity.
"""

from __future__ import annotations

import json
import mimetypes
from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse, Response, StreamingResponse

from .base import Provider, TranslationError, anthropic_error, dumps, is_custom_tool, translate_error
from .discovery import discover_models
from .events import MessageBuilder, MessageStream, anthropic_message, iter_sse_json, new_id, stream_error

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

    from ..catalog import ModelEntry
    from ..config import ProviderSpec
    from .base import Incoming
    from .events import BlockWriter

AUTH_HEADER = "x-goog-api-key"
SIGNATURE_PREFIX = "llmswitch.gemini:"
PLACEHOLDER_SIGNATURE = "skip_thought_signature_validator"

# Anthropic's effort scale onto Gemini's thinking levels (Gemini stops at "high").
EFFORT_LEVELS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

REFUSAL_REASONS = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
    }
)

DEFAULT_MEDIA_TYPES = {"image": "image/png", "document": "application/pdf"}


def wrap_signature(signature: str) -> str:
    return SIGNATURE_PREFIX + signature


def unwrap_signature(signature: Any) -> str | None:
    """The Gemini signature inside a thinking block's ``signature``, or None if it is not one."""
    if isinstance(signature, str) and signature.startswith(SIGNATURE_PREFIX):
        return signature[len(SIGNATURE_PREFIX) :] or None
    return None


def model_resource(model: str) -> str:
    """``models/<id>`` for a plain model id; an id that already names a resource stays as it is."""
    return model if "/" in model else f"models/{model}"


# ------------------------------------------------------------------ request side


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content] if content else []
    if isinstance(content, list):
        return [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    return []


def _media_part(block: dict[str, Any]) -> dict[str, Any] | None:
    """An image or document block as a Gemini part (inline data, a file URL, or text)."""
    src = block.get("source") or {}
    kind = src.get("type")
    fallback = DEFAULT_MEDIA_TYPES.get(block.get("type", ""), "application/octet-stream")
    if kind == "base64" and src.get("data"):
        return {"inlineData": {"mimeType": src.get("media_type") or fallback, "data": src["data"]}}
    if kind == "url" and src.get("url"):
        # Gemini only fetches a URL when told its type, which Anthropic URL sources do not carry.
        mime = src.get("media_type") or mimetypes.guess_type(src["url"])[0] or fallback
        return {"fileData": {"fileUri": src["url"], "mimeType": mime}}
    if kind == "text" and src.get("data"):
        return {"text": src["data"]}
    if kind == "content":
        text = "\n\n".join(_texts(src.get("content")))
        return {"text": text} if text else None
    return None


def _function_response(block: dict[str, Any], names: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A ``tool_result`` as a ``functionResponse``, plus any images or files it returned."""
    tool_id = block.get("tool_use_id") or ""
    name = names.get(tool_id)
    if name is None:
        raise TranslationError(f"tool_result {tool_id!r} does not answer any earlier tool_use")
    content = block.get("content")
    texts: list[str] = [content] if isinstance(content, str) else []
    media: list[dict[str, Any]] = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            texts.append(b.get("text", ""))
        elif b.get("type") in ("image", "document"):
            part = _media_part(b)
            if part and "text" in part:
                texts.append(part["text"])
            elif part:
                media.append(part)
    key = "error" if block.get("is_error") else "output"
    response = {"functionResponse": {"id": tool_id, "name": name, "response": {key: "\n".join(texts)}}}
    return response, media


def _user_parts(content: Any, names: dict[str, str]) -> list[dict[str, Any]]:
    """A user turn. Function responses go first, the way Gemini pairs them with the calls."""
    if isinstance(content, str):
        return [{"text": content}] if content else []
    responses: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "text" and b.get("text"):
            rest.append({"text": b["text"]})
        elif kind in ("image", "document"):
            part = _media_part(b)
            if part:
                rest.append(part)
        elif kind == "tool_result":
            response, media = _function_response(b, names)
            responses.append(response)
            rest.extend(media)
    return responses + rest


def _model_parts(content: Any, names: dict[str, str]) -> list[dict[str, Any]]:
    """An assistant turn, with every thought signature back on the part it belongs to."""
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts: list[dict[str, Any]] = []
    pending: str | None = None
    for b in content or []:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "thinking":
            # Gemini's thought summaries are not needed back; the signature is.
            pending = unwrap_signature(b.get("signature")) or pending
            continue
        if kind == "text" and b.get("text"):
            part: dict[str, Any] = {"text": b["text"]}
        elif kind == "tool_use":
            tool_id = b.get("id") or new_id("toolu")
            name = b.get("name", "")
            names[tool_id] = name
            args = b.get("input")
            part = {"functionCall": {"id": tool_id, "name": name, "args": args if isinstance(args, dict) else {}}}
        else:
            continue
        if pending:
            part["thoughtSignature"] = pending
            pending = None
        parts.append(part)
    if pending:
        parts.append({"text": "", "thoughtSignature": pending})
    calls = [p for p in parts if "functionCall" in p]
    if calls and "thoughtSignature" not in calls[0]:
        calls[0]["thoughtSignature"] = PLACEHOLDER_SIGNATURE
    return parts


def _function_declaration(tool: dict[str, Any]) -> dict[str, Any]:
    decl: dict[str, Any] = {
        "name": tool["name"],
        # parametersJsonSchema takes full JSON Schema, so client schemas need no rewriting.
        "parametersJsonSchema": tool.get("input_schema") or {"type": "object", "properties": {}},
    }
    if tool.get("description"):
        decl["description"] = tool["description"]
    return decl


def _tool_config(tool_choice: dict[str, Any]) -> dict[str, Any]:
    kind = tool_choice.get("type")
    if kind == "any":
        cfg: dict[str, Any] = {"mode": "ANY"}
    elif kind == "tool":
        cfg = {"mode": "ANY", "allowedFunctionNames": [tool_choice.get("name", "")]}
    elif kind == "none":
        cfg = {"mode": "NONE"}
    else:
        cfg = {"mode": "AUTO"}
    return {"functionCallingConfig": cfg}


def thinking_config(body: dict[str, Any]) -> dict[str, Any] | None:
    """Anthropic's ``thinking`` and ``output_config.effort`` as a Gemini ``thinkingConfig``."""
    thinking = body.get("thinking") if isinstance(body.get("thinking"), dict) else {}
    output_config = body.get("output_config") if isinstance(body.get("output_config"), dict) else {}
    kind, effort = thinking.get("type"), output_config.get("effort")
    cfg: dict[str, Any] = {}
    if effort in EFFORT_LEVELS:
        cfg["thinkingLevel"] = EFFORT_LEVELS[effort]
    elif kind == "enabled" and isinstance(thinking.get("budget_tokens"), int):
        cfg["thinkingBudget"] = thinking["budget_tokens"]
    elif kind == "disabled":
        cfg["thinkingLevel"] = "low"  # Gemini's thinking models cannot stop thinking; this is the least
    if kind in ("enabled", "adaptive") and thinking.get("display") != "omitted":
        cfg["includeThoughts"] = True
    return cfg or None


def _generation_config(body: dict[str, Any], thinking: bool) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if "max_tokens" in body:
        cfg["maxOutputTokens"] = body["max_tokens"]
    for src, dst in (("temperature", "temperature"), ("top_p", "topP"), ("top_k", "topK")):
        if src in body:
            cfg[dst] = body[src]
    if body.get("stop_sequences"):
        cfg["stopSequences"] = body["stop_sequences"]
    if thinking and (tc := thinking_config(body)):
        cfg["thinkingConfig"] = tc
    return cfg


def anthropic_to_gemini(body: dict[str, Any], *, thinking: bool = True) -> dict[str, Any]:
    """The ``generateContent`` request for an Anthropic Messages request.

    ``thinking=False`` is for models that do not think: they reject any thinking configuration.
    """
    system = _texts(body.get("system"))
    contents: list[dict[str, Any]] = []
    names: dict[str, str] = {}  # tool_use id -> tool name, which the answering functionResponse must repeat
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            raise TranslationError("messages entries must be objects")
        role, content = m.get("role"), m.get("content")
        if role == "system":  # Gemini has a single system slot, so system turns join it
            system += _texts(content)
            continue
        if role == "user":
            parts, gemini_role = _user_parts(content, names), "user"
        elif role == "assistant":
            parts, gemini_role = _model_parts(content, names), "model"
        else:
            raise TranslationError(f"unsupported message role {role!r}")
        if parts:
            contents.append({"role": gemini_role, "parts": parts})
    out: dict[str, Any] = {"contents": contents}
    if system:
        out["systemInstruction"] = {"parts": [{"text": t} for t in system]}
    declarations = [_function_declaration(t) for t in body.get("tools") or [] if is_custom_tool(t)]
    if declarations:
        out["tools"] = [{"functionDeclarations": declarations}]
        if isinstance(body.get("tool_choice"), dict):
            out["toolConfig"] = _tool_config(body["tool_choice"])
    generation = _generation_config(body, thinking)
    if generation:
        out["generationConfig"] = generation
    return out


def count_tokens_request(body: dict[str, Any], model: str) -> dict[str, Any]:
    request = anthropic_to_gemini(body, thinking=False)
    request.pop("generationConfig", None)
    return {"generateContentRequest": {"model": model_resource(model), **request}}


# ----------------------------------------------------------------- response side


def usage_of(meta: dict[str, Any]) -> dict[str, int]:
    """Gemini usage in Anthropic terms: cached input counted apart, thinking counted as output."""

    def n(key: str) -> int:
        return int(meta.get(key) or 0)

    cached = n("cachedContentTokenCount")
    return {
        "input_tokens": n("promptTokenCount") - cached + n("toolUsePromptTokenCount"),
        "cache_read_input_tokens": cached,
        "output_tokens": n("candidatesTokenCount") + n("thoughtsTokenCount"),
    }


class ResponseTranslator:
    """Gemini responses (one, or each chunk of a stream) as Anthropic content blocks."""

    def __init__(self, out: BlockWriter) -> None:
        self.out = out
        self.usage = {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
        self.finish_reason: str | None = None
        self.blocked = False
        self.tool_calls = 0

    @property
    def stop_reason(self) -> str:
        if self.blocked or self.finish_reason in REFUSAL_REASONS:
            return "refusal"
        if self.finish_reason == "MAX_TOKENS":
            return "max_tokens"
        if self.tool_calls:
            return "tool_use"
        return "end_turn"

    def feed(self, response: dict[str, Any]) -> list[bytes]:
        events: list[bytes] = []
        if isinstance(response.get("usageMetadata"), dict):
            self.usage = usage_of(response["usageMetadata"])
        if (response.get("promptFeedback") or {}).get("blockReason"):
            self.blocked = True
        candidates = response.get("candidates") or []
        if candidates:  # only one is ever requested
            candidate = candidates[0]
            for part in (candidate.get("content") or {}).get("parts") or []:
                events += self._part(part)
            if candidate.get("finishReason"):
                self.finish_reason = candidate["finishReason"]
        return events

    def _part(self, part: dict[str, Any]) -> list[bytes]:
        events: list[bytes] = []
        call = part.get("functionCall")
        has_content = isinstance(call, dict) or bool(part.get("text"))
        # A signature goes in a thinking block just before its part's content. One on a part
        # with no content (the empty part a streamed text answer ends with) has nothing to
        # precede, and a message must not end in thinking after its answer, so it is dropped
        # unless a thinking block is still open to take it. Gemini only insists on signatures
        # for function calls, which always have content.
        if part.get("thoughtSignature") and (has_content or self.out.open_type == "thinking"):
            if self.out.open_type != "thinking":
                events += self.out.open({"type": "thinking", "thinking": "", "signature": ""})
            events += self.out.delta({"type": "signature_delta", "signature": wrap_signature(part["thoughtSignature"])})
            events += self.out.close()
        if isinstance(call, dict):
            self.tool_calls += 1
            events += self.out.open(
                {"type": "tool_use", "id": new_id("toolu"), "name": call.get("name", ""), "input": {}}
            )
            args = json.dumps(call.get("args") or {}, ensure_ascii=False)
            events += self.out.delta({"type": "input_json_delta", "partial_json": args})
            events += self.out.close()
        elif part.get("text"):
            if part.get("thought"):
                if self.out.open_type != "thinking":
                    events += self.out.open({"type": "thinking", "thinking": "", "signature": ""})
                events += self.out.delta({"type": "thinking_delta", "thinking": part["text"]})
            else:
                if self.out.open_type != "text":
                    events += self.out.open({"type": "text", "text": ""})
                events += self.out.delta({"type": "text_delta", "text": part["text"]})
        return events


def gemini_to_anthropic(data: dict[str, Any], model: str) -> dict[str, Any]:
    out = MessageBuilder()
    tr = ResponseTranslator(out)
    tr.feed(data)
    out.close()
    message_id = f"msg_{data['responseId']}" if data.get("responseId") else new_id("msg")
    return anthropic_message(message_id, model, out.content, tr.stop_reason, tr.usage)


async def translate_stream(resp: httpx.Response, model: str) -> AsyncIterator[bytes]:
    out = MessageStream(model)
    tr = ResponseTranslator(out)
    yield out.start()
    try:
        async for chunk in iter_sse_json(resp):
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                err = chunk["error"]
                yield stream_error(str(err.get("message")) if isinstance(err, dict) else str(err))
                return
            for ev in tr.feed(chunk):
                yield ev
    finally:
        await resp.aclose()
    for ev in out.finish(tr.stop_reason, tr.usage):
        yield ev


# ---------------------------------------------------------------------- provider


class GeminiProvider(Provider):
    protocol = "gemini"

    def __init__(self, spec: ProviderSpec, http: httpx.AsyncClient) -> None:
        super().__init__(spec, http)
        self._without_thinking: set[str] = set()  # discovered models that reject a thinking configuration

    async def discover(self) -> list[ModelEntry]:
        entries = await discover_models(self.spec, self.http, self.auth_headers(AUTH_HEADER), self.static_entries())
        self._without_thinking = {e.upstream_id for e in entries if e.details.get("thinking") is False}
        return entries

    async def messages(self, inc: Incoming, upstream_model: str) -> Response:
        if inc.json is None:
            return anthropic_error(400, "llmswitch: request body must be a JSON object")
        try:
            payload = anthropic_to_gemini(inc.json, thinking=upstream_model not in self._without_thinking)
        except TranslationError as e:
            return anthropic_error(400, f"llmswitch: {e}")
        stream = bool(inc.json.get("stream"))
        method, query = ("streamGenerateContent", "alt=sse") if stream else ("generateContent", "")
        resp = await self._post(inc, upstream_model, method, self.apply_compat(payload), query)
        if isinstance(resp, Response):
            return resp
        requested = str(inc.json.get("model") or upstream_model)
        if stream:
            return StreamingResponse(
                translate_stream(resp, requested),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
            )
        data = await self._read_json(resp)
        if isinstance(data, Response):
            return data
        return JSONResponse(gemini_to_anthropic(data, requested))

    async def count_tokens(self, inc: Incoming, upstream_model: str) -> Response:
        if inc.json is None:
            return anthropic_error(400, "llmswitch: request body must be a JSON object")
        try:
            payload = count_tokens_request(inc.json, upstream_model)
        except TranslationError as e:
            return anthropic_error(400, f"llmswitch: {e}")
        resp = await self._post(inc, upstream_model, "countTokens", payload)
        if isinstance(resp, Response):
            return resp
        data = await self._read_json(resp)
        if isinstance(data, Response):
            return data
        return JSONResponse({"input_tokens": int(data.get("totalTokens") or 0)})

    async def _post(
        self, inc: Incoming, model: str, method: str, payload: dict[str, Any], query: str = ""
    ) -> httpx.Response | Response:
        """POST to one of the model's methods; an upstream failure comes back as an Anthropic error."""
        headers = self.translated_headers(inc, default_auth_header=AUTH_HEADER)
        url = self.url(f"/{model_resource(model)}:{method}", query)
        resp = await self.send("POST", url, headers, dumps(payload))
        if isinstance(resp, Response):
            return resp
        if resp.status_code >= 400:
            body = await resp.aread()
            await resp.aclose()
            return translate_error(resp.status_code, body, self.name)
        return resp

    async def _read_json(self, resp: httpx.Response) -> dict[str, Any] | Response:
        body = await resp.aread()
        await resp.aclose()
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return anthropic_error(502, f"llmswitch: provider '{self.name}' returned a response that is not JSON")
        return data
