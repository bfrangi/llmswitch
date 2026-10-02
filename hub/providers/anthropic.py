from __future__ import annotations
import httpx
from typing import TYPE_CHECKING, Any

from .base import BaseProvider

class AnthropicProvider(BaseProvider):
    async def list_models(self) -> list[dict[str, Any]]:
        # For now, return a curated list of Claude models.
        models = [
            "claude-3-5-sonnet-20240620",
            "claude-3-opus-20240229",
            "claude-3-haiku-20240307"
        ]

        return [
            {
                "name": m,
                "display_name": f"{self.prefix} {m}" if self.prefix else m,
                "provider_name": self.name
            } for m in models
        ]

    async def generate(self, model: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            try:
                headers = {
                    "x-api-key": self.config.api_key or "",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                }
                payload = {
                    "model": model,
                    "max_tokens": kwargs.get("max_tokens", 1024),
                    "messages": [{"role": "user", "content": prompt}]
                }
                response = await client.post(f"{self.endpoint}/v1/messages", json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()

                return {
                    "response": data["content"][0]["text"],
                    "model": model,
                    "done": True
                }
            except Exception as e:
                return {"error": f"Anthropic generation error: {str(e)}"}
