# Handover

Short orientation for whoever picks this up next. The README is the user-facing
reference; this file records why things are the way they are.

## What it is

A local gateway that speaks the Anthropic Messages API to Claude Code and routes each
request to an upstream by model name. Routing, hosts, ports, credentials, and model
lists all live in `~/.config/ai-hub/config.yaml`; the code names none of them.

## Decisions

- **Route, don't translate, whenever possible.** Ollama (0.14+), LM Studio, vLLM and
  Anthropic all speak `/v1/messages`, so the `anthropic` provider is a byte-for-byte
  pass-through with the model field rewritten. That preserves caching markers, beta
  headers, streaming pings, usage headers, and error wording, all of which Claude Code
  relies on (code.claude.com/docs/en/llm-gateway-protocol). The `openai` provider is a
  translation layer for servers that have nothing better.
- **Credentials are per provider.** `passthrough` forwards the client's auth headers,
  which is what makes a Claude subscription login work; everything else strips them.
- **Shared lifecycle.** The gateway is a detached daemon and each tunnel an SSH
  control-master session, both reused across launches and closed by `ai-hub down`.
  The previous script's `exec claude` skipped its own cleanup trap and leaked a gateway
  and eight tunnels; nothing here depends on an EXIT trap.
- **The picker lives in the launcher.** Claude Code's own gateway discovery filters
  model ids to ones containing `claude`/`anthropic` and needs a credential variable,
  so it cannot show Ollama models to a subscription user. The launcher picker and
  `/model <name>` cover that.
- **Per-provider `launch.env` with `{model}` placeholders** replaces hardcoding Claude
  Code variable names. The template seeds the ones Ollama's own `ollama launch claude`
  sets (attribution header off, token reminder off, background models pinned).

## Verified against

Ollama 0.32.1 (local) and 0.34.4 (remote): `/v1/messages` ignores unknown fields and
headers, relaxes unsupported thinking instead of erroring, has no `count_tokens`
(plain-text 404, which the hub turns into an API-shaped 404), and takes its context
window from `OLLAMA_CONTEXT_LENGTH` on the server, not from the request.

## Open ideas

- `ai-hub status` could read Ollama's `/api/ps` to show each loaded model's actual
  context window.
- An `ollama` protocol provider could expose `/api/chat` for non-Claude clients if
  the hub is ever meant to front tools other than Claude Code.
