"""The starter configuration written by ``llmswitch init``."""

from __future__ import annotations

HEADER = """\
# llmswitch configuration.  Reference: README.md in the llmswitch repository.
# After editing, run `llmswitch restart` so the gateway picks the changes up.

gateway:
  host: 127.0.0.1        # keep loopback: the gateway forwards your Anthropic login headers
  port: 11436

# SSH tunnels the gateway opens before talking to remote servers. Each one is a
# control-master session that `llmswitch status` can see and `llmswitch down` can close.
tunnels: {}
#  workstation:
#    ssh: workstation                  # anything `ssh` accepts: a ~/.ssh/config alias or user@host
#    forward: 11435:localhost:11434    # same syntax as `ssh -L`; a list of them is fine too

# Providers are the upstream servers. In Claude Code a model is addressed as
# <provider>/<model>, e.g. `--model local/qwen3:14b` or `/model workstation/qwen3-coder:30b`.
providers:
  anthropic:
    protocol: anthropic
    base_url: https://api.anthropic.com
    auth: passthrough                   # your Claude Code login (or API key) goes through untouched
    match: ["claude-*"]                 # bare names such as claude-opus-5-5 route here without a prefix
    models:                             # aliases Claude Code expands by itself
      - {id: default, bare: true, description: "whatever Claude Code would pick on its own"}
      - {id: opus, bare: true, description: "alias resolved by Claude Code"}
      - {id: sonnet, bare: true, description: "alias resolved by Claude Code"}
      - {id: haiku, bare: true, description: "alias resolved by Claude Code"}
"""

LOCAL_BLOCK = """
  local:
    protocol: anthropic                 # Ollama speaks the Anthropic Messages API natively
    base_url: {base_url}
    models: ollama                      # list models with Ollama's /api/tags
    launch:                             # applied when a model from this provider is chosen
      env:
        CLAUDE_CODE_TOTAL_TOKENS_REMINDER: "off"  # that reminder breaks Ollama's KV cache on every turn
        CLAUDE_CODE_AUTO_MODE_SERVER: "0"         # a local server cannot review actions; use Claude Code's own classifier
        CLAUDE_CODE_MAX_CONTEXT_TOKENS: "{{details.context_length}}"  # the model's window, so compaction is timed right
        # Leave the ANTHROPIC_DEFAULT_*_MODEL slots alone. Through a gateway, background tasks already follow
        # the main model; auto mode's classifier must resolve to a Claude model; and /model's built-in rows
        # (Opus, Sonnet, Haiku) would otherwise silently point at this model and swallow its own row.
"""

ANTIGRAVITY_BLOCK = """
  google:
    protocol: anthropic                 # Google models (via antigravity-claude-proxy)
    base_url: {base_url}
    models: anthropic                   # list models with /v1/models
    match: ["gemini-*"]                 # bare gemini-* names route here without a prefix
"""

FOOTER = """
#  workstation:                        # an Ollama on another machine, through the tunnel above
#    protocol: anthropic
#    base_url: http://127.0.0.1:11435   # the local end of the tunnel
#    tunnel: workstation
#    models: ollama
#    launch:
#      env:
#        CLAUDE_CODE_TOTAL_TOKENS_REMINDER: "off"
#        CLAUDE_CODE_AUTO_MODE_SERVER: "0"
#        CLAUDE_CODE_MAX_CONTEXT_TOKENS: "{details.context_length}"

#  some-openai-server:                  # anything that only speaks OpenAI chat completions
#    protocol: openai
#    base_url: https://host.example/v1
#    api_key: ${SOME_API_KEY}           # ${VAR} and ${VAR:-default} are expanded
#    models: openai                     # GET /v1/models

#  gemini-key:                          # Google's Gemini API with an API key, spoken natively
#    protocol: gemini
#    base_url: https://generativelanguage.googleapis.com/v1beta
#    api_key: ${GEMINI_API_KEY}         # sent as x-goog-api-key
#    models:
#      source: gemini                   # GET /models, keeping the models that can generate text
#      include: ["gemini-*"]

claude:
  command: claude
  default_model: null                   # null: Claude Code's own default when you just press Enter
  env: {}                               # extra environment for every launch; {model} placeholders work here too
  model_picker: true                    # list every routable model in Claude Code's /model picker (via --settings)
"""


def render_template(local_ollama: str | None, local_antigravity: str | None = None) -> str:
    local = (
        LOCAL_BLOCK.format(base_url=local_ollama)
        if local_ollama
        else "\n".join(
            ("#" + line if line.strip() else line)
            for line in LOCAL_BLOCK.format(base_url="http://127.0.0.1:11434").splitlines()
        )
        + "\n"
    )
    ag = (
        ANTIGRAVITY_BLOCK.format(base_url=local_antigravity)
        if local_antigravity
        else "\n".join(
            ("#" + line if line.strip() else line)
            for line in ANTIGRAVITY_BLOCK.format(base_url="http://127.0.0.1:8080").splitlines()
        )
        + "\n"
    )
    return HEADER + local + ag + FOOTER
