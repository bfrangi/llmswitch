from __future__ import annotations
import os
import contextlib
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from typing import Any, AsyncGenerator

from .router import Router

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup logic
    await router.initialize()
    yield
    # Shutdown logic (if needed)

app = FastAPI(title="AI Hub Gateway", lifespan=lifespan)
router = Router()

@app.get("/api/tags")
async def get_tags() -> Any:
    """Aggregated list of models from all providers."""
    try:
        models = await router.get_all_models()
        return {"models": models}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/debug/model_map")
async def debug_model_map() -> Any:
    """Returns the current model map for debugging."""
    return router.model_map

@app.post("/api/generate")
async def generate(request: Request) -> Any:
    """Unified generation endpoint."""
    data = await request.json()
    model_name = data.get("model")
    prompt = data.get("prompt")

    if not model_name or not prompt:
        raise HTTPException(status_code=400, detail="Missing model or prompt")

    kwargs = {k: v for k, v in data.items() if k not in ["model", "prompt"]}

    result = await router.route_request(model_name, prompt, **kwargs)

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result

@app.get("/v1/models")
async def list_models() -> Any:
    """Returns the available models in an OpenAI-compatible format."""
    try:
        models_data = []
        for display_name, entry in router.model_map.items():
            models_data.append({
                "id": display_name,
                "object": "model",
                "created": 1700000000,  # Dummy timestamp
                "owned_by": entry["provider"].name
            })
        return {
            "object": "list",
            "data": models_data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/messages")
async def anthropic_messages(request: Request) -> Any:
    """Anthropic-compatible messages endpoint."""
    try:
        data = await request.json()
        model = data.get("model")
        messages = data.get("messages")

        if not model or not messages:
            raise HTTPException(status_code=400, detail="Missing model or messages")

        result = await router.route_anthropic_request(model, messages)

        if "error" in result:
             raise HTTPException(status_code=404, detail=result["error"])

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def main() -> None:
    # Default to 11436 to avoid conflict with local Ollama (11434)
    port = int(os.getenv("HUB_PORT", "11436"))
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
