"""The interactive model picker: type to filter, arrows to move, Enter to launch.

``Picker`` is a pure state machine (query, matches, cursor, scroll), drawn with the
standard library's curses, so the picker needs no dependencies and its behaviour can be
tested without a terminal. Without a capable terminal it falls back to a numbered list.
"""

from __future__ import annotations

import contextlib
import locale
import os
import sys
from dataclasses import dataclass
from typing import Any

try:
    import curses
except ImportError:  # a Python built without curses: the numbered list still works
    curses = None  # type: ignore[assignment]


PROMPT = "\u276f"  # heavy right-pointing angle, the search prompt


class PickerQuit(Exception):
    """The user left the picker without choosing."""


# ------------------------------------------------------------------------- model


@dataclass(frozen=True)
class Header:
    provider: str
    note: str  # the base URL, or why the provider is unavailable
    ok: bool
    total: int  # models the provider offers, before filtering


@dataclass(frozen=True)
class Choice:
    entry: dict[str, Any] | None  # None stands for Claude Code's (or the configured) default
    provider: str
    name: str  # shown first and highlighted: the model id within its provider
    detail: str  # display name and description, dimmed
    launch: str  # what Claude Code is started with ("" for the default)
    haystack: str  # lower-cased text every query word must occur in
    initials: str  # first character of each segment of "<provider>/<id>": "g4c" finds gemini-4-carbon


Line = Header | Choice


def _choice(m: dict[str, Any]) -> Choice:
    upstream = m["upstream_id"]
    display = m.get("display_name") or ""
    parts = [display] if display and display != upstream else []
    if m.get("description") and m["description"] not in (display, upstream):
        parts.append(m["description"])
    detail = " · ".join(parts)
    return Choice(
        entry=m,
        provider=m["provider"],
        name=upstream,
        detail=detail,
        launch=m["launch_id"],
        haystack=f"{m['provider']}/{upstream} {m['launch_id']} {detail}".lower(),
        initials="".join(
            f"{m['provider']}/{upstream}".lower()[i] for i in segment_starts(f"{m['provider']}/{upstream}")
        ),
    )


SEPARATORS = "-/.:_ "


def segment_starts(text: str) -> list[int]:
    """Where each segment of a model name starts (after - / . : _ or a space)."""
    return [i for i, ch in enumerate(text) if ch not in SEPARATORS and (i == 0 or text[i - 1] in SEPARATORS)]


def is_subsequence(needle: str, hay: str) -> bool:
    it = iter(hay)
    return all(ch in it for ch in needle)


def word_matches(word: str, choice: Choice) -> bool:
    return word in choice.haystack or (len(word) > 1 and is_subsequence(word, choice.initials))


def match_positions(text: str, words: list[str]) -> set[int]:
    """Characters of ``text`` to highlight: each word where it occurs, else the initials it matched."""
    low = text.lower()
    starts = segment_starts(text)
    out: set[int] = set()
    for word in words:
        at = low.find(word)
        if at >= 0:
            out.update(range(at, at + len(word)))
            continue
        hits: list[int] = []
        pending = iter(starts)
        for ch in word:
            hit = next((i for i in pending if low[i] == ch), None)
            if hit is None:
                hits = []
                break
            hits.append(hit)
        out.update(hits)
    return out


