# ai-hub

One local endpoint in front of every LLM server you use, so Claude Code can run on
Anthropic's models, on an Ollama on this machine, on an Ollama on another machine,
or on any OpenAI-compatible server, and switch between them by model name.

```
                          ┌──────────────────────────── ai-hub gateway (127.0.0.1:11436) ───┐
 Claude Code ──────────►  │  POST /v1/messages   model: "workstation/qwen3-coder:30b"           │
 ANTHROPIC_BASE_URL       │        │                                                        │
                          │        ▼  route by model name                                   │
                          │   claude-*  ──────────► api.anthropic.com   (your login, untouched)
                          │   local/…   ──────────► http://127.0.0.1:11434   (Ollama)        │
                          │   workstation/…  ──────────► http://127.0.0.1:11435 ──ssh tunnel──► workstation
                          │   other/…   ──────────► https://host/v1   (OpenAI-style, translated)
                          └────────────────────────────────────────────────────────────────┘
```

The gateway speaks the Anthropic Messages API. Upstreams that speak it too (Anthropic,
Ollama, and most current local servers) are plain pass-throughs: the request goes out
unchanged except for the model name, and the response streams back byte for byte.
Upstreams that only speak OpenAI chat completions get translated both ways.

Nothing in the code names a host, a port, or a model. The configuration file does.

## Install

