"""The HTTP gateway: an Anthropic Messages API front door that routes by model name."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import __version__
from .catalog import Catalog
from .config import APP_NAME, ConfigError, load_config
from .providers import Incoming, anthropic_error, build_provider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .config import Config
    from .providers import Provider

log = logging.getLogger("llmswitch.gateway")


def make_http_client(config: Config) -> httpx.AsyncClient:
    """One shared upstream client: no read timeout (streams can idle), no redirects."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=config.gateway.connect_timeout, read=None, write=None, pool=None),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        follow_redirects=False,
    )


def provider_report(config: Config, catalog: Catalog) -> dict[str, dict[str, Any]]:
    status = catalog.status()
    out: dict[str, dict[str, Any]] = {}
    for spec in config.providers:
        s = status.get(spec.name)
        out[spec.name] = {
            "protocol": spec.protocol,
            "base_url": spec.base_url,
            "auth": spec.auth.mode,
            "tunnel": spec.tunnel,
            "match": spec.match,
            "default": spec.default,
            "description": spec.description,
            "ok": bool(s and s.ok),
            "error": s.error if s else None,
            "models": s.count if s else 0,
        }
    return out


async def read_incoming(request: Request) -> Incoming:
    body = await request.body()
    payload: Any = None
    if body:
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
    headers = [(k.lower(), v) for k, v in request.headers.items()]
    return Incoming(
        request.method,
        request.url.path,
        request.url.query,
        headers,
        body,
        payload if isinstance(payload, dict) else None,
    )


def create_app(config: Config, http: httpx.AsyncClient | None = None) -> FastAPI:
    owns_client = http is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = http or make_http_client(config)
        providers = {spec.name: build_provider(spec, client) for spec in config.providers}
        catalog = Catalog(providers, ttl=config.gateway.discovery_ttl, timeout=config.gateway.discovery_timeout)
        await catalog.refresh()
        app.state.providers = providers
        app.state.catalog = catalog
        app.state.started_at = time.time()
        for name, st in catalog.status().items():
            log.info("provider %s: %s", name, f"{st.count} models" if st.ok else f"unavailable ({st.error})")
        try:
            yield
        finally:
            if owns_client:
                await client.aclose()

    app = FastAPI(
        title=APP_NAME, version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    async def dispatch(request: Request, call: str) -> Response:
        inc = await read_incoming(request)
        if inc.json is None:
            return anthropic_error(400, "llmswitch: request body must be a JSON object")
        model = inc.json.get("model")
        if not isinstance(model, str) or not model:
            return anthropic_error(400, "llmswitch: 'model' is required")
        catalog: Catalog = request.app.state.catalog
        route, reason = await catalog.route(model)
        if route is None:
            return anthropic_error(404, f"llmswitch: {reason}")
        provider: Provider = request.app.state.providers[route.provider]
        log.info("%s %s -> %s/%s [%s]", call, model, route.provider, route.upstream_model, route.how)
        handler = provider.messages if call == "messages" else provider.count_tokens
        try:
            return await handler(inc, route.upstream_model)
        except Exception as e:
            log.exception("provider %s failed", route.provider)
            return anthropic_error(502, f"llmswitch: provider '{route.provider}' failed: {type(e).__name__}: {e}")

    @app.api_route("/api/hello", methods=["GET", "HEAD"])
    async def hello() -> Response:
        return Response(status_code=200)

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "service": APP_NAME,
                "version": __version__,
                "pid": os.getpid(),
                "config": str(config.path),
                "listen": config.gateway.base_url,
                "started_at": request.app.state.started_at,
                "providers": provider_report(config, request.app.state.catalog),
            }
        )

    @app.get("/llmswitch/models")
    async def hub_models(request: Request, refresh: int = 0) -> JSONResponse:
        catalog: Catalog = request.app.state.catalog
        await catalog.refresh(force=bool(refresh))
        return JSONResponse(
            {
                "models": [m.to_dict(catalog.specs[m.provider]) for m in catalog.models()],
                "providers": provider_report(config, catalog),
            }
        )

    @app.post("/llmswitch/refresh")
    async def hub_refresh(request: Request) -> JSONResponse:
        catalog: Catalog = request.app.state.catalog
        await catalog.refresh()
        return JSONResponse({"ok": True, "models": len(catalog.models())})

    @app.get("/v1/models")
    async def list_models(request: Request) -> JSONResponse:
        catalog: Catalog = request.app.state.catalog
        await catalog.refresh(force=False)
        data = []
        for m in catalog.models():
            spec = catalog.specs[m.provider]
            entry: dict[str, Any] = {
                "type": "model",
                "id": m.launch_id(spec),
                "display_name": m.display_name or m.upstream_id,
                "description": m.description or f"{m.provider} via {APP_NAME}",
                "hub_id": m.id,
                "provider": m.provider,
            }
            if m.details.get("created_at"):
                entry["created_at"] = m.details["created_at"]
            data.append(entry)
        return JSONResponse(
            {
                "data": data,
                "has_more": False,
                "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None,
            }
        )

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        return await dispatch(request, "messages")

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request) -> Response:
        return await dispatch(request, "count_tokens")

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def fallback(path: str, request: Request) -> Response:
        return anthropic_error(404, f"llmswitch: no route for {request.method} /{path}")

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llmswitch gateway", description="Run the llmswitch gateway in the foreground.")
    ap.add_argument(
        "--config", "-c", help="config file (default: $LLMSWITCH_CONFIG or ~/.config/llmswitch/config.yaml)"
    )
    ap.add_argument("--host", help="override gateway.host")
    ap.add_argument("--port", type=int, help="override gateway.port")
    ap.add_argument("--log-level", help="override gateway.log_level")
    args = ap.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"llmswitch: {e}", file=sys.stderr)
        return 2
    level = (args.log_level or config.gateway.log_level).lower()
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        create_app(config),
        host=args.host or config.gateway.host,
        port=args.port or config.gateway.port,
        log_level=level,
        access_log=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
