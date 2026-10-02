"""Configuration model and loader.

Everything the gateway knows about the machine comes from one YAML file (by default
``~/.config/llmswitch/config.yaml``). The loader is strict: unknown keys and bad
types are reported with their path so typos surface immediately.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

APP_NAME = "llmswitch"
CONFIG_ENV_VAR = "LLMSWITCH_CONFIG"

PROTOCOLS = ("anthropic", "openai")
MODEL_SOURCES = ("ollama", "openai", "anthropic", "none")
AUTH_MODES = ("passthrough", "key", "none")
AUTH_HEADERS = ("x-api-key", "authorization")


class ConfigError(Exception):
    """A missing, unreadable, or invalid configuration."""


# ----------------------------------------------------------------------------- paths


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


def xdg_state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))


def default_config_path() -> Path:
    return xdg_config_home() / APP_NAME / "config.yaml"


def resolve_config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """``--config`` flag, then ``$LLMSWITCH_CONFIG``, then the XDG default."""
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        return Path(from_env).expanduser()
    return default_config_path()


# --------------------------------------------------------------------- interpolation

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def interpolate(value: Any, where: str = "") -> Any:
    """Expand ``${VAR}`` / ``${VAR:-default}`` and a leading ``~`` in every string."""
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"{where or 'config'}: environment variable ${{{name}}} is not set")

        out = _VAR.sub(repl, value)
        return str(Path(out).expanduser()) if out.startswith("~") else out
    if isinstance(value, dict):
        return {k: interpolate(v, f"{where}.{k}" if where else str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


# ------------------------------------------------------------------------- schema


@dataclass
class GatewaySettings:
    host: str = "127.0.0.1"
    port: int = 11436
    state_dir: Path = field(default_factory=lambda: xdg_state_home() / APP_NAME)
    discovery_ttl: float = 30.0
    discovery_timeout: float = 5.0
    connect_timeout: float = 10.0
    log_level: str = "info"

    @property
    def base_url(self) -> str:
        host = "127.0.0.1" if self.host in ("", "0.0.0.0", "::") else self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"


@dataclass
class TunnelSpec:
    name: str
    ssh: str
    forwards: list[str]
    options: list[str] = field(default_factory=list)

    def local_ports(self) -> list[int]:
        """Local ports from ``[bind:]port:host:hostport`` forward specs."""
        ports: list[int] = []
        for fwd in self.forwards:
            for part in fwd.split(":"):
                if part.isdigit():
                    ports.append(int(part))
                    break
        return ports


@dataclass
class ModelEntrySpec:
    id: str
    display_name: str | None = None
    description: str | None = None
    bare: bool = False


@dataclass
class ModelsSpec:
    source: str = "none"
    static: list[ModelEntrySpec] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class AuthSpec:
    mode: str = "none"
    key: str | None = None
    header: str | None = None


@dataclass
class CompatSpec:
    """Per-provider request surgery for upstreams that reject fields they don't know."""

    drop_fields: list[str] = field(default_factory=list)
    drop_tool_fields: list[str] = field(default_factory=list)
    drop_headers: list[str] = field(default_factory=list)
    rename_fields: dict[str, str] = field(default_factory=dict)
    set_fields: dict[str, Any] = field(default_factory=dict)

    def is_noop(self) -> bool:
        return not (self.drop_fields or self.drop_tool_fields or self.rename_fields or self.set_fields)


@dataclass
class LaunchSpec:
    """Extra environment for Claude Code when the chosen model lives on this provider.

    Values may use ``{model}``, ``{upstream_model}``, ``{provider}`` and
    ``{details.<key>}`` placeholders, filled in from the chosen catalog entry.
    """

    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ProviderSpec:
    name: str
    protocol: str
    base_url: str
    auth: AuthSpec = field(default_factory=AuthSpec)
    models: ModelsSpec = field(default_factory=ModelsSpec)
    match: list[str] = field(default_factory=list)
    tunnel: str | None = None
    default: bool = False
    compat: CompatSpec = field(default_factory=CompatSpec)
    headers: dict[str, str] = field(default_factory=dict)
    launch: LaunchSpec = field(default_factory=LaunchSpec)
    description: str | None = None


