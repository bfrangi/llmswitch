"""Command-line entry points: ``ai-hub`` (manage) and ``ai-hub-shell`` (launch Claude Code)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

from . import __version__
from .config import ConfigError, load_config, resolve_config_path
from .daemon import GatewayError, GatewayManager
from .init_template import render_template
from .picker import PickerQuit, pick
from .ports import port_open
from .tunnels import TunnelError, TunnelManager

if TYPE_CHECKING:
    from .config import Config


def _err(msg: str) -> None:
    print(f"ai-hub: {msg}", file=sys.stderr)


def _load(path: str | None) -> Config:
    try:
        return load_config(path)
    except ConfigError as e:
        raise SystemExit(f"ai-hub: {e}") from e


# ------------------------------------------------------------------ discovery


def offline_models(config: Config) -> dict[str, Any]:
    """Probe providers directly, for when no gateway is running."""
    from .catalog import Catalog
    from .gateway import make_http_client, provider_report
    from .providers import build_provider

    async def run() -> dict[str, Any]:
        async with make_http_client(config) as http:
            providers = {s.name: build_provider(s, http) for s in config.providers}
            catalog = Catalog(providers, ttl=config.hub.discovery_ttl, timeout=config.hub.discovery_timeout)
            await catalog.refresh()
            return {
                "models": [m.to_dict(catalog.specs[m.provider]) for m in catalog.models()],
                "providers": provider_report(config, catalog),
            }

    return asyncio.run(run())


def fetch_models(config: Config, gm: GatewayManager | None = None) -> dict[str, Any]:
    gm = gm or GatewayManager(config)
    if gm.is_up():
        return gm.get_json("/hub/models?refresh=1")
    return offline_models(config)


def find_entry(models: list[dict[str, Any]], chosen: str) -> dict[str, Any] | None:
    for key in ("launch_id", "id", "upstream_id"):
        hits = [m for m in models if m.get(key) == chosen]
        if len(hits) == 1:
            return hits[0]
    return None


# ----------------------------------------------------------------- launching

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][\w.]*)\}")


def render_value(value: str, ctx: dict[str, str], details: dict[str, Any]) -> str | None:
    """Fill ``{model}``-style placeholders; None when one of them has no value."""
    missing = False

    def repl(m: re.Match[str]) -> str:
        nonlocal missing
        key = m.group(1)
        v = details.get(key[len("details.") :]) if key.startswith("details.") else ctx.get(key)
        if v in (None, ""):
            missing = True
            return ""
        return str(v)

    out = _PLACEHOLDER.sub(repl, value)
    return None if missing else out


def launch_env(config: Config, chosen: str | None, models: list[dict[str, Any]]) -> dict[str, str]:
    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = config.hub.base_url
    entry = find_entry(models, chosen) if chosen else None
    spec = config.provider(entry["provider"]) if entry else None
    ctx = {
        "model": chosen or "",
        "upstream_model": entry["upstream_id"] if entry else (chosen or ""),
        "provider": entry["provider"] if entry else "",
    }
    details = (entry or {}).get("details") or {}
    layers = [config.claude.env] + ([spec.launch.env] if spec else [])
    for layer in layers:
        for key, value in layer.items():
            rendered = render_value(value, ctx, details)
            if rendered is None:
                continue
            env[key] = rendered
    return env


def ensure_up(config: Config, force: bool = False) -> GatewayManager:
    tm = TunnelManager(config)
    for spec in config.tunnels_in_use():
        try:
            print(f"tunnel {spec.name}: {tm.up(spec, force=force)}")
        except TunnelError as e:
            _err(str(e))
            _err(f"continuing without tunnel '{spec.name}'")
    gm = GatewayManager(config)
    print(f"gateway: {gm.up(force=force)} at {gm.base_url}")
    return gm


# ------------------------------------------------------------------ commands


def _detect_local_ollama() -> str | None:
    """Pre-fill the template with a local Ollama if one answers at its usual address."""
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    h, _, p = host.rpartition(":")
    if not h or not p.isdigit():
        h, p = host, "11434"
    h = h or "127.0.0.1"
    return f"http://{h}:{p}" if port_open(h, int(p)) else None


def cmd_init(args: argparse.Namespace) -> int:
    path = resolve_config_path(args.config)
    if path.exists() and not args.force:
        _err(f"{path} already exists (use --force to overwrite it)")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    local = _detect_local_ollama()
    path.write_text(render_template(local))
    print(f"wrote {path}")
    print(f"local Ollama: {'found at ' + local if local else 'not detected (left commented out)'}")
    print("edit it, then run `ai-hub-shell`")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    config = _load(args.config)
    ensure_up(config, force=args.force)
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    config = _load(args.config)
    print(f"gateway: {GatewayManager(config).down()}")
    tm = TunnelManager(config)
    for spec in config.tunnels.values():
        print(f"tunnel {spec.name}: {tm.down(spec)}")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    config = _load(args.config)
    print(f"gateway: {GatewayManager(config).down()}")
    ensure_up(config, force=args.force)
    return 0


def _age(started_at: float | None) -> str:
    if not started_at:
        return ""
    s = int(time.time() - started_at)
    h, m = divmod(s // 60, 60)
    return f"up {h}h{m:02d}m" if h else f"up {m}m{s % 60:02d}s"


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args.config)
    gm = GatewayManager(config)
    health = gm.health()
    tm = TunnelManager(config)
    tunnels = {t.name: tm.is_up(t) for t in config.tunnels.values()}
    providers = health["providers"] if health else offline_models(config)["providers"]
    if args.json:
        print(
            json.dumps(
                {"config": str(config.path), "gateway": health, "tunnels": tunnels, "providers": providers}, indent=2
            )
        )
        return 0
    print(f"ai-hub {__version__}")
    print(f"config    {config.path}")
    if health:
        print(f"gateway   up      {gm.base_url}   pid {health.get('pid')}   {_age(health.get('started_at'))}")
    else:
        print(f"gateway   down    (would listen on {gm.base_url})")
    for t in config.tunnels.values():
        print(f"tunnel    {t.name:<10} {'up' if tunnels[t.name] else 'down':<7} {t.ssh}  {' '.join(t.forwards)}")
    for name, p in providers.items():
        state = "ok" if p["ok"] else "DOWN"
        extra = f"{p['models']} models" if p["ok"] else (p.get("error") or "")
        auth = f"  auth: {p['auth']}" if p["auth"] != "none" else ""
        print(f"provider  {name:<10} {state:<7} {p['protocol']:<10} {p['base_url']}{auth}   {extra}")
    if not health:
        print("\n(gateway is down; provider lines come from a direct probe)")
    return 0


def print_models(data: dict[str, Any], order: list[str]) -> None:
    models, providers = data["models"], data["providers"]
    width = min(max([len(m["launch_id"]) for m in models] + [20]), 56)
    for name in order:
        p = providers.get(name, {})
        state = "" if p.get("ok") else f"   unavailable: {p.get('error') or ''}"
        print(f"\n{name}   {p.get('base_url', '')}{state}")
        for m in [m for m in models if m["provider"] == name]:
            print(f"  {m['launch_id']:<{width}}  {m.get('description') or ''}")
    print("\nuse a name with `ai-hub-shell --model <name>`, or `/model <name>` inside Claude Code")


def cmd_models(args: argparse.Namespace) -> int:
    config = _load(args.config)
    data = offline_models(config) if args.offline else fetch_models(config)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print_models(data, [p.name for p in config.providers])
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    config = _load(args.config)
    gm = GatewayManager(config)
    if args.follow:
        try:
            return subprocess.call(["tail", "-n", str(args.lines), "-f", str(gm.logfile)])
        except FileNotFoundError:
            pass
    print(gm.tail(args.lines))
    return 0


def cmd_gateway(args: argparse.Namespace) -> int:
    from .gateway import main as gateway_main

    extra = [a for a in (args.extra or []) if a != "--"]
    return gateway_main((["--config", args.config] if args.config else []) + extra)


def cmd_config(args: argparse.Namespace) -> int:
    path = resolve_config_path(args.config)
    if args.edit:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        return subprocess.call([editor, str(path)])
    print(path)
    if not path.exists():
        print("(missing; run `ai-hub init`)")
        return 1
    try:
        cfg = load_config(path)
    except ConfigError as e:
        print(f"invalid: {e}")
        return 1
    print(f"valid: {len(cfg.providers)} provider(s), {len(cfg.tunnels)} tunnel(s), hub at {cfg.hub.base_url}")
    return 0


# -------------------------------------------------------------------- parsers


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "shell":
        return main_shell(argv[1:])
    ap = argparse.ArgumentParser(
        prog="ai-hub", allow_abbrev=False, description="One local endpoint in front of every LLM server you use."
    )
    ap.add_argument("--config", "-c", help="config file (default: $AI_HUB_CONFIG or ~/.config/ai-hub/config.yaml)")
    ap.add_argument("--version", action="version", version=f"ai-hub {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="command")
    sub.required = True

    p = sub.add_parser("init", help="write a starter config file")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("up", help="start tunnels and the gateway (idempotent)")
    p.add_argument(
        "--force", "-f", action="store_true", help="replace stale tunnels or an old gateway holding the ports"
    )
    p.set_defaults(fn=cmd_up)
    p = sub.add_parser("down", help="stop the gateway and all tunnels")
    p.set_defaults(fn=cmd_down)
    p = sub.add_parser("restart", help="restart the gateway (after editing the config)")
    p.add_argument("--force", "-f", action="store_true")
    p.set_defaults(fn=cmd_restart)
    p = sub.add_parser("status", help="show gateway, tunnels, and providers")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("models", help="list every model the hub can route")
    p.add_argument("--json", action="store_true")
    p.add_argument("--offline", action="store_true", help="probe providers directly instead of asking the gateway")
    p.set_defaults(fn=cmd_models)
    p = sub.add_parser("logs", help="show the gateway log")
    p.add_argument("-n", "--lines", type=int, default=40)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(fn=cmd_logs)
    p = sub.add_parser("gateway", help="run the gateway in the foreground")
    p.add_argument("extra", nargs=argparse.REMAINDER, help="arguments for the gateway (--host, --port, --log-level)")
    p.set_defaults(fn=cmd_gateway)
    p = sub.add_parser("config", help="show the config path and whether it is valid")
    p.add_argument("--edit", action="store_true", help="open it in $EDITOR")
    p.set_defaults(fn=cmd_config)
    sub.add_parser("shell", help="launch Claude Code through the hub (same as ai-hub-shell)")

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (GatewayError, TunnelError, ConfigError) as e:
        _err(str(e))
        return 1
    except KeyboardInterrupt:
        return 130


SHELL_EPILOG = """\
Anything not listed above is passed to Claude Code unchanged, for example:
  ai-hub-shell --resume                 ai-hub-shell --model local/qwen3:14b -p "explain this repo"
