from __future__ import annotations

import pytest

from llmswitch import picker
from llmswitch.picker import Choice, Header, Picker, PickerQuit, match_positions


def model(provider: str, upstream: str, launch: str | None = None, **extra: str) -> dict:
    return {
        "id": f"{provider}/{upstream}",
        "launch_id": launch or f"{provider}/{upstream}",
        "provider": provider,
        "upstream_id": upstream,
        **extra,
    }


MODELS = [
    model("google", "gemini-4-carbon-high", launch="gemini-4-carbon-high", display_name="Gemini 4 Carbon High"),
    model("google", "gemini-4-neon-low", launch="gemini-4-neon-low"),
    model("google", "claude-opus-5-5-high"),
    model("gemini-key", "gemini-4-carbon", description="ctx 1024k · thinking"),
    model("local", "qwen3:14b", description="14.8B · qwen3 · tools"),
]
PROVIDERS = {
    "google": {"ok": True, "base_url": "http://127.0.0.1:8080"},
    "gemini-key": {"ok": True, "base_url": "https://generativelanguage.googleapis.com/v1beta"},
    "local": {"ok": True, "base_url": "http://127.0.0.1:11434"},
    "offline": {"ok": False, "error": "ConnectError"},
}
ORDER = ["google", "gemini-key", "local", "offline"]


def make() -> Picker:
    return Picker(MODELS, PROVIDERS, ORDER, default_label="Claude Code's own default")


def typed(p: Picker, text: str) -> Picker:
    for ch in text:
        p.handle(ch)
    return p


def names(p: Picker) -> list[str]:
    return [m.launch or "default" for m in p.matches]


def test_everything_is_listed_by_provider_with_the_default_first() -> None:
    p = make()
    assert names(p)[0] == "default" and p.total == 5
    headers = [line.provider for line in p.lines if isinstance(line, Header)]
    assert headers == ORDER, "an unavailable provider still shows, to say why it is empty"
    assert next(line for line in p.lines if isinstance(line, Header) and line.provider == "offline").note == (
        "unavailable: ConnectError"
    )


def test_enter_straight_away_keeps_the_default() -> None:
    chosen = make().handle("enter")
    assert isinstance(chosen, Choice) and chosen.entry is None


def test_every_word_must_match_and_the_list_regroups() -> None:
    p = typed(make(), "carbon")
    assert names(p) == ["gemini-4-carbon-high", "gemini-key/gemini-4-carbon"]
    typed(p, " key")
    assert names(p) == ["gemini-key/gemini-4-carbon"]
    assert [line.provider for line in p.lines if isinstance(line, Header)] == ["gemini-key"]


def test_descriptions_and_initials_match() -> None:
    assert names(typed(make(), "thinking")) == ["gemini-key/gemini-4-carbon"]
    assert names(typed(make(), "g4n")) == ["gemini-4-neon-low"], "a subsequence of the id"
    assert names(typed(make(), "zzz")) == []
    assert make().handle("enter") is not None and typed(make(), "zzz").handle("enter") is None


def test_moving_choosing_and_editing() -> None:
    p = make()
    p.handle("down")
    assert p.handle("enter").launch == "gemini-4-carbon-high"
    p.handle("end")
    assert p.selected.launch == "local/qwen3:14b"
    p.handle("down")
    assert p.selected.launch == "local/qwen3:14b", "the cursor stops at the ends"
    p.handle("home")
    p.handle("up")
    assert p.selected.entry is None
    typed(p, "neon low")
    p.handle("ctrl-w")
    assert p.query == "neon "
    p.handle("backspace")
    p.handle("backspace")
    assert p.query == "neo"
    p.handle("ctrl-u")
    assert p.query == "" and len(p.matches) == 6


def test_esc_clears_the_search_then_leaves() -> None:
    p = typed(make(), "neon")
    p.handle("esc")
    assert p.query == ""
    with pytest.raises(PickerQuit):
        p.handle("esc")


def test_scrolling_keeps_the_cursor_and_its_header_in_view() -> None:
    p = make()
    p.handle("end")
    top = p.scroll(3)
    assert p.selected in p.lines[top : top + 3]
    p.handle("home")
    p.handle("down")  # the first google model: its header comes back into view with it
    top = p.scroll(3)
    assert p.lines[top].provider == "google" or p.lines[top] is p.default


def test_match_highlights() -> None:
    assert match_positions("gemini-4-carbon", ["carbon"]) == set(range(9, 15))
    assert match_positions("gemini-4-neon-low", ["g4n"]) == {0, 7, 9}
    assert match_positions("qwen3", ["nothing"]) == set()


def test_without_a_terminal_the_numbered_list_is_used(monkeypatch) -> None:
    monkeypatch.setattr(picker, "interactive_terminal", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")
    chosen = picker.pick(MODELS, PROVIDERS, ORDER, default_label="default")
    assert chosen["launch_id"] == "gemini-4-neon-low"
