# AI Hub

A modular, config-driven model orchestration gateway. 

It allows you to unify disparate model providers (Local Ollama, Remote Ollama, Anthropic, OpenAI, etc.) into a single, seamless endpoint.

## Features

- **Unified Model Picker:** See all your models in one place.
- **Config-Driven:** Add or remove providers via a simple YAML file.
- **Modular Architecture:** Easily add new provider types with simple plugins.
- **Orchestration Mode:** Launch the entire ecosystem (Tunnel + Gateway + Claude) with a single command.

## Installation

### 1. Prerequisites

Ensure you have [`uv`](https://github.com/astral-sh/uv) installed.

To use the remote orchestration feature, you must have SSH access to your remote machine and have it configured in your `~/.ssh/config`.

### 2. Install the package

Run this command from the project directory:

```bash
uv tool install .
```

This makes the `ai-hub-shell` command available globally in your terminal.

## Configuration

The tool looks for configuration in two places:
1.  The current working directory: `./config.yaml`
2.  Your user config directory: `~/.config/ai-hub/config.yaml`

### Automatic Setup

You can automatically generate a default configuration file in `~/.config/ai-hub/config.yaml` by running:

```bash
ai-hub-shell --init
```

### Manual Setup

If you prefer to create it manually, use the following structure:

```yaml
HUB_PORT: 11436
LOCAL_OLLAMA_PORT: 11435
REMOTE_OLLAMA_PORT: 11434
REMOTE_HOST: "your-remote-host-name"
HUB_LOG: "$HOME/Documents/ai-hub/hub.log"
```

## Usage

### Orchestration Mode (Recommended)

Launch the entire ecosystem (Tunnel, Gateway, and Claude) with one command. This mode automatically reads your configuration and handles the lifecycle of the background processes.

```bash
ai-hub-shell [any-claude-flags]
```

**Examples:**

*   **Default startup:** `ai-hub-shell`
*   **With a specific config file:** `ai-hub-shell ./my-project-config.yaml`
*   **Forcing a stale tunnel to close:** `ai-hub-shell --force`
*   **Resuming a session:** `ai-hub-shell --resume`

When you exit Claude Code, the tunnel and gateway will automatically shut down.

### Standard Gateway Mode

If you only want to run the gateway (e.g., to use it with other tools), run it using `uv`:

```bash
# Using default port 11436
uv run python -m hub.proxy

# Using a custom port from config
HUB_PORT=1234 uv run python -m hub.proxy
```

## Uninstallation

### If installed via `uv`:
```bash
uv tool uninstall ai-hub
```