Requires Python 3.10+ and [`uv`](https://github.com/astral-sh/uv); `ssh` for tunnels.

```bash
uv tool install .            # from this directory; rerun with --reinstall to upgrade
ai-hub init                  # writes ~/.config/ai-hub/config.yaml
$EDITOR ~/.config/ai-hub/config.yaml
ai-hub-shell                 # starts what is needed, shows the model picker, launches Claude Code
```

`ai-hub init` pre-fills a `local` provider if an Ollama answers on this machine, and
leaves commented examples for a tunnelled remote machine and an OpenAI-style server.

## Daily use

```bash
ai-hub-shell                                  # picker, then Claude Code
ai-hub-shell --model workstation/qwen3-coder:30b   # skip the picker
ai-hub-shell --model local/qwen3:14b -p "summarise this repo"   # anything else goes to Claude Code
ai-hub-shell --resume                         # same
ai-hub models                                 # everything the hub can route, grouped by provider
ai-hub status                                 # gateway, tunnels, providers
ai-hub down                                   # stop the gateway and the tunnels
```

Inside Claude Code, `/model workstation/qwen3-coder:30b` switches models at any time; the
name just has to be one the hub routes. Pressing Enter in the picker launches Claude
Code with its own default model, which goes to Anthropic through the hub.

The gateway and the tunnels stay up after Claude Code exits and are reused by the
next launch, so several sessions can share them. `ai-hub restart` reloads the config.

### Model names

| You write                     | Routed to                                   | Rule          |
| ----------------------------- | ------------------------------------------- | ------------- |
| `workstation/qwen3-coder:30b`      | provider `workstation`, model `qwen3-coder:30b`  | prefix        |
| `claude-opus-5-5`, `sonnet`   | the provider whose `match` globs fit        | match         |
| `qwen3:14b`                   | the one provider that lists it              | discovered    |
| anything else                 | the provider with `default: true`, if any   | default       |

A bare name present on two providers is an error that names both candidates.
Aliases such as `opus` are expanded by Claude Code itself before they reach the hub.

## Configuration

`~/.config/ai-hub/config.yaml`, or `$AI_HUB_CONFIG`, or `--config PATH`. Strings may use
`${VAR}` and `${VAR:-default}`. Unknown keys are rejected, so typos surface at once.

```yaml
hub:
  host: 127.0.0.1            # bind address (default). Keep loopback: your Anthropic login passes through here.
  port: 11436
  # state_dir: ~/.local/state/ai-hub     # logs, pidfile, tunnel sockets
  # discovery_ttl: 30                    # seconds a provider's model list is cached
  # discovery_timeout: 5                 # per-provider listing timeout
  # connect_timeout: 10                  # upstream TCP connect timeout
  # log_level: info

tunnels:
  workstation:
    ssh: workstation                          # anything `ssh` accepts
    forward: 11435:localhost:11434       # `ssh -L` syntax; a list is fine
    # options: ["-J", "bastion"]         # extra ssh arguments

providers:                               # a mapping (or a list with `name:` keys); order is picker order
  anthropic:
    protocol: anthropic                  # anthropic | openai
    base_url: https://api.anthropic.com  # for `openai`, include the /v1 the server expects
    auth: passthrough                    # passthrough | none (default) | api_key: ...  | {mode: key, key: ..., header: ...}
    match: ["claude-*"]                  # bare names routed here
    models:                              # ollama | openai | anthropic | none | a list | {source, static, include, exclude}
      - {id: default, bare: true, description: "Claude Code's own choice"}
      - {id: opus, bare: true}
    # default: true                      # catch-all for unknown bare names
    # headers: {x-tenant: team}          # always added upstream
    # compat:                            # only for upstreams that reject unknown fields
    #   drop_fields: [context_management]
    #   drop_tool_fields: [strict, defer_loading]
    #   drop_headers: [anthropic-beta]
    #   rename_fields: {max_tokens: max_completion_tokens}
    #   set_fields: {reasoning_effort: high}
  workstation:
    protocol: anthropic
    base_url: http://127.0.0.1:11435
    tunnel: workstation                       # opened before the gateway talks to it
    models: ollama                       # GET /api/tags
    launch:                              # environment for Claude Code when a model from here is chosen
      env:
        CLAUDE_CODE_ATTRIBUTION_HEADER: "0"
        CLAUDE_CODE_TOTAL_TOKENS_REMINDER: "off"
        ANTHROPIC_DEFAULT_HAIKU_MODEL: "{model}"      # {model} {upstream_model} {provider} {details.<key>}
        CLAUDE_CODE_SUBAGENT_MODEL: "{model}"

claude:
  command: claude
  args: []                               # always passed to Claude Code
  env: {}                                # for every launch; placeholders work here too
  default_model: null                    # what Enter in the picker means; null = Claude Code decides
```

### Credentials

- `auth: passthrough` forwards the client's `Authorization` and `x-api-key` headers as
  received. Use it for Anthropic so a Claude subscription login keeps working; the
  `anthropic-beta` header it depends on is forwarded as well.
- `api_key: ...` strips the client's credentials and sends the provider's own key,
  as `x-api-key` for `anthropic`, `Authorization: Bearer` for `openai`.
- The default, `none`, strips the client's credentials. Your Anthropic token never
  reaches a server you did not mark `passthrough`.

### Remote machines

A tunnel is the zero-setup option: it needs only `ssh` access, and the hub manages it
as an SSH control-master session (`ai-hub status` sees it, `ai-hub down` closes it,
`--force` replaces a stale one). If both machines are already on a private network such
as Tailscale, you can skip the tunnel: make the remote server listen on that network
(`OLLAMA_HOST=<tailnet-ip>` for Ollama) and point `base_url` at it directly.

### OpenAI-only servers

`protocol: openai` translates Anthropic requests into chat completions and back,
including streaming, tool calls, images, and system prompts. Anthropic-only
features (cache markers, thinking configuration, context management) are dropped.
Token counting is not available, so Claude Code estimates context usage itself.

## What Claude Code sees

- `ai-hub-shell` sets `ANTHROPIC_BASE_URL` to the hub and passes `--model` when you
  chose one. A value in your `~/.claude/settings.json` `env` block would override it.
- Requests to Anthropic go through with headers, body, and query string unchanged, so
  prompt caching, betas, usage headers, and error wording all behave as without a proxy.
- Claude Code treats a name it does not recognise as a current Claude model and sends
  its full feature set. Ollama ignores what it does not support; an upstream that
  rejects unknown fields gets the `compat` block above, or set
  `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` in that provider's `launch.env`.
- Claude Code's own gateway model discovery (`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`)
  works against `/v1/models` but only lists ids containing `claude` or `anthropic`, and
  only when a credential variable is set. The `ai-hub-shell` picker has no such limits.
- Local models need a large context window. For Ollama that is set on the server
  (`OLLAMA_CONTEXT_LENGTH=65536 ollama serve` or the app's settings), not per request.

## Command reference

| Command | What it does |
| --- | --- |
| `ai-hub-shell [--model M] [--force] [--no-picker] [--config P] [claude args]` | ensure tunnels and gateway, pick a model, `exec` Claude Code |
| `ai-hub-shell --list` / `--init` | list routable models / write a starter config |
| `ai-hub init [--force]` | write the starter config |
| `ai-hub up [--force]` / `down` / `restart` | manage tunnels and the gateway |
| `ai-hub status [--json]` | gateway, tunnels, providers (probes directly when the gateway is down) |
| `ai-hub models [--json] [--offline]` | every routable model |
| `ai-hub logs [-n N] [-f]` | gateway log (`~/.local/state/ai-hub/gateway.log`) |
| `ai-hub gateway [--host H] [--port P]` | run the gateway in the foreground |
| `ai-hub config [--edit]` | config path and validity |

Gateway endpoints: `POST /v1/messages`, `POST /v1/messages/count_tokens`, `GET /v1/models`,
`HEAD /api/hello`, `GET /healthz`, `GET /hub/models?refresh=1`, `POST /hub/refresh`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `port 11436 is in use by ...` | something else owns the hub port; `ai-hub up --force` replaces an old ai-hub, otherwise change `hub.port` |
| `tunnel 'x': local port ... in use by ssh` | a tunnel from before; `ai-hub up --force` |
| `model 'foo' is not served by any provider` | `ai-hub models` shows the names; use `<provider>/<model>` |
| Claude Code shows a 401 on Claude models | the Anthropic provider must have `auth: passthrough`; check `/status` inside Claude Code shows the hub URL and your login |
| Local model answers are cut short or forgetful | raise the server's context length (see above) |
| `400 ... Extra inputs are not permitted` from a non-Anthropic upstream | add a `compat.drop_fields` entry naming the field |
| Nothing happens, then a timeout | `ai-hub logs -n 50` shows the upstream the request went to |

## Extending

A provider class implements a wire protocol, not a vendor: subclass
`hub.providers.base.Provider`, set `protocol = "myproto"`, implement `discover`,
`messages`, and `count_tokens`, and register it under the `ai_hub.providers`
entry-point group in your own package. `protocol: myproto` then works in the config.

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check hub tests && uv run ruff format hub tests
uv run ai-hub gateway --config config.example.yaml --port 11499   # foreground, verbose
```

Layout: `hub/config.py` (schema and loader), `hub/catalog.py` (discovery and routing),
`hub/providers/` (`anthropic` pass-through, `openai` translation, discovery sources),
`hub/gateway.py` (FastAPI app), `hub/tunnels.py` and `hub/daemon.py` (process
management), `hub/picker.py`, `hub/cli.py` (both entry points).