"""


def main_shell(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(
        prog="ai-hub-shell",
        allow_abbrev=False,
        epilog=SHELL_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Start the hub (tunnels + gateway) if needed, pick a model, and launch Claude Code through it.",
    )
    ap.add_argument("--config", "-c", help="config file (default: $AI_HUB_CONFIG or ~/.config/ai-hub/config.yaml)")
    ap.add_argument("--model", help="model to launch with (skips the picker); use <provider>/<model>")
    ap.add_argument("--list", action="store_true", help="list models and exit")
    ap.add_argument("--no-picker", action="store_true", help="never prompt; use claude.default_model")
    ap.add_argument(
        "--force", "-f", action="store_true", help="replace stale tunnels or an old gateway holding the ports"
    )
    ap.add_argument("--init", action="store_true", help="write a starter config and exit")
    args, rest = ap.parse_known_args(argv)
    if rest and rest[0] == "--":
        rest = rest[1:]
    try:
        if args.init:
            return cmd_init(args)
        config = _load(args.config)
        if args.list:
            print_models(fetch_models(config), [p.name for p in config.providers])
            return 0
        gm = ensure_up(config, force=args.force)
        data = gm.get_json("/hub/models?refresh=1")
        chosen: str | None = args.model
        if chosen is None and not args.no_picker and sys.stdin.isatty() and sys.stdout.isatty():
            default_label = (
                f"{config.claude.default_model}" if config.claude.default_model else "Claude Code's own default"
            )
            entry = pick(
                data["models"], data["providers"], [p.name for p in config.providers], default_label=default_label
            )
            chosen = entry["launch_id"] if entry else config.claude.default_model
        elif chosen is None:
            chosen = config.claude.default_model
        env = launch_env(config, chosen, data["models"])
        cmd = [config.claude.command, *config.claude.args]
        if chosen:
            cmd += ["--model", chosen]
        cmd += rest
        exe = shutil.which(config.claude.command) or config.claude.command
        print(f"launching: {' '.join(cmd)}  (ANTHROPIC_BASE_URL={env['ANTHROPIC_BASE_URL']})")
        os.execvpe(exe, cmd, env)
    except FileNotFoundError:
        _err(f"cannot run '{config.claude.command}'; set claude.command in {config.path}")
        return 127
    except PickerQuit:
        return 130
    except (GatewayError, TunnelError, ConfigError) as e:
        _err(str(e))
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
