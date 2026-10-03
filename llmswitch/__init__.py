"""llmswitch: one local endpoint in front of every LLM server you use.

The gateway speaks the Anthropic Messages API to clients such as Claude Code and
routes each request, by model name, to the upstream that serves that model:
Anthropic itself, an Ollama on this machine, an Ollama behind an SSH tunnel, or
any OpenAI-compatible server. Which upstreams exist is entirely a matter of the
configuration file; nothing here names a host, a port, or a model.
"""

__version__ = "0.3.0"
