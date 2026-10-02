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

Ensure you have `uv` or `pip` installed. 

To use the remote orchestration feature, you must have SSH access to your remote machine (`workstation`) and have it configured in your `~/.ssh/config` as follows:

```text
Host workstation
    HostName <tailnet-ip>
    User bernat
    IdentityFile ~/.ssh/id_ed25519
    # This ensures we only use this host via the established identity
```

### 2. Install the package

```bash
# Using uv (Recommended)
uv tool install .

# Using pip
pip install .
```

### 3. Setup the Orchestrator

Make the script executable:

```bash
chmod +x scripts/ai-hub-shell
```

(Optional) Move it to your path:
```bash
sudo cp scripts/ai-hub-shell /usr/local/bin/ai-hub-shell
```

## Usage

### Standard Gateway Mode
Run the gateway normally (it defaults to port `11436` to avoid conflicts):
```bash
ai-hub
```
Then, in another terminal, run Claude Code pointing to the gateway:
```bash
export OLLAMA_HOST=http://localhost:11436
claude
```

### Orchestration Mode (Recommended)
Launch the entire ecosystem (Tunnel, Gateway, and Claude) with one command. No environment variables required:
```bash
ai-hub-shell [any-claude-flags]
```
Example:
```bash
ai-hub-shell --resume
```
When you exit Claude Code, the tunnel and gateway will automatically shut down.

## Uninstallation

### If installed via `uv`:
```bash
uv tool uninstall ai-hub
```

### If installed via `pip`:
```bash
pip uninstall ai-hub
```
