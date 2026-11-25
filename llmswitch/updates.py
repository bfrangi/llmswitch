"""Keeping llmswitch current from the project's tagged releases.

A release is a git tag ``v<major>.<minor>.<patch>`` in the repository the package metadata
names. The launcher looks for one at most every ``updates.check_hours``, in a detached
process so that a launch never waits on the network; with ``updates: auto`` that process
also installs the release with the tool that installed llmswitch (``uv tool`` or ``pipx``),
and the next launch replaces the gateway once it is idle. A checkout installed from a
directory is never touched: its owner updates it.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from .config import APP_NAME, ConfigError, load_config

if TYPE_CHECKING:
    from .config import Config

STATE_FILE = "update.json"
LOG_FILE = "update.log"
REPOSITORY_LABELS = ("repository", "source", "source code")
_TAG = re.compile(r"refs/tags/v(\d+)\.(\d+)\.(\d+)$")


def parse_version(text: str) -> tuple[int, int, int] | None:
    """``1.2.3`` or ``v1.2.3`` as a comparable tuple; anything else (pre-releases too) is None."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def is_newer(candidate: str | None, current: str) -> bool:
    new, now = parse_version(candidate or ""), parse_version(current)
    return bool(new and now and new > now)


def newest_release(ls_remote: str) -> str | None:
    """The highest ``v<x>.<y>.<z>`` tag in ``git ls-remote --tags`` output, without the v."""
    versions = [
        (int(m[1]), int(m[2]), int(m[3])) for line in ls_remote.splitlines() if (m := _TAG.search(line.strip()))
    ]
    return ".".join(map(str, max(versions))) if versions else None


def repository_url() -> str | None:
    """The source repository named in the installed package's metadata."""
    try:
        urls = importlib.metadata.metadata(APP_NAME).get_all("Project-URL") or []
    except importlib.metadata.PackageNotFoundError:
        return None
    for entry in urls:
        label, _, url = entry.partition(",")
        if label.strip().lower() in REPOSITORY_LABELS and url.strip():
            return url.strip()
    return None


def fetch_newest_release(url: str, timeout: float = 30.0) -> str | None:
    out = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", url],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},  # never stop to ask for credentials
    )
    return newest_release(out.stdout)


# ------------------------------------------------------------------- installing


@dataclass(frozen=True)
class Install:
    """How this copy of llmswitch was installed, which decides how it can be updated."""

    tool: str  # "uv", "pipx", "pip", or "checkout" (a local directory, updated by its owner)
    location: str = ""  # the directory, for a checkout

    def command(self, url: str, version: str) -> list[str] | None:
        """The command that installs ``version``, or None when it has to be done by hand."""
        spec = f"git+{url}@v{version}"
        if self.tool == "uv" and shutil.which("uv"):
            return ["uv", "tool", "install", "--force", spec]
        if self.tool == "pipx" and shutil.which("pipx"):
            return ["pipx", "install", "--force", spec]
        return None

    def hint(self, url: str, version: str) -> str:
        """What to run by hand."""
        if self.tool == "checkout":
            return f"update the checkout at {self.location} (git pull), then `llmswitch restart`"
        command = self.command(url, version)
        if command:
            return "`llmswitch update`"
        return f"`{sys.executable} -m pip install --upgrade git+{url}@v{version}`"


def detect_install() -> Install:
    try:
        dist = importlib.metadata.distribution(APP_NAME)
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
    except (importlib.metadata.PackageNotFoundError, ValueError):
        direct = {}
    if "dir_info" in direct:  # installed from a directory (PEP 610); a git or archive source says otherwise
        return Install("checkout", str(direct.get("url", "")).removeprefix("file://"))
    prefix = Path(sys.prefix)
    if (prefix / "uv-receipt.toml").exists():
        return Install("uv")
    if "pipx" in prefix.parts:
        return Install("pipx")
    return Install("pip")


# ------------------------------------------------------------------------ state


def state_path(config: Config) -> Path:
    return config.gateway.state_dir / STATE_FILE


def load_state(config: Config) -> dict[str, Any]:
    try:
        data = json.loads(state_path(config).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(config: Config, state: dict[str, Any]) -> None:
    path = state_path(config)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(path)


def check_and_install(config: Config, *, install: bool, log: Any = None) -> dict[str, Any]:
    """Look for a newer release and, when ``install`` is set, install it. Returns the new state."""
    state = load_state(config)
    url = repository_url()
    state.update(checked_at=time.time(), error=None)
    try:
        if not url:
            raise RuntimeError("the package metadata names no repository")
        latest = fetch_newest_release(url)
        state["latest"] = latest
        how = detect_install()
        command = how.command(url, latest) if latest else None
        if install and command and is_newer(latest, __version__):
            done = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, check=False)
            if done.returncode:
                raise RuntimeError(f"`{' '.join(command)}` exited with status {done.returncode}")
            state.update(installed=latest, previous=__version__)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        state["error"] = str(e)
    save_state(config, state)
    return state


# ------------------------------------------------------------------- the launcher


def on_launch(config: Config, *, now: float | None = None) -> list[str]:
    """Lines for the launcher to print, after starting a background check when one is due."""
    settings = config.updates
    if settings.mode == "off":
        return []
    how = detect_install()
    if how.tool == "checkout":
        return []
    now = time.time() if now is None else now
    state = load_state(config)
    url = repository_url() or ""
    lines: list[str] = []
    latest = state.get("latest")
    if state.get("installed") == __version__ and state.get("announced") != __version__:
        lines.append(f"llmswitch updated: {state.get('previous') or 'older'} -> {__version__}")
        state["announced"] = __version__
    elif is_newer(latest, __version__):
        if settings.mode == "notify" or not how.command(url, latest):
            lines.append(f"llmswitch {latest} is available (this is {__version__}): {how.hint(url, latest)}")
        elif state.get("error"):
            log = config.gateway.state_dir / LOG_FILE
            lines.append(f"llmswitch could not update itself to {latest}: {state['error']} (see {log})")
    last = state.get("checked_at")
    if not isinstance(last, (int, float)) or now - last >= settings.check_hours * 3600:
        state["checked_at"] = now  # claimed, so launches in the meantime do not start a second check
        spawn_check(config)
    save_state(config, state)
    return lines


def spawn_check(config: Config) -> None:
    """Run the check (and an install, in auto mode) in a detached process that outlives the launcher."""
    config.gateway.state_dir.mkdir(parents=True, exist_ok=True)
    with (config.gateway.state_dir / LOG_FILE).open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "llmswitch.updates", "--config", str(config.path)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )


def main(argv: list[str] | None = None) -> int:
    """The background check started by ``spawn_check``."""
    ap = argparse.ArgumentParser(prog="python -m llmswitch.updates")
    ap.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"llmswitch: {e}", file=sys.stderr)
        return 2
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} checking for a release newer than {__version__}", flush=True)
    state = check_and_install(config, install=config.updates.mode == "auto", log=sys.stdout)
    print(f"latest {state.get('latest')}; installed {state.get('installed')}; error {state.get('error')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
