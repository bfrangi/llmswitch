from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from llmswitch import updates
from llmswitch.config import ConfigError
from llmswitch.daemon import GatewayManager
from llmswitch.gateway import Activity, TrackActivity
from llmswitch.updates import Install
from tests.test_config import parse

LS_REMOTE = """\
1111111111111111111111111111111111111111\trefs/tags/v0.3.0
2222222222222222222222222222222222222222\trefs/tags/v0.10.1
3333333333333333333333333333333333333333\trefs/tags/v0.9.0
4444444444444444444444444444444444444444\trefs/tags/v1.0.0-rc1
5555555555555555555555555555555555555555\trefs/tags/nightly
"""


def test_versions_and_the_newest_release() -> None:
    assert updates.parse_version("v1.2.3") == (1, 2, 3) and updates.parse_version("1.2.3-rc1") is None
    assert updates.is_newer("0.10.0", "0.9.9") and not updates.is_newer("0.3.0", "0.3.0")
    assert not updates.is_newer(None, "0.3.0")
    assert updates.newest_release(LS_REMOTE) == "0.10.1", "numeric order; pre-releases and other tags ignored"
    assert updates.newest_release("") is None


def test_the_repository_comes_from_the_package_metadata() -> None:
    assert updates.repository_url() == "https://github.com/bfrangi/llmswitch"


class FakeDist:
    def __init__(self, direct: dict | None) -> None:
        self.direct = direct

    def read_text(self, name: str) -> str | None:
        return json.dumps(self.direct) if self.direct is not None and name == "direct_url.json" else None


@pytest.mark.parametrize(
    ("direct", "receipt", "expected"),
    [
        ({"url": "file:///home/me/llmswitch", "dir_info": {"editable": True}}, True, "checkout"),
        ({"url": "https://github.com/bfrangi/llmswitch", "vcs_info": {"vcs": "git"}}, True, "uv"),
        ({"url": "file:///srv/mirror/llmswitch.git", "vcs_info": {"vcs": "git"}}, True, "uv"),
        ({"url": "https://github.com/bfrangi/llmswitch", "vcs_info": {"vcs": "git"}}, False, "pip"),
        (None, True, "uv"),
    ],
)
def test_how_it_was_installed(monkeypatch, tmp_path: Path, direct, receipt, expected) -> None:
    monkeypatch.setattr(updates.importlib.metadata, "distribution", lambda _name: FakeDist(direct))
    if receipt:
        (tmp_path / "uv-receipt.toml").write_text("[tool]\n")
    monkeypatch.setattr(updates.sys, "prefix", str(tmp_path))
    assert updates.detect_install().tool == expected


def test_install_commands_and_hints(monkeypatch) -> None:
    monkeypatch.setattr(updates.shutil, "which", lambda name: f"/usr/bin/{name}")
    url = "https://github.com/bfrangi/llmswitch"
    assert Install("uv").command(url, "0.4.0") == [
        "uv",
        "tool",
        "install",
        "--force",
        "git+https://github.com/bfrangi/llmswitch@v0.4.0",
    ]
    assert Install("pipx").command(url, "0.4.0")[:3] == ["pipx", "install", "--force"]
    assert Install("pip").command(url, "0.4.0") is None, "a plain pip install is never changed behind your back"
    assert "pip install --upgrade git+https://github.com/bfrangi/llmswitch@v0.4.0" in Install("pip").hint(url, "0.4.0")
    assert Install("checkout", "/src").command(url, "0.4.0") is None
    monkeypatch.setattr(updates.shutil, "which", lambda _name: None)
    assert Install("uv").command(url, "0.4.0") is None, "no uv on PATH: say what to run instead"


# ----------------------------------------------------------------------- launches


