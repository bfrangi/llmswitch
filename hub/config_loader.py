from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config_loader import HubConfig

@dataclass
class ProviderConfig:
    name: str
    type: str
    endpoint: str
    prefix: str = ""
    api_key: str | None = None
    mappings: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

@dataclass
class HubConfig:
    providers: list[ProviderConfig] = field(default_factory=list)

def load_config() -> HubConfig:
    """
    Loads configuration from:
    1. Current working directory: ./config.yaml
    2. User config directory: ~/.config/ai-hub/config.yaml
    """
    from pathlib import Path
    import yaml

    paths_to_check = [
        Path("config.yaml"),
        Path.home() / ".config" / "ai-hub" / "config.yaml"
    ]

    config_data = None
    for path in paths_to_check:
        if path.exists():
            with open(path, "r") as f:
                try:
                    config_data = yaml.safe_load(f)
                    break
                except yaml.YAMLError as e:
                    print(f"Error parsing config at {path}: {e}")
                    continue

    if not config_data:
        return HubConfig()

    providers = []
    for p in config_data.get("providers", []):
        known_fields = {"name", "type", "endpoint", "prefix", "api_key", "mappings"}
        p_data = p.copy()
        extra = {k: v for k, v in p_data.items() if k not in known_fields}

        providers.append(ProviderConfig(
            name=p_data.get("name"),
            type=p_data.get("type"),
            endpoint=p_data.get("endpoint"),
            prefix=p_data.get("prefix", ""),
            api_key=p_data.get("api_key"),
            mappings=p_data.get("mappings", {}),
            extra=extra
        ))

    return HubConfig(providers=providers)