@dataclass
class ClaudeSettings:
    command: str = "claude"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    default_model: str | None = None


@dataclass
class Config:
    path: Path
    gateway: GatewaySettings
    tunnels: dict[str, TunnelSpec]
    providers: list[ProviderSpec]
    claude: ClaudeSettings

    def provider(self, name: str) -> ProviderSpec | None:
        return next((p for p in self.providers if p.name == name), None)

    def tunnels_in_use(self) -> list[TunnelSpec]:
        names = {p.tunnel for p in self.providers if p.tunnel}
        return [t for n, t in self.tunnels.items() if n in names]


# ------------------------------------------------------------------------ parsing


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(value).__name__}")
    return value


def _only_keys(raw: dict[str, Any], allowed: tuple[str, ...], where: str) -> None:
    unknown = [k for k in raw if k not in allowed]
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(map(str, unknown))}; allowed: {', '.join(allowed)}")


def _str(value: Any, where: str, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a string, got {type(value).__name__}")
    return value


def _req_str(value: Any, where: str) -> str:
    s = _str(value, where)
    if not s:
        raise ConfigError(f"{where}: is required")
    return s


def _str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ConfigError(f"{where}: expected a string or a list of strings")


def _str_dict(value: Any, where: str) -> dict[str, str]:
    m = _mapping(value, where)
    return {str(k): str(v) for k, v in m.items()}


def _bool(value: Any, where: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true/false")
    return value


def _int(value: Any, where: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected an integer")
    return value


def _float(value: Any, where: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected a number")
    return float(value)


def _parse_gateway(raw: Any) -> GatewaySettings:
    m = _mapping(raw, "gateway")
    _only_keys(
        m,
        ("host", "port", "state_dir", "discovery_ttl", "discovery_timeout", "connect_timeout", "log_level"),
        "gateway",
    )
    d = GatewaySettings()
    state_dir = _str(m.get("state_dir"), "gateway.state_dir")
    return GatewaySettings(
        host=_str(m.get("host"), "gateway.host", d.host) or d.host,
        port=_int(m.get("port"), "gateway.port", d.port),
        state_dir=Path(state_dir).expanduser() if state_dir else d.state_dir,
        discovery_ttl=_float(m.get("discovery_ttl"), "gateway.discovery_ttl", d.discovery_ttl),
        discovery_timeout=_float(m.get("discovery_timeout"), "gateway.discovery_timeout", d.discovery_timeout),
        connect_timeout=_float(m.get("connect_timeout"), "gateway.connect_timeout", d.connect_timeout),
        log_level=(_str(m.get("log_level"), "gateway.log_level", d.log_level) or d.log_level).lower(),
    )


def _parse_tunnels(raw: Any) -> dict[str, TunnelSpec]:
    out: dict[str, TunnelSpec] = {}
    for name, body in _mapping(raw, "tunnels").items():
        where = f"tunnels.{name}"
        m = _mapping(body, where)
        _only_keys(m, ("ssh", "forward", "forwards", "options"), where)
        forwards = _str_list(m.get("forward"), f"{where}.forward") + _str_list(m.get("forwards"), f"{where}.forwards")
        if not forwards:
            raise ConfigError(f"{where}: needs at least one forward (e.g. 11435:localhost:11434)")
        out[str(name)] = TunnelSpec(
            name=str(name),
            ssh=_req_str(m.get("ssh"), f"{where}.ssh"),
            forwards=forwards,
            options=_str_list(m.get("options"), f"{where}.options"),
        )
    return out


def _parse_auth(raw: dict[str, Any], where: str) -> AuthSpec:
    auth, api_key = raw.get("auth"), raw.get("api_key")
    if api_key is not None:
        if auth is not None and auth != "key":
            raise ConfigError(f"{where}: use either api_key or auth, not both")
        return AuthSpec(mode="key", key=_req_str(api_key, f"{where}.api_key"))
    if auth is None:
        return AuthSpec()
    if isinstance(auth, str):
        if auth not in AUTH_MODES:
            raise ConfigError(f"{where}.auth: expected one of {', '.join(AUTH_MODES)}")
        if auth == "key":
            raise ConfigError(f"{where}.auth: 'key' needs a key; use api_key: ... or auth: {{mode: key, key: ...}}")
        return AuthSpec(mode=auth)
    m = _mapping(auth, f"{where}.auth")
    _only_keys(m, ("mode", "key", "header"), f"{where}.auth")
    mode = _str(m.get("mode"), f"{where}.auth.mode", "key" if m.get("key") else "none") or "none"
    if mode not in AUTH_MODES:
        raise ConfigError(f"{where}.auth.mode: expected one of {', '.join(AUTH_MODES)}")
    key = _str(m.get("key"), f"{where}.auth.key")
    if mode == "key" and not key:
        raise ConfigError(f"{where}.auth: mode 'key' needs a key")
    header = _str(m.get("header"), f"{where}.auth.header")
    if header and header.lower() not in AUTH_HEADERS:
        raise ConfigError(f"{where}.auth.header: expected one of {', '.join(AUTH_HEADERS)}")
    return AuthSpec(mode=mode, key=key, header=header.lower() if header else None)


def _parse_model_entry(raw: Any, where: str) -> ModelEntrySpec:
    if isinstance(raw, str):
        return ModelEntrySpec(id=raw)
    m = _mapping(raw, where)
    _only_keys(m, ("id", "display_name", "description", "bare"), where)
    return ModelEntrySpec(
        id=_req_str(m.get("id"), f"{where}.id"),
        display_name=_str(m.get("display_name"), f"{where}.display_name"),
        description=_str(m.get("description"), f"{where}.description"),
        bare=_bool(m.get("bare"), f"{where}.bare"),
    )


def _parse_models(raw: Any, where: str) -> ModelsSpec:
    if raw is None:
        return ModelsSpec()
    if isinstance(raw, str):
        if raw not in MODEL_SOURCES:
            raise ConfigError(f"{where}: expected one of {', '.join(MODEL_SOURCES)} or a list of models")
        return ModelsSpec(source=raw)
    if isinstance(raw, list):
        return ModelsSpec(static=[_parse_model_entry(e, f"{where}[{i}]") for i, e in enumerate(raw)])
    m = _mapping(raw, where)
    _only_keys(m, ("source", "static", "include", "exclude"), where)
    source = _str(m.get("source"), f"{where}.source", "none") or "none"
    if source not in MODEL_SOURCES:
        raise ConfigError(f"{where}.source: expected one of {', '.join(MODEL_SOURCES)}")
    static_raw = m.get("static") or []
    if not isinstance(static_raw, list):
        raise ConfigError(f"{where}.static: expected a list")
    return ModelsSpec(
        source=source,
        static=[_parse_model_entry(e, f"{where}.static[{i}]") for i, e in enumerate(static_raw)],
        include=_str_list(m.get("include"), f"{where}.include"),
        exclude=_str_list(m.get("exclude"), f"{where}.exclude"),
    )


def _parse_compat(raw: Any, where: str) -> CompatSpec:
    m = _mapping(raw, where)
    _only_keys(m, ("drop_fields", "drop_tool_fields", "drop_headers", "rename_fields", "set_fields"), where)
    return CompatSpec(
        drop_fields=_str_list(m.get("drop_fields"), f"{where}.drop_fields"),
        drop_tool_fields=_str_list(m.get("drop_tool_fields"), f"{where}.drop_tool_fields"),
        drop_headers=[h.lower() for h in _str_list(m.get("drop_headers"), f"{where}.drop_headers")],
        rename_fields=_str_dict(m.get("rename_fields"), f"{where}.rename_fields"),
        set_fields=dict(_mapping(m.get("set_fields"), f"{where}.set_fields")),
    )


def _parse_launch(raw: Any, where: str) -> LaunchSpec:
    m = _mapping(raw, where)
    _only_keys(m, ("env",), where)
    return LaunchSpec(env=_str_dict(m.get("env"), f"{where}.env"))


_PROVIDER_KEYS = (
    "protocol",
    "base_url",
    "auth",
    "api_key",
    "models",
    "match",
    "tunnel",
    "default",
    "compat",
    "headers",
    "launch",
    "description",
)


def _parse_provider(name: str, raw: Any) -> ProviderSpec:
    where = f"providers.{name}"
    if not name or "/" in name or any(c.isspace() for c in name):
        raise ConfigError(f"{where}: provider names must be non-empty, without '/' or whitespace")
    m = _mapping(raw, where)
    _only_keys(m, _PROVIDER_KEYS, where)
    protocol = _req_str(m.get("protocol"), f"{where}.protocol")
    if protocol not in PROTOCOLS:
        raise ConfigError(f"{where}.protocol: expected one of {', '.join(PROTOCOLS)}")
    base_url = _req_str(m.get("base_url"), f"{where}.base_url").rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"{where}.base_url: must start with http:// or https://")
    return ProviderSpec(
        name=name,
        protocol=protocol,
        base_url=base_url,
        auth=_parse_auth(m, where),
        models=_parse_models(m.get("models"), f"{where}.models"),
        match=_str_list(m.get("match"), f"{where}.match"),
        tunnel=_str(m.get("tunnel"), f"{where}.tunnel"),
        default=_bool(m.get("default"), f"{where}.default"),
        compat=_parse_compat(m.get("compat"), f"{where}.compat"),
        headers=_str_dict(m.get("headers"), f"{where}.headers"),
        launch=_parse_launch(m.get("launch"), f"{where}.launch"),
        description=_str(m.get("description"), f"{where}.description"),
    )


def _parse_providers(raw: Any) -> list[ProviderSpec]:
    if raw is None:
        raise ConfigError("providers: at least one provider is required")
    if isinstance(raw, list):
        items = []
        for i, entry in enumerate(raw):
            m = dict(_mapping(entry, f"providers[{i}]"))
            name = _req_str(m.pop("name", None), f"providers[{i}].name")
            items.append((name, m))
    else:
        items = [(str(k), v) for k, v in _mapping(raw, "providers").items()]
    if not items:
        raise ConfigError("providers: at least one provider is required")
    seen: set[str] = set()
    out: list[ProviderSpec] = []
    for name, body in items:
        if name in seen:
            raise ConfigError(f"providers.{name}: duplicate provider name")
        seen.add(name)
        out.append(_parse_provider(name, body))
    defaults = [p.name for p in out if p.default]
    if len(defaults) > 1:
        raise ConfigError(f"providers: only one provider may set default: true (got {', '.join(defaults)})")
    return out


def _parse_claude(raw: Any) -> ClaudeSettings:
    m = _mapping(raw, "claude")
    _only_keys(m, ("command", "args", "env", "default_model"), "claude")
    return ClaudeSettings(
        command=_str(m.get("command"), "claude.command", "claude") or "claude",
        args=_str_list(m.get("args"), "claude.args"),
        env=_str_dict(m.get("env"), "claude.env"),
        default_model=_str(m.get("default_model"), "claude.default_model"),
    )


def parse_config(raw: Any, path: Path) -> Config:
    top = _mapping(raw, "config")
    _only_keys(top, ("gateway", "tunnels", "providers", "claude"), str(path))
    top = interpolate(top)
    cfg = Config(
        path=path,
        gateway=_parse_gateway(top.get("gateway")),
        tunnels=_parse_tunnels(top.get("tunnels")),
        providers=_parse_providers(top.get("providers")),
        claude=_parse_claude(top.get("claude")),
    )
    for p in cfg.providers:
        if p.tunnel and p.tunnel not in cfg.tunnels:
            raise ConfigError(
                f"providers.{p.name}.tunnel: no tunnel named '{p.tunnel}' (defined: {', '.join(cfg.tunnels) or 'none'})"
            )
    return cfg


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    p = resolve_config_path(path)
    if not p.exists():
        raise ConfigError(f"no configuration at {p}; run `llmswitch init` to create one")
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{p}: invalid YAML: {e}") from e
    return parse_config(raw, p)
