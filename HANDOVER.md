# Handover: AI-Hub Project

## Overview
The project `ai-hub` aims to provide a unified interface for accessing multiple LLM providers (Local Ollama, Remote Ollama via SSH/Tailscale, and Anthropic Claude) through a single command-line tool. The goal is to allow a user on one machine (e.g., a `pop-os` laptop) to seamlessly use models hosted on another machine (e.g., a `workstation` workstation) alongside their local models and Claude models, all within the Claude Code CLI.

## Key Components
- **`ai-hub` (Gateway):** A Python-based gateway that proxies requests to various model providers. It's intended to be configuration-driven.
- **`ai-hub-shell` (CLI Wrapper):** A shell script that automates the setup:
    - Establishing SSH/Tailscale tunnels to remote machines.
    - Starting the `ai-hub` gateway.
    - Launching `claude code` with the necessary environment configurations to see the proxied models.

## Current Objectives & Progress
- [x] Basic implementation of `ai-hub` gateway.
- [x] Shell script for automating tunnel and gateway startup.
- [x] Transitioned to `uv` for dependency management and `ruff` for linting/formatting.
- [ ] **Critical Blocker:** Claude Code is not detecting the models provided by the `ai-hub` gateway; it only shows the default Anthropic models.
- [ ] **Refinement:** Make `ai-hub-shell` truly generic (remove "workstation" hardcoding) and configuration-driven.
- [ ] **Refinement:** Improve the shell script's handling of port conflicts (e.g., asking for confirmation before killing a process).
- [ ] **Refinement:** Implement an "auto-approve" flag for port cleanup in the shell script.

## Technical Details
- **Repository Location:** `~/ai-hub`
- **Target Environment:** `pop-os` (development) and `workstation` (remote models).
- **Dependency Management:** `uv`
- **Formatting/Linting:** `ruff`
- **Configuration:** Intended to be stored in `~/.config/ai-hub/` or similar.

## Immediate Next Steps
1. **Investigate Model Discovery:** Determine why Claude Code is not picking up the models from the `ai-hub` gateway. Check if there are specific environment variables or proxy settings required by Claude Code to see non-Anthropic models via a local proxy.
2. **Verify Gateway Output:** Ensure the `ai-hub` gateway is correctly exposing the model list in a format that Claude Code can consume.
3. **Generalize `ai-hub-shell`:** Replace hardcoded strings with configurable variables.
4. **Improve Error Handling:** Fix the port conflict detection and add the requested confirmation/auto-approve logic.