class Picker:
    """What the picker shows and where its cursor is; ``handle`` takes one key at a time.

    Keys are symbolic: ``up``, ``down``, ``pgup``, ``pgdn``, ``home``, ``end``, ``enter``,
    ``esc``, ``backspace``, ``ctrl-u``, ``ctrl-w``, or a single printable character.
    """

    def __init__(
        self,
        models: list[dict[str, Any]],
        providers: dict[str, dict[str, Any]],
        order: list[str],
        *,
        default_label: str,
    ) -> None:
        self.default = Choice(
            entry=None,
            provider="",
            name="default",
            detail=default_label,
            launch="",
            haystack=f"default {default_label}".lower(),
            initials="default",
        )
        names = order + sorted({m["provider"] for m in models} - set(order))
        self.groups: list[tuple[Header, list[Choice]]] = []
        for name in names:
            p = providers.get(name, {})
            choices = [_choice(m) for m in models if m["provider"] == name]
            note = p.get("base_url", "") if p.get("ok", True) else f"unavailable: {p.get('error') or 'not probed'}"
            self.groups.append((Header(name, note, bool(p.get("ok", True)), len(choices)), choices))
        self.total = sum(len(c) for _, c in self.groups)
        self.query = ""
        self.page = 10  # rows per page; the view sets it from the terminal height
        self._filter()

    # ------------------------------------------------------------------ state
    @property
    def words(self) -> list[str]:
        return self.query.lower().split()

    @property
    def selected(self) -> Choice | None:
        return self.matches[self.cursor] if self.matches else None

    def _filter(self) -> None:
        words = self.words
        keep = [] if words and not all(word_matches(w, self.default) for w in words) else [self.default]
        lines: list[Line] = list(keep)
        matches: list[Choice] = list(keep)
        for header, choices in self.groups:
            hits = [c for c in choices if all(word_matches(w, c) for w in words)]
            if hits or not words:  # with no query, empty and unavailable providers still say why
                lines.append(header)
                lines.extend(hits)
                matches.extend(hits)
        self.lines = lines
        self.matches = matches
        self.cursor = 0
        self.top = 0

    def visible_total(self, header: Header) -> int:
        return sum(1 for line in self.lines if isinstance(line, Choice) and line.provider == header.provider)

    # ------------------------------------------------------------------- keys
    def handle(self, key: str) -> Choice | None:
        """Apply one key; the chosen row on Enter. Raises PickerQuit to leave."""
        if key == "enter":
            return self.selected
        if key == "esc":
            if not self.query:
                raise PickerQuit
            self.query = ""
            self._filter()
        elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
            step = {"up": -1, "down": 1, "pgup": -self.page, "pgdn": self.page}.get(key, 0)
            last = max(len(self.matches) - 1, 0)
            target = 0 if key == "home" else last if key == "end" else self.cursor + step
            self.cursor = min(max(target, 0), last)
        elif key == "backspace":
            if self.query:
                self.query = self.query[:-1]
                self._filter()
        elif key == "ctrl-u":
            self.query = ""
            self._filter()
        elif key == "ctrl-w":
            self.query = self.query.rstrip()
            self.query = self.query[: self.query.rfind(" ") + 1] if " " in self.query else ""
            self._filter()
        elif len(key) == 1 and key.isprintable():
            self.query += key
            self._filter()
        return None

    def scroll(self, height: int) -> int:
        """The first line to show so the cursor (and its provider header) fit in ``height`` lines."""
        if self.selected is None or height <= 0:
            self.top = 0
            return 0
        at = self.lines.index(self.selected)
        if at < self.top:
            self.top = at - 1 if at > 0 and isinstance(self.lines[at - 1], Header) else at
        elif at >= self.top + height:
            self.top = at - height + 1
        self.top = max(0, min(self.top, max(len(self.lines) - height, 0)))
        return self.top


# -------------------------------------------------------------------- terminal


def _rest_of_sequence(scr: Any) -> str:
    """Whatever arrived right behind an Esc: the tail of a key sequence curses did not know."""
    tail = ""
    scr.nodelay(True)
    try:
        while True:
            try:
                ch = scr.get_wch()
            except curses.error:  # nothing more waiting
                break
            tail += ch if isinstance(ch, str) else "?"
    finally:
        scr.nodelay(False)
    return tail


def _read_key(scr: Any) -> str:
    ch = scr.get_wch()
    if ch == "\x1b" and _rest_of_sequence(scr):
        return ""  # an unknown key (F-keys, Alt+...) must not count as Esc, which can quit
    if isinstance(ch, int):
        return {
            curses.KEY_UP: "up",
            curses.KEY_DOWN: "down",
            curses.KEY_PPAGE: "pgup",
            curses.KEY_NPAGE: "pgdn",
            curses.KEY_HOME: "home",
            curses.KEY_END: "end",
            curses.KEY_BACKSPACE: "backspace",
            curses.KEY_ENTER: "enter",
            curses.KEY_RESIZE: "resize",
        }.get(ch, "")
    return {
        "\n": "enter",
        "\r": "enter",
        "\x1b": "esc",
        "\x7f": "backspace",
        "\x08": "backspace",
        "\x15": "ctrl-u",
        "\x17": "ctrl-w",
        "\x10": "up",  # ctrl-p
        "\x0e": "down",  # ctrl-n
        "\t": "down",
    }.get(ch, ch)


@dataclass
class Palette:
    accent: int
    error: int
    dim: int
    bold: int
    match: int


def _palette() -> Palette:
    accent = error = 0
    if curses.has_colors() and "NO_COLOR" not in os.environ:
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_CYAN, background)
        curses.init_pair(2, curses.COLOR_RED, background)
        accent, error = curses.color_pair(1), curses.color_pair(2)
    return Palette(
        accent=accent,
        error=error or curses.A_BOLD,
        dim=curses.A_DIM,
        bold=curses.A_BOLD,
        match=curses.A_BOLD | curses.A_UNDERLINE,
    )


class _Canvas:
    """Clipped writes: curses raises when text runs past the last column."""

    def __init__(self, scr: Any) -> None:
        self.scr = scr
        self.height, self.width = scr.getmaxyx()

    def put(self, y: int, x: int, text: str, attr: int = 0, *, ellipsis: bool = False) -> int:
        if y < 0 or y >= self.height or x >= self.width:
            return x
        room = max(self.width - x - (1 if y == self.height - 1 else 0), 0)
        if len(text) > room:
            text = text[: room - 1] + "…" if ellipsis and room > 1 else text[:room]
        with contextlib.suppress(curses.error):
            self.scr.addstr(y, x, text, attr)
        return x + len(text)


