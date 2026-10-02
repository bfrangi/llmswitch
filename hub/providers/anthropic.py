"""Pass-through to any upstream that speaks the Anthropic Messages API.

That is Anthropic itself, Ollama, and a growing list of local servers. The request
goes upstream unchanged except for the model name (and any configured ``compat``
surgery); the response streams back byte-for-byte, so caching markers, beta
headers, usage, and error wording all survive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse, Response

from .base import Provider, anthropic_error, dumps, passthrough_response
from .discovery import discover_models

if TYPE_CHECKING:
    from ..catalog import ModelEntry
    from .base import Incoming


class AnthropicProvider(Provider):
    protocol = "anthropic"

    async def discover(self) -> list[ModelEntry]:
        return await discover_models(self.spec, self.http, self.auth_headers("x-api-key"), self.static_entries())

    async def messages(self, inc: Incoming, upstream_model: str) -> Response:
        return await self._forward(inc, "/v1/messages", upstream_model)

    async def count_tokens(self, inc: Incoming, upstream_model: str) -> Response:
        resp = await self._forward(inc, "/v1/messages/count_tokens", upstream_model, raw=True)
        if isinstance(resp, JSONResponse):
            return resp
        # Upstreams without a counting endpoint (Ollama answers a plain-text 404) get a
        # proper API error so the client falls back to its own estimate.
        if resp.status_code in (404, 405) and "json" not in resp.headers.get("content-type", ""):
            await resp.aclose()
            return anthropic_error(404, f"ai-hub: provider '{self.name}' has no token counting endpoint")
        return passthrough_response(resp)

    def _prepare_body(self, inc: Incoming, upstream_model: str) -> bytes:
        payload = inc.json or {}
        if payload.get("model") == upstream_model and self.spec.compat.is_noop():
            return inc.body  # nothing to change: keep the client's exact bytes
        payload = dict(payload)
        payload["model"] = upstream_model
        return dumps(self.apply_compat(payload))

    async def _forward(self, inc: Incoming, path: str, upstream_model: str, raw: bool = False):
        body = self._prepare_body(inc, upstream_model)
        headers = self.upstream_headers(inc, default_auth_header="x-api-key")
        resp = await self.send("POST", self.url(path, inc.query), headers, body)
        if isinstance(resp, JSONResponse) or raw:
            return resp
        return passthrough_response(resp)
