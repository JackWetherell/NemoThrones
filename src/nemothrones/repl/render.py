"""Plain-text rendering of engine Blocks (ANSI colour optional; NO_COLOR honoured)."""

from __future__ import annotations

import shutil
import textwrap

STYLES = {
    "title": "\033[1;37m",
    "header": "\033[2;36m",
    "prose": "",
    "roll": "\033[1;33m",
    "options": "\033[33m",
    "system": "\033[2m",
    "notice": "\033[2;35m",
    "warn": "\033[1;31m",
    "error": "\033[31m",
    "chronicle": "\033[3m",
    "sheet": "",
}
RESET = "\033[0m"


def width(default: int) -> int:
    cols = shutil.get_terminal_size((default, 24)).columns
    return max(40, min(default, cols - 2))


def wrap(text: str, w: int) -> str:
    out = []
    for para in text.split("\n"):
        out.append(textwrap.fill(para, w) if para.strip() else "")
    return "\n".join(out)


def render(blocks, *, color: bool = True, w: int = 92) -> str:
    w = width(w)
    parts = []
    for b in blocks:
        text = b.text or ""
        if b.kind in ("sheet", "options"):
            body = "\n".join(textwrap.fill(line, w, subsequent_indent="     ") if line else "" for line in text.split("\n"))
        elif b.kind == "title":
            body = text.center(w).rstrip()
        else:
            body = wrap(text, w)
        if b.kind in ("prose", "chronicle", "title"):
            body = "\n" + body
        style = STYLES.get(b.kind, "") if color else ""
        parts.append(f"{style}{body}{RESET if style else ''}")
    return "\n".join(parts)
