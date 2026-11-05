# Design notes

The README says how to use llmswitch. This file records why it is built the way it is.

## What it is

A local gateway that speaks the Anthropic Messages API to Claude Code and routes each
request to an upstream by model name. Routing, hosts, ports, credentials, and model
lists all live in the configuration file; the code names none of them.

## Decisions

- **Route, don't translate, whenever possible.** Ollama (0.14+), LM Studio, vLLM and
  Anthropic all speak `/v1/messages`, so the `anthropic` provider is a byte-for-byte
  pass-through with the model field rewritten. That preserves caching markers, beta
  headers, streaming pings, usage headers, and error wording, all of which Claude Code
  relies on (see Claude Code's gateway compatibility guide). The `openai` provider is a
  translation layer for servers that have nothing better.
- **Translate to a vendor's own API when the OpenAI shape loses something that matters.**
  Gemini's OpenAI-compatible endpoint has no place for thought signatures, and Gemini
  refuses the second step of any tool call that comes back without one, so the `gemini`
  provider speaks `generateContent` directly. That also gives thinking levels and exact
  token counting.
- **No state in the gateway, even for Gemini's signatures.** A thought signature reaches
  the client as the `signature` of a thinking block placed just before the part it belongs
  to, and Anthropic clients send thinking blocks back unchanged. Signatures carry a
  `llmswitch.gemini:` tag so ones from other vendors (a Claude model earlier in the same
  conversation) are never forwarded to Google, and a model turn whose first function call
  still lacks one gets Google's documented placeholder. Anthropic messages never end in
  thinking, so a signature Gemini sends after the answer is already out is dropped; Gemini
  only enforces them on function calls, which carry their own.
- **One block model for every translator.** A translating provider describes its answer as
  content blocks opened, extended, and closed. `MessageStream` renders that as server-sent
  events and `MessageBuilder` as a finished message, so streamed and non-streamed answers
  cannot drift apart. Translated requests also drop the client's content negotiation
  headers (`accept`, `accept-encoding`): the gateway decodes those responses itself and
  can only ask for encodings it understands.
- **Credentials are per provider.** `passthrough` forwards the client's auth headers,
  which is what makes a Claude subscription login work; everything else strips them, so
  a login token never reaches a server that was not marked for it.
- **Shared lifecycle.** The gateway is a detached daemon and each tunnel an SSH
  control-master session, both reused across launches and closed by `llmswitch down`.
  A launcher that `exec`s the agent cannot rely on its own exit handlers for cleanup,
  so nothing here depends on one.
- **The picker lives in the launcher, and in Claude Code's `/model`.** Claude Code's
  built-in gateway discovery filters model ids to ones containing `claude` or
  `anthropic` and needs a credential variable rather than a login, so it cannot list
  Ollama models for a subscription user. The launcher shows its own picker and passes a
  `modelPicker` lineup to Claude Code through `--settings`.
- **Per-provider `launch.env` with `{model}` placeholders** instead of variable names
  baked into code. The template seeds only what helps on a gateway: the token reminder
  off (it defeats Ollama's prompt cache), own-classifier mode, and the context window.
  It pins none of the `ANTHROPIC_DEFAULT_*_MODEL` slots: Claude Code resolves `/model`
  rows and the auto-mode safety classifier through them, so a pin hides the local
  model's own row, turns the Claude rows into the local model, and sends safety checks
  to a model that cannot answer them.

## Verified against

Ollama 0.32 and 0.34: `/v1/messages` ignores unknown fields and headers, relaxes
unsupported thinking instead of erroring, has no `count_tokens` (a plain-text 404,
which the gateway turns into an API-shaped 404 so Claude Code falls back to its own
estimate), and takes its context window from `OLLAMA_CONTEXT_LENGTH` on the server,
not from the request. Claude Code 2.1.286 for the launcher behaviour.

Gemini API `v1beta` (November 2026) with gemini-3.8-flash and gemini-4-argon, through Claude
Code 2.1.318: multi-step and parallel tool calls streamed and not, thinking levels and
summaries, `countTokens`, and implicit prompt caching reported as cache reads. Gemini rejects
a function call without a signature or with one it did not issue, accepts a signature from
another Gemini model, needs a `mimeType` to fetch a URL, rejects any thinking configuration
for models whose listing says they do not think, and accepts `maxOutputTokens` above a
model's limit.

## Roadmap

- **Named launch profiles.** The launcher already treats the agent as data: it brings
  the gateway up, picks a model, and execs `claude.command` with `ANTHROPIC_BASE_URL`
  and the per-provider `launch.env`. Only two things are fixed in code: the `--model`
  flag name and the `ANTHROPIC_BASE_URL` variable. Turning the single `claude:` section
  into a list of launch targets, each with its own command, model flag, base-URL
  variable, and env, would allow `llmswitch opencode` or `llmswitch cline` next to
  `llmswitch claude`, all sharing one gateway and one model catalog. Any agent that
  speaks the Anthropic Messages API to a configurable base URL qualifies.
- **`llmswitch status --loaded`**: read each Ollama provider's `/api/ps` to show which
  models are resident where, with the context window they were loaded with.
- **An `ollama` protocol provider** exposing `/api/chat` for non-Claude clients, if the
  gateway is ever meant to front tools other than Claude Code.
