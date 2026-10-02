from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config_loader import ProviderConfig

@dataclass
class BaseProvider(ABC):
    config: ProviderConfig

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def prefix(self) -> str:
        return self.config.prefix

    @property
    def endpoint(self) -> str:
        return self.config.endpoint

    @abstractmethod
    async def list_models(self) -> list[dict[str, Any]]:
        """Returns a list of models available on this provider."""
        pass

    @abstractmethod
    async def generate(self, model: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        """Generates a response for a given model and prompt."""
        pass
