from __future__ import annotations
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import BaseProvider

class Router:
    def __init__(self) -> None:
        from .config_loader import load_config
        self.config = load_config()
        self.providers: list[BaseProvider] = []
        self.model_map: dict[str, dict[str, Any]] = {}

    async def initialize(self) -> None:
        """Initialize all providers and build the model lookup map."""
        from .factory import ProviderFactory

        # 1. Initialize all providers and collect their models
        for p_config in self.config.providers:
            provider = ProviderFactory.create(p_config)
            self.providers.append(provider)

            models = await provider.list_models()
            for m in models:
                self.model_map[m["display_name"]] = {
                    "provider": provider,
                    "original_name": m["name"]
                }

        # 2. Apply all mappings from all providers (these take precedence)
        for p_config in self.config.providers:
            # Find the corresponding provider that was just created
            # Since we created them in the same order, we can find them by index.
            # But let's be more robust and find them by name.
            provider = next((p for p in self.providers if p.name == p_config.name), None)
            if provider and p_config.mappings:
                for mapped_name, original_name in p_config.mappings.items():
                    self.model_map[mapped_name] = {
                        "provider": provider,
                        "original_name": original_name
                    }

    async def get_all_models(self) -> list[dict[str, Any]]:
        """Returns the aggregated list of models for the model picker."""
        all_models = []
        for provider in self.providers:
            all_models.extend(await provider.list_models())
        return all_models

    async def route_request(self, display_name: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        """Routes a generation request to the correct provider."""
        if display_name in self.model_map:
            entry = self.model_map[display_name]
            return await entry["provider"].generate(entry["original_name"], prompt, **kwargs)

        for entry in self.model_map.values():
            if entry["original_name"] == display_name:
                return await entry["provider"].generate(entry["original_name"], prompt, **kwargs)

        return {"error": f"Model '{display_name}' not found in any provider."}

    async def route_anthropic_request(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        """Routes an Anthropic-style messages request to the correct provider."""
        if not messages:
            return {"error": "No messages provided"}

        # Extract the last message as the prompt
        last_message = messages[-1]
        if last_message.get("role") != "user":
            return {"error": "Last message must be from user"}

        # Handle different content formats (text is most common)
        content = last_message.get("content", "")
        if isinstance(content, list):
            # Extract text from the first text block
            prompt = ""
            for item in content:
                if item.get("type") == "text":
                    prompt += item.get("text", "")
        else:
            prompt = content

        if not prompt:
            return {"error": "No text content found in the last message"}

        # Route the request using the existing logic
        result = await self.route_request(model, prompt, **kwargs)

        if "error" in result:
            return result

        # Wrap the result into an Anthropic-compatible response
        # We use a dummy ID for now.
        return {
            "id": f"msg_{os.urandom(8).hex()}",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": result.get("response", result.get("content", ""))
                }
            ],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 0,  # We don't track this yet
                "output_tokens": 0
            }
        }
