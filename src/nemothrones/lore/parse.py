"""Wikitext -> structured page (infobox, lead, sections, links) via mwparserfromhell.

Deliberately tolerant: the infobox is the first top-level template whose name looks
like an infobox (Character, Infobox ..., House, Castle, Location ...), keys are
lower-cased, references and templates are dropped from prose, and quote templates
(verbatim book text) never survive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import mwparserfromhell

from .dates import parse_year_range

INFOBOX_RE = re.compile(r"^(character|infobox|person|house|castle|location|noble|region|settlement|city)", re.I)
SKIP_SECTION_RE = re.compile(
    r"^(quotes?\b.*|references|notes|behind the scenes|external links|gallery|media|images|see also|"
    r"family|family tree|footnotes|sources|appearances)$", re.I)
# Kin that cannot be anachronistic for people alive at the start date: parents precede
# you, and a child in the cast is by construction already born. Spouses and lovers can
# be in the future, so they come only from the (time-sliced) dossier.
KIN_KEYS = ("father", "mother", "issue", "children")
CHAR_KEYS = {"born", "died", "father", "mother", "spouse", "allegiance", "culture", "issue"}


@dataclass
class Section:
    title: str
    level: int
    path: tuple[str, ...]
    text: str


@dataclass
class ParsedPage:
    title: str
    infobox_name: str | None
    infobox: dict[str, str]
    infobox_links: dict[str, list[str]]
    lead: str
    sections: list[Section]
    links: list[str]
    categories: list[str] = field(default_factory=list)

    @property
    def is_character(self) -> bool:
        if self.infobox_name and re.match(r"(character|person)", self.infobox_name, re.I):
            return True
        return len(CHAR_KEYS & set(self.infobox)) >= 2

    @property
    def born(self) -> tuple[int | None, int | None] | None:
        return parse_year_range(self.infobox.get("born"))

    @property
    def died(self) -> tuple[int | None, int | None] | None:
        return parse_year_range(self.infobox.get("died"))

    def kin_links(self) -> list[str]:
        out: list[str] = []
        for k in KIN_KEYS:
            out += self.infobox_links.get(k, [])
        return list(dict.fromkeys(out))

    def allegiances(self) -> list[str]:
        return [t for t in self.infobox_links.get("allegiance", []) if t.lower().startswith("house ")] or \
            self.infobox_links.get("allegiance", [])

    @property
    def display_name(self) -> str:
        return re.sub(r"\s*\([^)]*\)\s*$", "", self.title).strip()


def _link_title(link) -> str | None:
    t = str(link.title).strip().split("#", 1)[0].strip().replace("_", " ")
    if not t or ":" in t:
        return None
    return t[:1].upper() + t[1:]


def clean(wikitext: str) -> str:
    """Plain text: refs, templates, files and tags removed; <br> becomes a newline."""
    s = re.sub(r"<ref[^>/]*/>", "", wikitext)
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    code = mwparserfromhell.parse(s)
    for node in list(code.filter_templates(recursive=False)):
        try:
            code.remove(node)
        except ValueError:
            pass
    for link in list(code.filter_wikilinks(recursive=False)):
        if str(link.title).strip().lower().startswith(("file:", "image:", "category:")):
            try:
                code.remove(link)
            except ValueError:
                pass
    text = code.strip_code(normalize=True, collapse=True)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_wikitext(title: str, wikitext: str, categories: list[str] | None = None) -> ParsedPage:
    code = mwparserfromhell.parse(wikitext or "")
    infobox_name, fields, flinks = None, {}, {}
    for tpl in code.filter_templates(recursive=False):
        name = str(tpl.name).strip()
        if INFOBOX_RE.search(name):
            infobox_name = name
            for p in tpl.params:
                key = str(p.name).strip().lower()
                raw = str(p.value)
                fields[key] = clean(raw)
                flinks[key] = [t for t in (_link_title(lk) for lk in p.value.filter_wikilinks()) if t]
            break
    sections: list[Section] = []
    lead = ""
    stack: list[tuple[int, str]] = []
    for sec in code.get_sections(flat=True, include_lead=True):
        headings = sec.filter_headings(recursive=False)
        if not headings:
            body = str(sec)
            if infobox_name:
                for tpl in mwparserfromhell.parse(body).filter_templates(recursive=False):
                    if str(tpl.name).strip() == infobox_name:
                        body = body.replace(str(tpl), "", 1)
                        break
            lead = clean(body)
            continue
        h = headings[0]
        level = int(h.level)
        htitle = clean(str(h.title))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, htitle))
        body = str(sec).replace(str(h), "", 1)
        sections.append(Section(htitle, level, tuple(t for _, t in stack), clean(body)))
    links = [t for t in (_link_title(lk) for lk in code.filter_wikilinks()) if t]
    cats = list(categories or [])
    cats += [str(lk.title).split(":", 1)[1].strip() for lk in code.filter_wikilinks()
             if str(lk.title).strip().lower().startswith("category:")]
    return ParsedPage(title, infobox_name, fields, flinks, lead, sections, list(dict.fromkeys(links)),
                      list(dict.fromkeys(cats)))


def skip_section(sec: Section) -> bool:
    return any(SKIP_SECTION_RE.match(t) for t in sec.path)
