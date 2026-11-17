"""Terminal styling for interactive commands (setup): sections, notes and prompts.

Plain text when output isn't a terminal, when NO_COLOR is set (https://no-color.org) or TERM is
"dumb", so logs, pipes and tests see no escape codes.
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap

INDENT = "  "


def _color() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def _sgr(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _color() else text


def bold(t: str) -> str:
    return _sgr("1", t)


def dim(t: str) -> str:
    return _sgr("2", t)


def accent(t: str) -> str:
    return _sgr("36", t)     # cyan


def good(t: str) -> str:
    return _sgr("32", t)     # green


def warn(t: str) -> str:
    return _sgr("33", t)     # yellow


def width() -> int:
    return max(40, min(shutil.get_terminal_size((80, 24)).columns, 80)) - len(INDENT)


def title(text: str, sub: str = "") -> None:
    print(f"\n{bold(text)}" + (f"  {dim(sub)}" if sub else ""))


def section(n: int, total: int, name: str) -> None:
    print(f"\n{accent(f'[{n}/{total}]')} {bold(name)}")


def note(text: str) -> None:
    """A short explanation under a section heading, wrapped and dimmed."""
    for line in textwrap.wrap(text, width(), break_on_hyphens=False):
        print(INDENT + dim(line))


def info(text: str) -> None:
    """A fact worked out from an answer (free space, what a choice means)."""
    print(INDENT + dim("→ ") + text)


def caution(text: str) -> None:
    print(INDENT + warn("! ") + text)


def done(text: str) -> None:
    print(f"{good('✓')} {text}")


def ask(prompt: str, default, cast=str):
    while True:
        raw = input(f"{INDENT}{accent('?')} {prompt} {dim(f'({default})')}: ").strip()
        if not raw:
            return default
        try:
            return cast(raw)
        except ValueError:
            caution("not understood, try again")


def yes(prompt: str, default: bool) -> bool:
    raw = input(f"{INDENT}{accent('?')} {prompt} {dim('(Y/n)' if default else '(y/N)')}: ").strip().lower()
    return default if not raw else raw.startswith("y")


def table(rows: list[tuple[str, str]], key_style=dim, value_style=str) -> None:
    w = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(f"{INDENT}{key_style(k.ljust(w))}  {value_style(v)}")
