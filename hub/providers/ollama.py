from __future__ import annotations
import httpx
import traceback
from typing import TYPE_CHECKING, Any

from .base import BaseProvider

class OllamaProvider(BaseProvider):
    async def list_models(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(f"{self.endpoint}/api/tags")
                response.raise_for_status()
                data = response.json()

                models = []
                for m in data.get("models", []):
                    name = m["name"]
                    # Apply prefix with a space as requested
                    formatted_name = f"{self.prefix} {name}" if self.prefix else name
                    models.append({
                        "name": name,
                        "display_name": formatted_name,
                        "provider_name": self.name
                    })
                return models
            except Exception as e:
                print(f"Error listing models on Ollama ({self.name}): {e}")
                traceback.print_exc()
                return []

    async def generate(self, model: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        # Use a longer timeout for model generation
        async with httpx.AsyncClient(timeout=300.0) as client:
            try:
                payload = {
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    **kwargs
                }
                response = await client.post(f"{self.endpoint}/api/generate", json=payload)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                print(f"Ollama generation error on {self.name} for model {model}: {e}")
                traceback.print_exc()
                return {"error": f"Ollama generation error: {str(e)}"}
