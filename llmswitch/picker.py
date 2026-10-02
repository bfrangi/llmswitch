"""A small interactive model picker for the terminal. No dependencies."""

from __future__ import annotations

from typing import Any


class PickerQuit(Exception):
    pass


def render(
    models: list[dict[str, Any]], providers: dict[str, dict[str, Any]], order: list[str]
) -> tuple[list[str], list[dict[str, Any]]]:
    lines: list[str] = []
    rows: list[dict[str, Any]] = []
    width = max((len(m["launch_id"]) for m in models), default=20)
    width = min(max(width, 20), 56)
    for name in order:
        p = providers.get(name, {})
        state = "" if p.get("ok") else f"   unavailable: {p.get('error') or 'not probed'}"
        lines.append(f"\n  {name}   {p.get('base_url', '')}{state}")
        for m in [m for m in models if m["provider"] == name]:
            rows.append(m)
            lines.append(f"  {len(rows):>3}  {m['launch_id']:<{width}}  {m.get('description') or ''}")
    return lines, rows


def pick(
    models: list[dict[str, Any]], providers: dict[str, dict[str, Any]], order: list[str], *, default_label: str
) -> dict[str, Any] | None:
    """Return the chosen model entry, or None for the default. Raises PickerQuit on q."""
    lines, rows = render(models, providers, order)
    print("llmswitch models" + "\n".join(lines))
    print(f"\n  Enter = {default_label}; a number or a name to choose; q to quit.")
    print("  Inside Claude Code, /model <name> switches at any time.\n")
    while True:
        try:
            s = input("model> ").strip()
        except EOFError:
            return None
        if not s:
            return None
        if s.lower() in ("q", "quit", "exit"):
            raise PickerQuit
        if s.isdigit() and 1 <= int(s) <= len(rows):
            return rows[int(s) - 1]
        exact = [m for m in rows if s in (m["launch_id"], m["id"], m["upstream_id"])]
        if len(exact) == 1:
            return exact[0]
        partial = [m for m in rows if s.lower() in m["id"].lower()]
        if len(partial) == 1:
            return partial[0]
        if partial:
            print("  ambiguous: " + ", ".join(m["launch_id"] for m in partial[:8]))
        else:
            print("  no such model; pick a number from the list")