def _draw(scr: Any, p: Picker, pal: Palette) -> None:
    scr.erase()
    c = _Canvas(scr)
    h, w = c.height, c.width
    words = p.words

    # Title and counts.
    x = c.put(0, 1, "llmswitch", pal.bold | pal.accent)
    c.put(0, x, "  choose a model for Claude Code", pal.dim)
    count = f"{len([m for m in p.matches if m.entry is not None])}/{p.total} models"
    c.put(0, max(w - len(count) - 1, 0), count, pal.dim)

    # Search line; the terminal cursor sits at its end.
    c.put(1, 1, PROMPT, pal.accent | pal.bold)
    if p.query:
        c.put(1, 3, p.query, pal.bold)
    else:
        c.put(1, 3, "type to search: provider, model, or description", pal.dim)
    c.put(2, 0, "─" * w, pal.dim)

    list_top, list_height = 3, max(h - 5, 1)
    p.page = max(list_height - 1, 1)
    first = p.scroll(list_height)
    name_width = min(max((len(m.name) for m in p.matches), default=8), max(w // 2, 12), 48)

    if not p.matches:
        c.put(list_top + 1, 3, f"no model matches “{p.query}”  ·  backspace or esc to widen the search", pal.dim)
    for row, line in enumerate(p.lines[first : first + list_height]):
        y = list_top + row
        if isinstance(line, Header):
            x = c.put(y, 1, line.provider, pal.bold | pal.accent)
            tally = f"{line.total}" if not words else f"{p.visible_total(line)} of {line.total}"
            note_room = max(w - x - len(tally) - 5, 0)
            note = line.note if len(line.note) <= note_room else line.note[: max(note_room - 1, 0)] + "…"
            x = c.put(y, x + 2, note, pal.dim if line.ok else pal.error)
            c.put(y, max(w - len(tally) - 1, x + 2), tally, pal.dim)
            continue
        chosen = line is p.selected
        c.put(y, 1, "▌" if chosen else " ", pal.accent | pal.bold)
        base = (pal.bold | pal.accent) if chosen else 0
        hits = match_positions(line.name, words)
        x = 3
        for i, ch in enumerate(line.name[:name_width]):
            x = c.put(y, x, ch, base | (pal.match if i in hits else 0))
        if len(line.name) > name_width:
            c.put(y, x - 1, "…", base)
        c.put(y, 3 + name_width + 2, line.detail, pal.dim, ellipsis=True)

    # Footer: keys on the left, what Enter will run on the right.
    c.put(h - 2, 0, "─" * w, pal.dim)
    target = p.selected
    run = "" if target is None else f"claude --model {target.launch}" if target.launch else target.detail
    keys = next(
        (k for k in ("↑↓ move  ⏎ launch  esc clear / quit", "↑↓ ⏎ esc", "") if len(k) + len(run) + 5 <= w),
        "",
    )
    c.put(h - 1, 1, keys, pal.dim)
    if run:
        c.put(h - 1, max(w - len(run) - 2, len(keys) + 3 if keys else 1), run, pal.accent, ellipsis=True)
    try:
        curses.curs_set(1)
        scr.move(1, min(3 + len(p.query), w - 1))
    except curses.error:
        pass  # some terminals cannot show or place the cursor


def _run_curses(picker: Picker) -> Choice:
    os.environ.setdefault("ESCDELAY", "25")  # a lone Esc should not wait a second for a sequence
    locale.setlocale(locale.LC_ALL, "")

    def main(scr: Any) -> Choice:
        pal = _palette()
        scr.keypad(True)
        while True:
            _draw(scr, picker, pal)
            key = _read_key(scr)
            if key in ("", "resize"):
                continue
            chosen = picker.handle(key)
            if chosen is not None:
                return chosen

    return curses.wrapper(main)


def interactive_terminal() -> bool:
    return (
        curses is not None and sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb"
    )


# ------------------------------------------------------------------ the fallback


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


def pick_plain(
    models: list[dict[str, Any]], providers: dict[str, dict[str, Any]], order: list[str], *, default_label: str
) -> dict[str, Any] | None:
    """A numbered list read with ``input()``, for terminals curses cannot drive."""
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


def pick(
    models: list[dict[str, Any]], providers: dict[str, dict[str, Any]], order: list[str], *, default_label: str
) -> dict[str, Any] | None:
    """The chosen model entry, or None for the default. Raises PickerQuit when the user leaves."""
    if interactive_terminal():
        picker = Picker(models, providers, order, default_label=default_label)
        try:
            return _run_curses(picker).entry
        except KeyboardInterrupt:
            raise PickerQuit from None
        except curses.error:  # no usable terminfo for this terminal: the plain list still works
            pass
    return pick_plain(models, providers, order, default_label=default_label)
