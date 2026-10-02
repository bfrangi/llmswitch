from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseProvider
    from .config_loader import ProviderConfig

class ProviderFactory:
    @staticmethod
    def create(config: ProviderConfig) -> BaseProvider:
        from .providers.ollama import OllamaProvider
        from .providers.anthropic import AnthropicProvider

        registry = {
            "ollama": OllamaProvider,
            "anthropic": AnthropicProvider,
        }

        provider_class = registry.get(config.type)
        if not provider_class:
            raise ValueError(f"Unknown provider type: {config.type}")

        return provider_class(config)