@pytest.fixture
def launch(monkeypatch, tmp_path: Path, config):
    config.gateway.state_dir = tmp_path
    spawned: list[int] = []
    monkeypatch.setattr(updates, "__version__", "0.3.0")
    monkeypatch.setattr(updates, "repository_url", lambda: "https://example.test/llmswitch")
    monkeypatch.setattr(updates, "detect_install", lambda: Install("uv"))
    monkeypatch.setattr(updates.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(updates, "spawn_check", lambda _config: spawned.append(1))
    return config, spawned


def test_a_check_runs_at_most_once_per_interval(launch) -> None:
    config, spawned = launch
    assert updates.on_launch(config, now=1000.0) == [] and spawned == [1]
    updates.on_launch(config, now=1000.0 + 3600)
    assert spawned == [1], "not again within check_hours"
    updates.on_launch(config, now=1000.0 + 25 * 3600)
    assert spawned == [1, 1]


def test_auto_mode_announces_an_installed_update_once(launch, monkeypatch) -> None:
    config, _ = launch
    updates.save_state(config, {"checked_at": 1e12, "latest": "0.4.0", "installed": "0.4.0", "previous": "0.3.0"})
    monkeypatch.setattr(updates, "__version__", "0.4.0")
    assert updates.on_launch(config) == ["llmswitch updated: 0.3.0 -> 0.4.0"]
    assert updates.on_launch(config) == []


def test_auto_mode_stays_quiet_while_installing_and_reports_failures(launch) -> None:
    config, _ = launch
    updates.save_state(config, {"checked_at": 1e12, "latest": "0.4.0"})
    assert updates.on_launch(config) == []
    updates.save_state(config, {"checked_at": 1e12, "latest": "0.4.0", "error": "uv exited with status 2"})
    assert "could not update itself to 0.4.0: uv exited with status 2" in updates.on_launch(config)[0]


def test_notify_mode_and_installs_it_cannot_change_say_what_to_run(launch, monkeypatch) -> None:
    config, _ = launch
    config.updates.mode = "notify"
    updates.save_state(config, {"checked_at": 1e12, "latest": "0.4.0"})
    assert updates.on_launch(config) == ["llmswitch 0.4.0 is available (this is 0.3.0): `llmswitch update`"]
    config.updates.mode = "auto"
    monkeypatch.setattr(updates, "detect_install", lambda: Install("pip"))
    assert "pip install --upgrade" in updates.on_launch(config)[0]


def test_checkouts_and_off_mode_are_left_alone(launch, monkeypatch) -> None:
    config, spawned = launch
    config.updates.mode = "off"
    assert updates.on_launch(config) == [] and spawned == []
    config.updates.mode = "auto"
    monkeypatch.setattr(updates, "detect_install", lambda: Install("checkout", "/src"))
    assert updates.on_launch(config) == [] and spawned == []


def test_the_background_check_installs_a_newer_release(launch, monkeypatch) -> None:
    config, _ = launch
    ran: list[list[str]] = []
    monkeypatch.setattr(updates, "fetch_newest_release", lambda _url: "0.4.0")
    monkeypatch.setattr(
        updates.subprocess, "run", lambda cmd, **_kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0)
    )
    state = updates.check_and_install(config, install=True)
    assert ran == [["uv", "tool", "install", "--force", "git+https://example.test/llmswitch@v0.4.0"]]
    assert state["installed"] == "0.4.0" and state["previous"] == "0.3.0" and state["error"] is None
    ran.clear()
    updates.check_and_install(config, install=False)
    assert ran == [], "notify mode only looks"


def test_a_failed_install_is_remembered(launch, monkeypatch) -> None:
    config, _ = launch
    monkeypatch.setattr(updates, "fetch_newest_release", lambda _url: "0.4.0")
    monkeypatch.setattr(updates.subprocess, "run", lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 2))
    state = updates.check_and_install(config, install=True)
    assert "exited with status 2" in state["error"] and "installed" not in state


def test_update_settings_parse() -> None:
    base = "providers:\n  a: {protocol: anthropic, base_url: http://a.test}\n"
    assert parse(base).updates.mode == "auto"
    assert parse(base + "updates: notify\n").updates.mode == "notify"
    assert parse(base + "updates: off\n").updates.mode == "off", "YAML reads a bare off as false"
    assert parse(base + "updates: {mode: off, check_hours: 6}\n").updates.check_hours == 6
    with pytest.raises(ConfigError, match="updates"):
        parse(base + "updates: sometimes\n")


# --------------------------------------------------------------------- the gateway


def test_an_outdated_gateway_is_replaced_only_when_idle(config, monkeypatch) -> None:
    gm = GatewayManager(config)
    calls: list[str] = []
    monkeypatch.setattr(gm, "down", lambda: calls.append("down") or "stopped")
    monkeypatch.setattr(gm, "up", lambda: calls.append("up") or "started")
    monkeypatch.setattr(gm, "health", lambda: {"version": "0.4.0", "active_requests": 0})
    assert gm.replace_if_outdated("0.4.0") is None and calls == []
    monkeypatch.setattr(gm, "health", lambda: {"version": "0.3.0", "active_requests": 2})
    assert "2 request(s) are in progress" in gm.replace_if_outdated("0.4.0") and calls == []
    monkeypatch.setattr(gm, "health", lambda: {"version": "0.3.0"})
    assert "does not report" in gm.replace_if_outdated("0.4.0") and calls == []
    monkeypatch.setattr(gm, "health", lambda: {"version": "0.3.0", "active_requests": 0})
    assert gm.replace_if_outdated("0.4.0") == "replaced 0.3.0 with 0.4.0" and calls == ["down", "up"]


@pytest.mark.anyio
async def test_a_streamed_answer_counts_as_active_until_its_last_chunk() -> None:
    activity = Activity()
    seen: list[int] = []

    async def app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        seen.append(activity.active)
        await send({"type": "http.response.body", "body": b"b", "more_body": False})
        seen.append(activity.active)

    async def send(_message) -> None:
        return None

    tracked = TrackActivity(app, activity)
    await tracked({"type": "http", "path": "/v1/messages"}, None, send)
    await tracked({"type": "http", "path": "/healthz"}, None, send)
    assert seen == [1, 0, 0, 0] and activity.active == 0
