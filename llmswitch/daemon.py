"""The gateway as a background process: start it detached, find it again, stop it."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import APP_NAME
from .ports import listeners, port_open

if TYPE_CHECKING:
    from .config import Config


class GatewayError(Exception):
    pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


LEGACY_NAMES = ("hub.gateway", "hub.proxy", "ai-hub")  # what earlier versions of this tool were called


def _looks_like_gateway(cmdline: str) -> bool:
    return APP_NAME in cmdline or any(name in cmdline for name in LEGACY_NAMES)


class GatewayManager:
    START_TIMEOUT = 20.0

    def __init__(self, config: Config) -> None:
        self.config = config
        self.state_dir = config.gateway.state_dir
        self.pidfile = self.state_dir / "gateway.pid"
        self.logfile = self.state_dir / "gateway.log"

    @property
    def base_url(self) -> str:
        return self.config.gateway.base_url

    # ------------------------------------------------------------------- probes
    def get_json(self, path: str, timeout: float = 15.0) -> Any:
        try:
            with urllib.request.urlopen(f"{self.base_url}{path}", timeout=timeout) as r:
                return json.load(r)
        except (OSError, ValueError, urllib.error.URLError) as e:
            raise GatewayError(f"gateway at {self.base_url} did not answer {path}: {e}") from e

    def health(self, timeout: float = 1.5) -> dict[str, Any] | None:
        try:
            data = self.get_json("/healthz", timeout=timeout)
        except GatewayError:
            return None
        return data if isinstance(data, dict) and data.get("service") == APP_NAME else None

    def is_up(self) -> bool:
        return self.health() is not None

    def pid(self) -> int | None:
        h = self.health()
        if h and h.get("pid"):
            return int(h["pid"])
        try:
            return int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None

    def tail(self, n: int = 15) -> str:
        try:
            lines = self.logfile.read_text(errors="replace").splitlines()
        except OSError:
            return "(no log yet)"
        return "\n".join(lines[-n:])

    # ---------------------------------------------------------------- lifecycle
    def up(self, force: bool = False) -> str:
        h = self.health()
        if h:
            running = h.get("config")
            if running and Path(running).expanduser().resolve() != self.config.path.resolve():
                raise GatewayError(
                    f"a gateway is already running on {self.base_url} with another config ({running}); "
                    "run `llmswitch down` first or point --config at that file"
                )
            return "already running"
        port, host = self.config.gateway.port, "127.0.0.1"
        if port_open(host, port):
            who = listeners(port)
            desc = ", ".join(str(w) for w in who) or "an unknown process"
            if force and who and all(_looks_like_gateway(w.cmdline) for w in who):
                for w in who:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(w.pid, signal.SIGTERM)
                deadline = time.monotonic() + 5
                while port_open(host, port) and time.monotonic() < deadline:
                    time.sleep(0.1)
            if port_open(host, port):
                raise GatewayError(
                    f"port {port} is in use by {desc}; stop it, change gateway.port, or add --force if it is an old llmswitch"
                )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.logfile.open("ab") as logf:
            proc = subprocess.Popen(
                [sys.executable, "-m", "llmswitch.gateway", "--config", str(self.config.path)],
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                cwd=str(Path.home()),
            )
        self.pidfile.write_text(str(proc.pid))
        deadline = time.monotonic() + self.START_TIMEOUT
        while time.monotonic() < deadline:
            rc = proc.poll()
            if rc is not None:
                raise GatewayError(f"gateway exited with status {rc}; last log lines ({self.logfile}):\n{self.tail()}")
            if self.health():
                return "started"
            time.sleep(0.2)
        raise GatewayError(
            f"gateway did not answer within {self.START_TIMEOUT:g}s; last log lines ({self.logfile}):\n{self.tail()}"
        )

    def down(self) -> str:
        pid = self.pid()
        if pid is None or not _alive(pid):
            self.pidfile.unlink(missing_ok=True)
            return "not running"
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self.pidfile.unlink(missing_ok=True)
            return "not running"
        deadline = time.monotonic() + 8
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
        self.pidfile.unlink(missing_ok=True)
        return "stopped"
