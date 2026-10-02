from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from llmswitch.config import ConfigError, TunnelSpec, parse_config, resolve_config_path
from llmswitch.init_template import render_template


def parse(text: str, path: Path = Path("/tmp/c.yaml")):
    return parse_config(yaml.safe_load(text), path)


MINIMAL = """
providers:
  a: {protocol: anthropic, base_url: http://a.test}
"""


def test_defaults() -> None:
    cfg = parse(MINIMAL)
    assert cfg.gateway.port == 11436
    assert cfg.gateway.base_url == "http://127.0.0.1:11436"
    assert cfg.providers[0].auth.mode == "none"
    assert cfg.providers[0].models.source == "none"
    assert cfg.claude.command == "claude"


def test_full_config(config) -> None:
    names = [p.name for p in config.providers]
    assert names == ["anthropic", "ollama", "strict", "openai"]
    anth = config.provider("anthropic")
    assert anth.auth.mode == "passthrough" and anth.match == ["claude-*"]
    assert anth.models.static[0].bare is True
    openai = config.provider("openai")
    assert openai.auth.mode == "key" and openai.auth.key == "sk-test"
    assert openai.base_url == "http://openai.test/v1"
    strict = config.provider("strict")
    assert strict.compat.drop_fields == ["context_management"] and strict.compat.drop_headers == ["anthropic-beta"]
    assert config.tunnels["box"].local_ports() == [11435]
    assert [t.name for t in config.tunnels_in_use()] == ["box"]
    assert config.provider("ollama").launch.env["HUB_MODEL"] == "{model}"


def test_auth_mapping_form() -> None:
    cfg = parse("""
providers:
  a: {protocol: openai, base_url: http://a.test, auth: {mode: key, key: abc, header: X-Api-Key}}
""")
    assert cfg.providers[0].auth.header == "x-api-key"


@pytest.mark.parametrize(
    "snippet, needle",
    [
        ("providers:\n  a: {protocol: carrier-pigeon, base_url: http://a}", "protocol"),
        ("providers:\n  a/b: {protocol: anthropic, base_url: http://a}", "provider names"),
        ("providers:\n  a: {protocol: anthropic, base_url: a.test}", "http://"),
        ("providers:\n  a: {protocol: anthropic, base_url: http://a, tunnel: nope}", "no tunnel named"),
        (
            "providers:\n  a: {protocol: anthropic, base_url: http://a, default: true}\n  b: {protocol: anthropic, base_url: http://b, default: true}",
            "only one provider",
        ),
        ("providers:\n  a: {protocol: anthropic, base_url: http://a, prefix: x}", "unknown key"),
        ("providers:\n  a: {protocol: anthropic, base_url: http://a, auth: key}", "needs a key"),
        ("providers:\n  a: {protocol: anthropic, base_url: http://a, models: {source: magic}}", "models.source"),
        ("gateway: {port: '11436'}\nproviders:\n  a: {protocol: anthropic, base_url: http://a}", "integer"),
        ("providers: {}", "at least one"),
        ("bogus: 1\nproviders:\n  a: {protocol: anthropic, base_url: http://a}", "unknown key"),
    ],
)
def test_rejects_bad_config(snippet: str, needle: str) -> None:
    with pytest.raises(ConfigError, match=needle):
        parse(snippet)


def test_env_interpolation(monkeypatch) -> None:
    monkeypatch.setenv("HUB_T_KEY", "secret")
    monkeypatch.delenv("HUB_T_MISSING", raising=False)
    cfg = parse("""
providers:
  a: {protocol: openai, base_url: "${HUB_T_URL:-http://a.test}/v1", api_key: "${HUB_T_KEY}"}
""")
    assert cfg.providers[0].auth.key == "secret"
    assert cfg.providers[0].base_url == "http://a.test/v1"
    with pytest.raises(ConfigError, match="HUB_T_MISSING"):
        parse('providers:\n  a: {protocol: openai, base_url: http://a, api_key: "${HUB_T_MISSING}"}')


def test_list_form_and_shorthands() -> None:
    cfg = parse("""
providers:
  - name: one
    protocol: anthropic
    base_url: http://one.test/
    models: [m1, {id: m2, display_name: Two}]
  - name: two
    protocol: openai
    base_url: http://two.test
    models: {source: openai, include: ["gpt-*"], exclude: ["*-mini"]}
""")
    assert cfg.providers[0].base_url == "http://one.test"
    assert [m.id for m in cfg.providers[0].models.static] == ["m1", "m2"]
    assert cfg.providers[1].models.include == ["gpt-*"]


def test_resolve_config_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LLMSWITCH_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert resolve_config_path() == tmp_path / "llmswitch" / "config.yaml"
    monkeypatch.setenv("LLMSWITCH_CONFIG", str(tmp_path / "x.yaml"))
    assert resolve_config_path() == tmp_path / "x.yaml"
    assert resolve_config_path(tmp_path / "y.yaml") == tmp_path / "y.yaml"


def test_tunnel_local_ports() -> None:
    t = TunnelSpec("t", "host", ["127.0.0.1:2222:localhost:22", "3333:db:5432"])
    assert t.local_ports() == [2222, 3333]


@pytest.mark.parametrize("local", ["http://127.0.0.1:11434", None])
def test_template_is_valid(local: str | None) -> None:
    cfg = parse(render_template(local))
    assert cfg.provider("anthropic").auth.mode == "passthrough"
    assert (cfg.provider("local") is not None) is bool(local)
