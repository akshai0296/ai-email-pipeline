"""Small helpers for working on a finished email's HTML.

The renderer marks every content section with comments:

    <!-- Section:Start hero --> ...table markup... <!-- Section:End hero -->

so a section can be read, replaced or appended without touching the rest of the
document (Outlook conditional comments, head styles, the logo header). New blocks
are written as MJML and compiled with the same converter the pipeline uses, so
they come out as the same email-safe markup.
"""

from __future__ import annotations

import html as htmllib
import re
from typing import Optional

from bs4 import BeautifulSoup

SECTION_RE = re.compile(r"<!-- Section:Start (?P<id>[^>]*?) -->(?P<body>.*?)<!-- Section:End (?P=id) -->", re.S)


def sections(html: str) -> list[dict]:
    out = []
    for m in SECTION_RE.finditer(html):
        out.append({"id": m.group("id"), "start": m.start(), "end": m.end(), "inner": m.group("body"),
                    "text": visible_text(m.group("body"))})
    return out


def section(html: str, section_id: str) -> Optional[dict]:
    return next((s for s in sections(html) if s["id"] == section_id), None)


def replace_section(html: str, section_id: str, new_inner: str) -> str:
    s = section(html, section_id)
    if not s:
        raise KeyError(f"no section {section_id!r}")
    block = f"<!-- Section:Start {section_id} -->{new_inner}<!-- Section:End {section_id} -->"
    return html[: s["start"]] + block + html[s["end"]:]


def insert_after_sections(html: str, section_id: str, inner: str) -> str:
    """Add a new marked section after the last one (before the empty references block)."""
    ends = [m.end() for m in re.finditer(r"<!-- Section:End [^>]*-->", html)]
    at = ends[-1] if ends else _body_content_end(html)
    block = f"\n<!-- Section:Start {section_id} -->{inner}<!-- Section:End {section_id} -->\n"
    return html[:at] + block + html[at:]


def insert_before_section(html: str, before_id: str, section_id: str, inner: str) -> str:
    s = section(html, before_id)
    if not s:
        return insert_after_sections(html, section_id, inner)
    block = f"<!-- Section:Start {section_id} -->{inner}<!-- Section:End {section_id} -->\n"
    return html[: s["start"]] + block + html[s["start"]:]


def insert_after_section(html: str, after_id: str, section_id: str, inner: str) -> str:
    s = section(html, after_id)
    if not s:
        return insert_before_sections(html, section_id, inner)
    block = f"\n<!-- Section:Start {section_id} -->{inner}<!-- Section:End {section_id} -->"
    return html[: s["end"]] + block + html[s["end"]:]


def insert_before_sections(html: str, section_id: str, inner: str) -> str:
    """Add a new marked section before the first one (e.g. a logo header the designer left out)."""
    first = re.search(r"<!-- Section:Start [^>]*-->", html)
    if not first:
        body = re.search(r"<body[^>]*>", html)
        at = body.end() if body else 0
    else:
        at = first.start()
    block = f"<!-- Section:Start {section_id} -->{inner}<!-- Section:End {section_id} -->\n"
    return html[:at] + block + html[at:]


def _body_content_end(html: str) -> int:
    i = html.rfind("</body>")
    return i if i >= 0 else len(html)


def visible_text(fragment: str) -> str:
    soup = BeautifulSoup(fragment, "html.parser")
    for t in soup(["style", "script", "head", "title"]):
        t.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def links(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    return [{"href": a.get("href", ""), "text": " ".join(a.get_text(" ", strip=True).split())} for a in soup.find_all("a")]


def replace_href(html: str, old: str, new: str) -> str:
    for q in ('"', "'"):
        html = html.replace(f"href={q}{old}{q}", f"href={q}{new}{q}")
        html = html.replace(f"href={q}{htmllib.escape(old)}{q}", f"href={q}{htmllib.escape(new)}{q}")
    return html


def replace_text(html: str, old: str, new: str) -> tuple[str, int]:
    """Replace copy in the document's text (tries the raw and HTML-escaped forms). Returns (html, count)."""
    variants = [(old, new), (htmllib.escape(old, quote=False), htmllib.escape(new, quote=False)),
                (old.replace("'", "’"), new.replace("'", "’")), (old.replace("'", "&#39;"), new.replace("'", "&#39;"))]
    for o, n in variants:
        count = html.count(o)
        if count and o.strip():
            return html.replace(o, n), count
    return html, 0


async def compile_mjml_sections(mjml_sections: str, font_stack: str = "Helvetica, Arial, sans-serif") -> str:
    """Compile <mj-section>… blocks with the pipeline's converter and return the body markup."""
    from src.content_generation_new.application.Agentic_Workflows import message_generator_from_scratch_v0_1 as m

    doc = (
        "<mjml><mj-head><mj-attributes>"
        f'<mj-all font-family="{htmllib.escape(font_stack)}" />'
        "</mj-attributes></mj-head>"
        f'<mj-body width="600px">{mjml_sections}</mj-body></mjml>'
    )
    ok, out, err = await m._try_mjml_conversion(doc)
    if not ok or not out:
        raise RuntimeError(f"MJML compile failed: {err}")
    body_open = re.search(r"<body[^>]*>", out)
    if not body_open:
        raise RuntimeError("compiled MJML has no body")
    body = out[body_open.end(): out.rfind("</body>")]
    wrapper = re.match(r"\s*<div[^>]*>", body)  # mj-body's own wrapper div
    if wrapper:
        body = body[wrapper.end(): body.rfind("</div>")]
    return body
