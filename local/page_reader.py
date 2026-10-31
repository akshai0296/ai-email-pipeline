"""What a shopper sees on a page that plain page text leaves out.

Option swatches (with their colors), dropdown choices, structured product data and image alt
text. Stores often name a product's colors only there: Google's Pixel pages list the colors on
the swatch buttons ("Color: Fog") and in photo captions ("Pixel 11 Pro in Fog"). Used when a
product graph is built (product_graph) and when a graph is modified (graph_modify).
"""

from __future__ import annotations

import json
import re

import brand_graph as bg

OPTION_LABEL = re.compile(r"\b(colou?rs?|size|finish|style|material|storage|capacity|flavou?r|scent|pattern)\b", re.I)


def _context(el) -> str:
    """The first text around an element: the card or section it sits in."""
    a = el
    for _ in range(9):
        a = a.parent
        if a is None:
            return ""
        text = " ".join(a.get_text(" ", strip=True).split())
        if len(text) > 15:
            return text[:140]
    return ""


def details(soup) -> list[str]:
    """What a shopper sees but get_text() misses: option swatches (with their colors), image alt text, product data."""
    lines: list[str] = []
    groups: dict = {}
    for el in soup.find_all(attrs={"aria-label": OPTION_LABEL}):
        label = " ".join(el["aria-label"].split())
        if len(label) > 60 or el.name in ("nav", "section", "form", "fieldset", "ul", "div") and el.find(attrs={"aria-label": OPTION_LABEL}):
            continue
        hexm = re.search(r'(?:fill|background(?:-color)?|color)\s*[=:]\s*"?\s*(#[0-9A-Fa-f]{6})\b', str(el.parent)[:3000])
        key = el.get("name") or id(el.parent.parent)
        groups.setdefault(key, (el, []))[1].append(f"{label} {hexm.group(1).upper()}" if hexm else label)
    unique = {}  # the same picker often repeats (sticky header, mobile copy); keep one of each
    for first, opts in groups.values():
        opts = list(dict.fromkeys(opts))
        unique.setdefault((_context(first), tuple(opts)), (first, opts))
    for i, ((context, _), (first, opts)) in enumerate(unique.items()):
        lines.append(f"Option buttons (group {i + 1} of {len(unique)}, in the part of the page that reads “{context}”): " + "; ".join(opts))
    for sel in soup.find_all("select"):
        name = " ".join(filter(None, [sel.get("name"), sel.get("id"), sel.get("aria-label")]))
        if OPTION_LABEL.search(name):
            opts = [o.get_text(" ", strip=True) for o in sel.find_all("option") if o.get_text(strip=True)]
            lines.append(f"Dropdown {name}: " + "; ".join(opts[:40]))
    for d in bg._json_ld(soup):
        if str(d.get("@type")) in ("Product", "ProductGroup", "['Product']"):
            variants = [v.get("name") or v.get("color") for v in (d.get("hasVariant") or []) if isinstance(v, dict)]
            facts = {k: d.get(k) for k in ("name", "color", "material", "size") if d.get(k)}
            if variants:
                facts["variants"] = variants[:40]
            if facts:
                lines.append("Product data: " + json.dumps(facts, ensure_ascii=False)[:1500])
    alts = [" ".join(img.get("alt", "").split()) for img in soup.find_all("img")]
    alts = [a for a in dict.fromkeys(alts) if len(a.split()) >= 4 and "logo" not in a.lower()]  # photo descriptions, not menu icons
    lines += [f"Image: {a}" for a in alts[:80]]
    return lines
