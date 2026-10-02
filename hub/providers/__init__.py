"""Provider registry, keyed by wire protocol. Third parties can add one via the
``ai_hub.providers`` entry-point group."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from ..config import ConfigError
from .anthropic import AnthropicProvider
from .base import Incoming, Provider, anthropic_error
from .openai import OpenAIProvider

if TYPE_CHECKING:
    import httpx

    from ..config import ProviderSpec

REGISTRY: dict[str, type[Provider]] = {cls.protocol: cls for cls in (AnthropicProvider, OpenAIProvider)}


def _load_plugins() -> None:
    try:
        eps = entry_points(group="ai_hub.providers")
    except TypeError:  # pragma: no cover - very old importlib.metadata
        eps = []
    for ep in eps:
        try:
            cls = ep.load()
            REGISTRY[getattr(cls, "protocol", ep.name)] = cls
        except Exception:
            continue


_load_plugins()


def build_provider(spec: ProviderSpec, http: httpx.AsyncClient) -> Provider:
    cls = REGISTRY.get(spec.protocol)
    if cls is None:
        raise ConfigError(
            f"providers.{spec.name}: unknown protocol '{spec.protocol}' (available: {', '.join(sorted(REGISTRY))})"
        )
    return cls(spec, http)


__all__ = ["REGISTRY", "Incoming", "Provider", "anthropic_error", "build_provider"]
