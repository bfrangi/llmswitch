"""What every provider shares: the request envelope, header policy, and error shape."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..catalog import ModelEntry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ..config import ProviderSpec

log = logging.getLogger("llmswitch.provider")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
REQUEST_DROP = HOP_BY_HOP | {"host", "content-length"}
RESPONSE_DROP = HOP_BY_HOP | {"content-length", "date", "server"}
CREDENTIAL_HEADERS = {"authorization", "x-api-key"}
NEGOTIATION_HEADERS = {"accept", "accept-encoding", "content-type"}


class TranslationError(ValueError):
    """A client request that cannot be expressed in the upstream's protocol."""


def is_custom_tool(tool: Any) -> bool:
    """A tool the client runs itself (name + input schema), as opposed to an Anthropic server tool."""
    return isinstance(tool, dict) and tool.get("type") in (None, "custom") and "name" in tool and "input_schema" in tool


@dataclass
class Incoming:
    """A client request as received, before any provider touches it."""

    method: str
    path: str
    query: str
    headers: list[tuple[str, str]]  # lower-cased names, original order, repeats kept
    body: bytes
    json: dict[str, Any] | None


def error_type_for_status(status: int) -> str:
    return {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        413: "request_too_large",
        429: "rate_limit_error",
        529: "overloaded_error",
    }.get(status, "api_error")


def anthropic_error(status: int, message: str, err_type: str | None = None) -> JSONResponse:
    """An error in the exact shape the Anthropic API uses, so clients handle it natively."""
    return JSONResponse(
        {"type": "error", "error": {"type": err_type or error_type_for_status(status), "message": message}},
        status_code=status,
    )


def translate_error(status: int, body: bytes, provider: str) -> JSONResponse:
    """A non-Anthropic upstream's error, re-shaped so the client handles it natively.

    Understands ``{"error": {"message": ...}}``, ``{"error": "..."}``, either one wrapped in a
    one-element list (Google does that), and falls back to the raw body.
    """
    message = body.decode(errors="replace")[:2000]
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, list) and data:
        data = data[0]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        message = str(err.get("message") or message)
    elif isinstance(err, str):
        message = err
    return anthropic_error(status, f"{provider}: {message}", error_type_for_status(status))


async def _relay(resp: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in resp.aiter_raw():
            yield chunk
    finally:
        await resp.aclose()


def passthrough_response(resp: httpx.Response) -> StreamingResponse:
    """Stream an upstream response back byte-for-byte, headers included."""
    headers = {k: v for k, v in resp.headers.items() if k.lower() not in RESPONSE_DROP}
    return StreamingResponse(_relay(resp), status_code=resp.status_code, headers=headers)


class Provider(ABC):
    """One upstream server. Subclasses implement a wire protocol, not a vendor."""

    protocol: ClassVar[str] = ""

    def __init__(self, spec: ProviderSpec, http: httpx.AsyncClient) -> None:
        self.spec = spec
        self.http = http

    @property
    def name(self) -> str:
        return self.spec.name

    # ------------------------------------------------------------------ contract
    @abstractmethod
    async def discover(self) -> list[ModelEntry]:
        """Models this upstream serves right now (plus static entries)."""

    @abstractmethod
    async def messages(self, inc: Incoming, upstream_model: str) -> Response:
        """Serve ``POST /v1/messages`` for a model that routed here."""

    @abstractmethod
    async def count_tokens(self, inc: Incoming, upstream_model: str) -> Response:
        """Serve ``POST /v1/messages/count_tokens`` for a model that routed here."""

    # ------------------------------------------------------------------- helpers
    def static_entries(self) -> list[ModelEntry]:
        return [
            ModelEntry(
                provider=self.name,
                upstream_id=e.id,
                display_name=e.display_name or e.id,
                description=e.description or "",
                bare=e.bare,
            )
            for e in self.spec.models.static
        ]

    def url(self, path: str, query: str = "") -> str:
        base = self.spec.base_url.rstrip("/")
        if path.startswith("/v1") and base.endswith("/v1"):
            base = base[:-3]
        return f"{base}{path}" + (f"?{query}" if query else "")

    def auth_headers(self, default_header: str) -> dict[str, str]:
        """Credentials the provider itself holds (none in passthrough mode)."""
        if self.spec.auth.mode != "key" or not self.spec.auth.key:
            return {}
        header = self.spec.auth.header or default_header
        value = self.spec.auth.key
        if header == "authorization" and not value.lower().startswith("bearer "):
            value = f"Bearer {value}"
        return {header: value}

    def upstream_headers(self, inc: Incoming, *, default_auth_header: str) -> list[tuple[str, str]]:
        """Apply the header policy: hop-by-hop out, credentials per ``auth``, extras in."""
        drop = REQUEST_DROP | set(self.spec.compat.drop_headers)
        passthrough = self.spec.auth.mode == "passthrough"
        out = [(k, v) for k, v in inc.headers if k not in drop and (passthrough or k not in CREDENTIAL_HEADERS)]
        override = {**self.auth_headers(default_auth_header), **{k.lower(): v for k, v in self.spec.headers.items()}}
        if override:
            out = [(k, v) for k, v in out if k not in override]
            out.extend(override.items())
        return out

    def translated_headers(self, inc: Incoming, *, default_auth_header: str) -> list[tuple[str, str]]:
        """The header policy for a JSON body this provider wrote itself, in another protocol.

        The client's content negotiation describes the client's protocol and decoders, not
        this request: the body is JSON, and the HTTP client asks for an encoding it can decode.
        """
        out = [
            (k, v)
            for k, v in self.upstream_headers(inc, default_auth_header=default_auth_header)
            if k not in NEGOTIATION_HEADERS
        ]
        out.append(("content-type", "application/json"))
        return out

    def apply_compat(self, body: dict[str, Any]) -> dict[str, Any]:
        c = self.spec.compat
        if c.is_noop():
            return body
        out = {k: v for k, v in body.items() if k not in c.drop_fields}
        if c.drop_tool_fields and isinstance(out.get("tools"), list):
            out["tools"] = [
                {k: v for k, v in t.items() if k not in c.drop_tool_fields} if isinstance(t, dict) else t
                for t in out["tools"]
            ]
        for old, new in c.rename_fields.items():
            if old in out:
                out[new] = out.pop(old)
        out.update(c.set_fields)
        return out

    async def send(
        self, method: str, url: str, headers: list[tuple[str, str]], body: bytes
    ) -> httpx.Response | JSONResponse:
        """Open an upstream request as a stream; translate transport failures to API errors."""
        req = self.http.build_request(method, url, headers=headers, content=body)
        try:
            return await self.http.send(req, stream=True)
        except httpx.ConnectError as e:
            return anthropic_error(
                502, f"llmswitch: provider '{self.name}' is unreachable at {self.spec.base_url} ({e})"
            )
        except httpx.TimeoutException as e:
            return anthropic_error(504, f"llmswitch: provider '{self.name}' timed out ({type(e).__name__})")
        except httpx.HTTPError as e:
            return anthropic_error(502, f"llmswitch: provider '{self.name}' request failed ({type(e).__name__}: {e})")


def dumps(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
