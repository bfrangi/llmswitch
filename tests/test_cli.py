from __future__ import annotations

from pathlib import Path

import pytest

from llmswitch import cli
from llmswitch.config import load_config

MODELS = [
    {
        "id": "ollama/qwen3:14b",
        "launch_id": "ollama/qwen3:14b",
        "provider": "ollama",
        "upstream_id": "qwen3:14b",
        "details": {"context_length": 40960},
    },
    {"id": "anthropic/opus", "launch_id": "opus", "provider": "anthropic", "upstream_id": "opus", "details": {}},
]


def test_render_value() -> None:
    ctx = {"model": "m", "provider": "p"}
    assert cli.render_value("x-{model}-{provider}", ctx, {}) == "x-m-p"
    assert cli.render_value("{details.context_length}", ctx, {"context_length": 40960}) == "40960"
    assert cli.render_value("{details.missing}", ctx, {}) is None
    assert cli.render_value("{model}", {"model": ""}, {}) is None
    assert cli.render_value("plain", {}, {}) == "plain"


def test_launch_env_layers(config, monkeypatch) -> None:
    monkeypatch.delenv("HUB_MODEL", raising=False)
    env = cli.launch_env(config, "ollama/qwen3:14b", MODELS)
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:11436"
    assert env["HUB_BASE"] == "always" and env["HUB_PROVIDER"] == "ollama"
    assert env["HUB_MODEL"] == "ollama/qwen3:14b" and env["HUB_CTX"] == "40960"
    env = cli.launch_env(config, "opus", MODELS)
    assert "HUB_MODEL" not in env and env["HUB_PROVIDER"] == "anthropic"
    env = cli.launch_env(config, None, MODELS)
    assert "HUB_PROVIDER" not in env and env["HUB_BASE"] == "always"


def test_find_entry() -> None:
    assert cli.find_entry(MODELS, "opus")["provider"] == "anthropic"
    assert cli.find_entry(MODELS, "anthropic/opus")["provider"] == "anthropic"
    assert cli.find_entry(MODELS, "qwen3:14b")["provider"] == "ollama"
    assert cli.find_entry(MODELS, "nope") is None


def test_init_writes_and_refuses_overwrite(tmp_path: Path, capsys) -> None:
    path = tmp_path / "cfg" / "config.yaml"
    assert cli.main(["--config", str(path), "init"]) == 0
    cfg = load_config(path)
    assert cfg.provider("anthropic").auth.mode == "passthrough"
    assert cli.main(["--config", str(path), "init"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["--config", str(path), "init", "--force"]) == 0


def test_config_command(tmp_path: Path, capsys) -> None:
    path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(path), "config"]) == 1
    path.write_text("providers:\n  a: {protocol: anthropic, base_url: http://a.test}\n")
    assert cli.main(["--config", str(path), "config"]) == 0
    assert "valid: 1 provider(s)" in capsys.readouterr().out


def test_missing_config_is_a_clean_error(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(tmp_path / "none.yaml"), "status"])
    assert "llmswitch init" in str(e.value)


def test_help_texts(capsys) -> None:
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "launches Claude Code" in out and "init" in out and "status" in out
    with pytest.raises(SystemExit) as e:
        cli.main(["claude", "--help"])
    assert e.value.code == 0
    assert "passed to Claude Code" in capsys.readouterr().out


def test_global_config_flag_is_accepted_before_the_command(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(path), "init"]) == 0
    assert cli.main([f"--config={path}", "config"]) == 0
    assert cli._split_global_config(["--model", "x", "-p", "hi"]) == (None, ["--model", "x", "-p", "hi"])
