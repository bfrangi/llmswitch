"""Ways to ask an upstream which models it serves."""

from __future__ import annotations

import fnmatch
import logging
from typing import TYPE_CHECKING, Any

import httpx

from ..catalog import ModelEntry

if TYPE_CHECKING:
    from ..config import ProviderSpec

log = logging.getLogger("llmswitch.discovery")


def _ollama_description(m: dict[str, Any]) -> str:
    d = m.get("details") or {}
    parts: list[str] = []
    if d.get("parameter_size"):
        parts.append(str(d["parameter_size"]))
    if d.get("family"):
        parts.append(str(d["family"]))
    if d.get("context_length"):
        parts.append(f"ctx {int(d['context_length']) // 1024}k")
    caps = [c for c in (m.get("capabilities") or []) if c in ("tools", "vision", "thinking")]
    parts.extend(caps)
    return " · ".join(parts)


async def discover_ollama(spec: ProviderSpec, http: httpx.AsyncClient) -> list[ModelEntry]:
    base = spec.base_url[:-3] if spec.base_url.endswith("/v1") else spec.base_url
    r = await http.get(f"{base}/api/tags", timeout=10)
    r.raise_for_status()
    out = []
    for m in r.json().get("models") or []:
        d = m.get("details") or {}
        out.append(
            ModelEntry(
                provider=spec.name,
                upstream_id=m["name"],
                display_name=m["name"],
                description=_ollama_description(m),
                details={
                    "parameter_size": d.get("parameter_size"),
                    "family": d.get("family"),
                    "quantization": d.get("quantization_level"),
                    "context_length": d.get("context_length"),
                    "capabilities": m.get("capabilities") or [],
                    "size_bytes": m.get("size"),
                },
            )
        )
    return out


async def discover_openai(spec: ProviderSpec, http: httpx.AsyncClient, auth: dict[str, str]) -> list[ModelEntry]:
    base = spec.base_url.rstrip("/")
    url = f"{base}/models"
    r = await http.get(url, headers=auth, timeout=10)
    if r.status_code == 404 and not base.endswith("/v1"):
        r = await http.get(f"{base}/v1/models", headers=auth, timeout=10)
    r.raise_for_status()
    out = []
    for m in r.json().get("data") or []:
        mid = m.get("id")
        if not mid:
            continue
        owned = m.get("owned_by")
        out.append(
            ModelEntry(
                provider=spec.name,
                upstream_id=mid,
                display_name=mid,
                description=str(owned) if owned else "",
                details={k: v for k, v in m.items() if k not in ("id", "object")},
            )
        )
    return out


async def discover_anthropic(spec: ProviderSpec, http: httpx.AsyncClient, auth: dict[str, str]) -> list[ModelEntry]:
    if not auth and "api.anthropic.com" in spec.base_url:
        log.info("provider %s: model listing needs auth mode 'key'; showing static entries only", spec.name)
        return []
    base = spec.base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    headers = {"anthropic-version": "2023-06-01", **auth}
    try:
        r = await http.get(f"{base}/v1/models", params={"limit": 1000}, headers=headers, timeout=10)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403, 404, 501):
            log.info(
                "provider %s: model listing unavailable (%s); showing static entries only",
                spec.name,
                e.response.status_code,
            )
            return []
        raise
    except httpx.RequestError as e:
        log.info("provider %s: model listing failed (%s); showing static entries only", spec.name, e)
        return []
    out = []
    for m in r.json().get("data") or []:
        mid = m.get("id")
        if not mid:
            continue
        display_name = m.get("display_name") or m.get("description") or mid
        description = m.get("description") or m.get("owned_by") or ""
        out.append(
            ModelEntry(
                provider=spec.name,
                upstream_id=mid,
                display_name=display_name,
                description=description,
                details={"created_at": m.get("created_at")},
            )
        )
    return out


def _gemini_description(m: dict[str, Any]) -> str:
    parts: list[str] = []
    if m.get("inputTokenLimit"):
        parts.append(f"ctx {int(m['inputTokenLimit']) // 1024}k")
    if m.get("thinking"):
        parts.append("thinking")
    return " · ".join(parts)


async def discover_gemini(spec: ProviderSpec, http: httpx.AsyncClient, auth: dict[str, str]) -> list[ModelEntry]:
    """``GET {base}/models``, page by page, keeping the models that can ``generateContent``."""
    url = f"{spec.base_url.rstrip('/')}/models"
    out: list[ModelEntry] = []
    page: str | None = None
    while True:
        params: dict[str, Any] = {"pageSize": 1000}
        if page:
            params["pageToken"] = page
        r = await http.get(url, params=params, headers=auth, timeout=10)
        r.raise_for_status()
        data = r.json()
        for m in data.get("models") or []:
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            mid = str(m.get("name") or "").removeprefix("models/")
            if not mid:
                continue
            out.append(
                ModelEntry(
                    provider=spec.name,
                    upstream_id=mid,
                    display_name=m.get("displayName") or mid,
                    description=_gemini_description(m),
                    details={
                        "context_length": m.get("inputTokenLimit"),
                        "max_output_tokens": m.get("outputTokenLimit"),
                        "thinking": bool(m.get("thinking")),
                    },
                )
            )
        page = data.get("nextPageToken")
        if not page:
            return out


def _filtered(entries: list[ModelEntry], spec: ProviderSpec) -> list[ModelEntry]:
    inc, exc = spec.models.include, spec.models.exclude
    out = []
    for e in entries:
        if inc and not any(fnmatch.fnmatchcase(e.upstream_id, p) for p in inc):
            continue
        if exc and any(fnmatch.fnmatchcase(e.upstream_id, p) for p in exc):
            continue
        out.append(e)
    return out


async def discover_models(
    spec: ProviderSpec, http: httpx.AsyncClient, auth: dict[str, str], static: list[ModelEntry]
) -> list[ModelEntry]:
    """Discovered (filtered) entries followed by static ones, de-duplicated by id."""
    source = spec.models.source
    if source == "ollama":
        found = await discover_ollama(spec, http)
    elif source == "openai":
        found = await discover_openai(spec, http, auth)
    elif source in ("anthropic", "antigravity"):
        found = await discover_anthropic(spec, http, auth)
    elif source == "gemini":
        found = await discover_gemini(spec, http, auth)
    else:
        found = []
    seen: set[str] = set()
    out: list[ModelEntry] = []
    for e in _filtered(found, spec) + static:
        if e.upstream_id in seen:
            continue
        seen.add(e.upstream_id)
        out.append(e)
    return out
