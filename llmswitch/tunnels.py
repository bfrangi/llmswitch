"""SSH tunnels as control-master sessions, so they can be checked and closed by name."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .ports import listeners, port_open

if TYPE_CHECKING:
    from .config import Config, TunnelSpec


class TunnelError(Exception):
    pass


@dataclass
class TunnelStatus:
    name: str
    up: bool
    ssh: str
    forwards: list[str]


class TunnelManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.dir = config.gateway.state_dir / "tunnels"

    def socket_path(self, name: str):
        return self.dir / f"{name}.sock"

    def _control(self, spec: TunnelSpec, op: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["ssh", "-S", str(self.socket_path(spec.name)), "-O", op, spec.ssh], capture_output=True, text=True
        )

    def is_up(self, spec: TunnelSpec) -> bool:
        return self.socket_path(spec.name).exists() and self._control(spec, "check").returncode == 0

    def status(self, spec: TunnelSpec) -> TunnelStatus:
        return TunnelStatus(spec.name, self.is_up(spec), spec.ssh, list(spec.forwards))

    def up(self, spec: TunnelSpec, force: bool = False) -> str:
        if self.is_up(spec):
            return "already running"
        self.dir.mkdir(parents=True, exist_ok=True)
        sock = self.socket_path(spec.name)
        if sock.exists():
            sock.unlink()  # a socket without a master behind it: the machine or ssh was restarted
        self._free_ports(spec, force)
        cmd = [
            "ssh",
            "-f",
            "-N",
            "-M",
            "-S",
            str(sock),
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            *spec.options,
        ]
        for fwd in spec.forwards:
            cmd += ["-L", fwd]
        cmd.append(spec.ssh)
        try:
            proc = subprocess.run(cmd)  # inherits the terminal: ssh may ask for a passphrase
        except FileNotFoundError as e:
            raise TunnelError("ssh is not installed or not on PATH") from e
        if proc.returncode != 0:
            raise TunnelError(f"tunnel '{spec.name}': ssh exited with status {proc.returncode}")
        if not self.is_up(spec):
            raise TunnelError(f"tunnel '{spec.name}': ssh returned but its control socket is not answering")
        return "started"

    def _free_ports(self, spec: TunnelSpec, force: bool) -> None:
        for port in spec.local_ports():
            if not port_open("127.0.0.1", port):
                continue
            who = listeners(port)
            desc = ", ".join(str(w) for w in who) or "an unknown process"
            if force and who and all(w.comm == "ssh" for w in who):
                for w in who:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(w.pid, signal.SIGTERM)
                deadline = time.monotonic() + 3
                while port_open("127.0.0.1", port) and time.monotonic() < deadline:
                    time.sleep(0.1)
                if port_open("127.0.0.1", port):
                    raise TunnelError(f"tunnel '{spec.name}': port {port} is still busy after stopping {desc}")
                continue
            hint = "" if force else " (if that is a stale tunnel, rerun with --force)"
            raise TunnelError(f"tunnel '{spec.name}': local port {port} is already in use by {desc}{hint}")

    def down(self, spec: TunnelSpec) -> str:
        sock = self.socket_path(spec.name)
        if not self.is_up(spec):
            if sock.exists():
                sock.unlink()
            return "not running"
        self._control(spec, "exit")
        deadline = time.monotonic() + 5
        while self.is_up(spec) and time.monotonic() < deadline:
            time.sleep(0.1)
        if sock.exists():
            sock.unlink()
        return "stopped"
