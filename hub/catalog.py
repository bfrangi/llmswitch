"""The model catalog: what every provider serves, and which provider a name maps to.

Routing rules, in order:

1. ``<provider>/<model>`` names a configured provider explicitly.
2. A bare name matching a provider's ``match`` globs goes to that provider.
3. A bare name discovered on exactly one provider goes there.
4. Otherwise the provider marked ``default: true`` gets it, if any.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .config import ProviderSpec

log = logging.getLogger("ai-hub.catalog")


def matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


@dataclass
class ModelEntry:
    provider: str
    upstream_id: str
    display_name: str = ""
    description: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    bare: bool = False

    @property
    def id(self) -> str:
        return f"{self.provider}/{self.upstream_id}"

    def launch_id(self, spec: ProviderSpec) -> str:
        """The string a client should send: bare when the bare name already routes here."""
        if self.bare or matches_any(self.upstream_id, spec.match):
            return self.upstream_id
        return self.id

    def to_dict(self, spec: ProviderSpec) -> dict[str, Any]:
        return {
            "id": self.id,
            "launch_id": self.launch_id(spec),
            "provider": self.provider,
            "upstream_id": self.upstream_id,
            "display_name": self.display_name or self.upstream_id,
            "description": self.description,
            "details": self.details,
        }


@dataclass
class Route:
    provider: str
    upstream_model: str
    how: str  # prefix | match | discovered | default


@dataclass
class ProviderStatus:
    ok: bool = False
    error: str | None = None
    count: int = 0
    checked_at: float = 0.0


class Discoverer(Protocol):
    spec: ProviderSpec

    async def discover(self) -> list[ModelEntry]: ...

    def static_entries(self) -> list[ModelEntry]: ...


class Catalog:
    def __init__(self, providers: dict[str, Discoverer], *, ttl: float = 30.0, timeout: float = 5.0) -> None:
        self._providers = providers
        self._specs: dict[str, ProviderSpec] = {name: p.spec for name, p in providers.items()}
        self._ttl = ttl
        self._timeout = timeout
        self._models: list[ModelEntry] = []
        self._status: dict[str, ProviderStatus] = {n: ProviderStatus() for n in providers}
        self._refreshed_at = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ queries
    @property
    def specs(self) -> dict[str, ProviderSpec]:
        return self._specs

    def models(self) -> list[ModelEntry]:
        return list(self._models)

    def status(self) -> dict[str, ProviderStatus]:
        return dict(self._status)

    def stale(self) -> bool:
        return (time.monotonic() - self._refreshed_at) > self._ttl

    # ---------------------------------------------------------------- discovery
    async def refresh(self, force: bool = True) -> None:
        if not force and not self.stale():
            return
        async with self._lock:
            if not force and not self.stale():
                return
            results = await asyncio.gather(*(self._discover_one(n, p) for n, p in self._providers.items()))
            models: list[ModelEntry] = []
            for name, (entries, status) in zip(self._providers, results, strict=False):
                self._status[name] = status
                models.extend(entries)
            self._models = models
            self._refreshed_at = time.monotonic()

    async def _discover_one(self, name: str, provider: Discoverer) -> tuple[list[ModelEntry], ProviderStatus]:
        try:
            entries = await asyncio.wait_for(provider.discover(), self._timeout)
            return entries, ProviderStatus(ok=True, count=len(entries), checked_at=time.time())
        except asyncio.TimeoutError:
            msg = f"model discovery timed out after {self._timeout:g}s"
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
        log.warning("provider %s: %s", name, msg)
        static = provider.static_entries()
        return static, ProviderStatus(ok=False, error=msg, count=len(static), checked_at=time.time())

    # ------------------------------------------------------------------ routing
    def resolve(self, requested: str, *, use_default: bool = True) -> tuple[Route | None, str]:
        name, sep, rest = requested.partition("/")
        if sep and name in self._specs:
            if not rest:
                return None, f"model '{requested}' names provider '{name}' but no model"
            return Route(name, rest, "prefix"), ""
        for spec in self._specs.values():
            if matches_any(requested, spec.match):
                return Route(spec.name, requested, "match"), ""
        hits = [m for m in self._models if m.upstream_id == requested]
        if len(hits) == 1:
            return Route(hits[0].provider, requested, "discovered"), ""
        if len(hits) > 1:
            return None, f"model '{requested}' exists on several providers; use one of: " + ", ".join(
                h.id for h in hits
            )
        if use_default:
            for spec in self._specs.values():
                if spec.default:
                    return Route(spec.name, requested, "default"), ""
        return None, (
            f"model '{requested}' is not served by any provider; use <provider>/<model> with one of "
            + ", ".join(self._specs)
            + " (list them with `ai-hub models`)"
        )

    async def route(self, requested: str) -> tuple[Route | None, str]:
        """Resolve; on an unknown name, refresh a stale catalog before using the default provider."""
        route, reason = self.resolve(requested, use_default=False)
        if route is None and self.stale():
            await self.refresh()
            route, reason = self.resolve(requested, use_default=False)
        if route is None:
            route, reason = self.resolve(requested)
        return route, reason
