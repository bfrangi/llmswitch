# llmswitch

One local endpoint in front of every LLM server you use, so Claude Code can run on
Anthropic's models, on an Ollama on this machine, on an Ollama on another machine,
or on any OpenAI-compatible server, and switch between them by model name.

```
                          ┌───────────────────────── llmswitch gateway (127.0.0.1:11436) ───┐
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
uv tool install .                 # from this directory; rerun with --reinstall to upgrade
llmswitch init                    # writes ~/.config/llmswitch/config.yaml
$EDITOR ~/.config/llmswitch/config.yaml
llmswitch                         # starts what is needed, shows the model picker, launches Claude Code
```

`llmswitch init` pre-fills a `local` provider if an Ollama answers on this machine, and
leaves commented examples for a tunnelled remote machine and an OpenAI-style server.

[config.example.yaml](config.example.yaml) is a complete real configuration (this repository's
author's: a local Ollama, a remote one through an SSH tunnel, and Anthropic), kept identical
to the live file on the author's machine.

## Daily use

```bash
llmswitch                                    # picker, then Claude Code
llmswitch --model workstation/qwen3-coder:30b     # skip the picker
llmswitch --resume                           # anything it does not know is passed to Claude Code
llmswitch --model local/qwen3:14b -p "summarise this repo"
llmswitch models                             # everything the gateway can route, grouped by provider
llmswitch status                             # gateway, tunnels, providers
llmswitch down                               # stop the gateway and the tunnels
```

Inside Claude Code, `/model` lists every routable model below the built-in Claude rows,
because the launcher hands Claude Code the lineup through its `--settings` flag; typing
`/model workstation/qwen3-coder:30b` works too. Pressing Enter in the launcher picker starts
Claude Code with its own default model, which goes to Anthropic through the gateway.
Switching models mid-session keeps the launch environment of the model you started
with (background-task pin, context window), so start a new session when that matters.

The gateway and the tunnels stay up after Claude Code exits and are reused by the
next launch, so several sessions can share them. `llmswitch restart` reloads the config.

### Model names

| You write                     | Routed to                                   | Rule          |
| ----------------------------- | ------------------------------------------- | ------------- |
| `workstation/qwen3-coder:30b`      | provider `workstation`, model `qwen3-coder:30b`  | prefix        |
| `claude-opus-5-5`, `sonnet`   | the provider whose `match` globs fit        | match         |
| `qwen3:14b`                   | the one provider that lists it              | discovered    |
| anything else                 | the provider with `default: true`, if any   | default       |

A bare name present on two providers is an error that names both candidates.
Aliases such as `opus` are expanded by Claude Code itself before they reach the gateway.

## Configuration

`~/.config/llmswitch/config.yaml`, or `$LLMSWITCH_CONFIG`, or `--config PATH`. Strings may use
`${VAR}` and `${VAR:-default}`. Unknown keys are rejected, so typos surface at once.

```yaml
gateway:
  host: 127.0.0.1            # bind address (default). Keep loopback: your Anthropic login passes through here.
  port: 11436
  # state_dir: ~/.local/state/llmswitch     # logs, pidfile, tunnel sockets
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
        CLAUDE_CODE_TOTAL_TOKENS_REMINDER: "off"
        CLAUDE_CODE_AUTO_MODE_SERVER: "0"
        CLAUDE_CODE_MAX_CONTEXT_TOKENS: "{details.context_length}"   # {model} {upstream_model} {provider} {details.<key>};
                                                                     # a placeholder with no value drops the variable

claude:
  command: claude
  args: []                               # always passed to Claude Code
  env: {}                                # for every launch; placeholders work here too
  default_model: null                    # what Enter in the picker means; null = Claude Code decides
  model_picker: true                     # add every routable model to Claude Code's /model picker
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

A tunnel is the zero-setup option: it needs only `ssh` access, and the gateway manages it
as an SSH control-master session (`llmswitch status` sees it, `llmswitch down` closes it,
`--force` replaces a stale one). If both machines are already on a private network such
as Tailscale, you can skip the tunnel: make the remote server listen on that network
(`OLLAMA_HOST=<tailnet-ip>` for Ollama) and point `base_url` at it directly.

### OpenAI-only servers

`protocol: openai` translates Anthropic requests into chat completions and back,
including streaming, tool calls, images, and system prompts. Anthropic-only
features (cache markers, thinking configuration, context management) are dropped.
Token counting is not available, so Claude Code estimates context usage itself.

## What Claude Code sees

- `llmswitch` sets `ANTHROPIC_BASE_URL` to the gateway and passes `--model` when you
  chose one. A value in your `~/.claude/settings.json` `env` block would override it.
- Requests to Anthropic go through with headers, body, and query string unchanged, so
  prompt caching, betas, usage headers, and error wording all behave as without a proxy.
- Claude Code treats a name it does not recognise as a current Claude model and sends
  its full feature set. Ollama ignores what it does not support; an upstream that
  rejects unknown fields gets the `compat` block above, or set
  `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` in that provider's `launch.env`.
- `/model` shows the gateway's models because `llmswitch` passes a `modelPicker` lineup
  with `--settings` at launch. Claude Code uses one lineup source at a time, so if you
  keep your own `modelPicker` in `~/.claude/settings.json`, set `claude.model_picker:
  false` or pass your own `--settings`. Claude Code's built-in gateway discovery
  (`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`) is not a substitute: it needs a
  credential variable rather than a login, and only keeps ids containing `claude`.
- Claude Code does not know names like `workstation/qwen3-coder:30b`, so it assumes a 200k
  context window and says so once at startup. The template's `launch.env` sets
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` from the model's reported window. Background tasks
  such as session titles already follow the main model through a gateway, so nothing
  else needs pinning. Avoid pinning the `ANTHROPIC_DEFAULT_*_MODEL` slots to a local
  model: Claude Code resolves its `/model` rows and its safety classifier through them,
  so Opus, Sonnet, and Haiku would silently become that model and its own row vanish.
- Auto mode's safety classifier must run on a Claude model, so it goes to Anthropic
  through the gateway even while you work on a local model. That makes auto mode
  depend on Anthropic access. The template sets `CLAUDE_CODE_AUTO_MODE_SERVER=0` so
  Claude Code does not first wait for a review verdict a local server never produces.
  To send nothing to Anthropic, launch with `--permission-mode acceptEdits`.
- Local models need a large context window: Claude Code's system prompt alone is
  tens of thousands of tokens. Ollama sizes the window from the server's memory
  (4k below 24 GiB) unless `OLLAMA_CONTEXT_LENGTH` is set on the server, for example
  `OLLAMA_CONTEXT_LENGTH=65536 ollama serve`. It cannot be set per request through the
  Anthropic API. `curl http://host:11434/api/ps` shows the window a loaded model got.

## Command reference

| Command | What it does |
| --- | --- |
| `llmswitch [--model M] [--force] [--no-picker] [--config P] [claude args]` | ensure tunnels and gateway, pick a model, `exec` Claude Code (the default command) |
| `llmswitch claude [...]` | the same, spelled out |
| `llmswitch init [--force]` | write the starter config |
| `llmswitch up [--force]` / `down` / `restart` | manage tunnels and the gateway |
| `llmswitch status [--json]` | gateway, tunnels, providers (probes directly when the gateway is down) |
| `llmswitch models [--json] [--offline]` | every routable model |
| `llmswitch logs [-n N] [-f]` | gateway log (`~/.local/state/llmswitch/gateway.log`) |
| `llmswitch gateway [--host H] [--port P]` | run the gateway in the foreground |
| `llmswitch config [--edit]` | config path and validity |

Gateway endpoints: `POST /v1/messages`, `POST /v1/messages/count_tokens`, `GET /v1/models`,
`HEAD /api/hello`, `GET /healthz`, `GET /llmswitch/models?refresh=1`, `POST /llmswitch/refresh`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `port 11436 is in use by ...` | something else owns the gateway port; `llmswitch up --force` replaces an old llmswitch, otherwise change `gateway.port` |
| `tunnel 'x': local port ... in use by ssh` | a tunnel from before; `llmswitch up --force` |
| `model 'foo' is not served by any provider` | `llmswitch models` shows the names; use `<provider>/<model>` |
| Claude Code shows a 401 on Claude models | the Anthropic provider must have `auth: passthrough`; check `/status` inside Claude Code shows the gateway URL and your login |
| Local model answers are cut short or forgetful | raise the server's context length (see above) |
| `400 ... Extra inputs are not permitted` from a non-Anthropic upstream | add a `compat.drop_fields` entry naming the field |
| Nothing happens, then a timeout | `llmswitch logs -n 50` shows the upstream the request went to |
| `... is temporarily unavailable, so auto mode cannot determine the safety of ...` | the classifier was routed to a local model and queued behind your turn; remove any `ANTHROPIC_DEFAULT_*_MODEL` pins from `launch.env` so it reaches Anthropic |
| `/model` highlights Sonnet while a local model answers, or the local model has no row | an `ANTHROPIC_DEFAULT_SONNET_MODEL` pin made the Sonnet row resolve to it; remove the pin |

## Extending

A provider class implements a wire protocol, not a vendor: subclass
`llmswitch.providers.base.Provider`, set `protocol = "myproto"`, implement `discover`,
`messages`, and `count_tokens`, and register it under the `llmswitch.providers`
entry-point group in your own package. `protocol: myproto` then works in the config.

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check llmswitch tests && uv run ruff format llmswitch tests
uv run llmswitch gateway --config config.example.yaml --port 11499   # foreground, verbose
```

Layout: `llmswitch/config.py` (schema and loader), `llmswitch/catalog.py` (discovery and routing),
`llmswitch/providers/` (`anthropic` pass-through, `openai` translation, discovery sources),
`llmswitch/gateway.py` (FastAPI app), `llmswitch/tunnels.py` and `llmswitch/daemon.py` (process
management), `llmswitch/picker.py`, `llmswitch/cli.py` (both entry points).
