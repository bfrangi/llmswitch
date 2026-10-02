"""Small helpers to ask the OS about TCP ports, without extra dependencies."""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class Listener:
    pid: int
    comm: str
    cmdline: str

    def __str__(self) -> str:
        return f"{self.comm} (pid {self.pid})"


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        if raw:
            return raw.replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        cmd = _cmdline(pid)
        return Path(cmd.split(" ", 1)[0]).name if cmd else "?"


def listeners(port: int) -> list[Listener]:
    """Processes listening on ``port`` (Linux via ``ss``, otherwise ``lsof``)."""
    pids: set[int] = set()
    if shutil.which("ss"):
        try:
            out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True, timeout=3).stdout
            pids.update(int(p) for p in re.findall(r"pid=(\d+)", out))
        except (OSError, subprocess.SubprocessError):
            pass
    if not pids and shutil.which("lsof"):
        try:
            out = subprocess.run(
                ["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=3
            ).stdout
            pids.update(int(p) for p in out.split())
        except (OSError, subprocess.SubprocessError):
            pass
    return [Listener(pid, _comm(pid), _cmdline(pid)) for pid in sorted(pids)]


def describe_listeners(port: int) -> str:
    who = listeners(port)
    return ", ".join(str(w) for w in who) if who else "an unknown process"
