"""Edit a finished email section by section, keeping every version.

    notes [{section: "hero", text: "shorter headline"}, ...]
        → one model call per section with its notes (small find/replace edits, or a
          rewrite of that section only); the rest of the email stays byte-for-byte
        → the checks run again (links, alt text, logo, contrast, inbox; facts if a product is set)
        → saved as a new version; earlier versions can be viewed or restored

Versions live in runs/<id>/versions/<n>.html with versions.json beside them. The
current email is always runs/<id>/email.html (and original/email.html, which the
export step rebuilds the downloads from).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Callable, Optional

import brand_graph as bg
import email_checks
import email_doc
import export
import pipeline
import product_graph as pg
from bootstrap import STORAGE_DIR
from versions import ensure_original, save_version

EDIT_SYSTEM = """You edit one section of a finished marketing email. The HTML uses nested tables, inline styles and
Outlook conditional comments; keep that structure so it still renders in Outlook, Gmail and Apple Mail.
Change only what the notes ask for. Keep the brand's colors, fonts and voice. Use only the facts and images given.
Answer with JSON only (the rewrite mode puts the HTML after the JSON, see below)."""


# ── context for a run ─────────────────────────────────────────────────────────


def run_context(run_id: str, allow_host: Callable[[str], None], log: Callable[[str], None], fact_check: bool) -> email_checks.Context:
    spec = json.loads((STORAGE_DIR / "jobs" / f"{run_id}.json").read_text())
    brand = brand_g = product_g = None
    brand = pipeline.BrandSpec(**spec["brand"])
    if spec.get("brand_id"):
        brand_g = bg.load(spec["brand_id"])
        if spec.get("product_id"):
            product_g = pg.load(spec["brand_id"], spec["product_id"])
    descriptions = {img["key"]: img.get("description", "") for img in spec.get("images", [])}
    if product_g:
        descriptions.update({n["file"]: n.get("description", "") for n in bg.nodes(product_g, "ProductImage")})
    toppers = export.run_dir(run_id) / "toppers.html"
    return email_checks.Context(
        brand_spec=brand, brief=spec.get("brief", ""), brand=brand_g, product=product_g, image_descriptions=descriptions,
        subject_lines=export.parse_subject_lines(toppers.read_text()) if toppers.exists() else [],
        allow_host=allow_host, log=log, fact_check=fact_check,
    )


# ── editing ───────────────────────────────────────────────────────────────────


def _facts_text(ctx: email_checks.Context) -> str:
    parts = [f"Brand: {ctx.brand_spec.name}. Voice: {ctx.brand_spec.voice or '(none)'}. "
             f"Colors: background {ctx.brand_spec.background}, text {ctx.brand_spec.text}, accent {ctx.brand_spec.accent}, "
             f"button label {ctx.brand_spec.accent_text}. Font: {ctx.brand_spec.font_stack}."]
    if ctx.product:
        v = pg.values(ctx.product)
        parts.append(f"Product: {v['name']} {v['price']}. Facts: {json.dumps(v['features'])}. Page: {v['url']}")
    parts.append(f"Original brief: {ctx.brief[:1500]}")
    return "\n".join(parts)


def _allowed_images(html: str, ctx: email_checks.Context) -> list[str]:
    srcs = re.findall(r'<img[^>]+src="([^"]+)"', html)
    origin = srcs[0].split("/files/")[0] if srcs and "/files/" in srcs[0] else ""
    if ctx.product and origin:
        srcs += [f"{origin}/files/{n['file']}" for n in bg.nodes(ctx.product, "ProductImage")]
    return list(dict.fromkeys(srcs))


def _valid(fragment: str, allowed_imgs: list[str]) -> Optional[str]:
    """Why a rewritten section is unsafe to use, or None."""
    if len(fragment.strip()) < 40:
        return "empty section"
    if fragment.count("<table") != fragment.count("</table>"):
        return "unbalanced tables"
    if fragment.count("<!--[if") != fragment.count("<![endif]-->"):
        return "broken Outlook conditional comments"
    for src in re.findall(r'<img[^>]+src="([^"]+)"', fragment):
        if src not in allowed_imgs:
            return f"uses an image that isn't in the email or the product graph: {src[:80]}"
    if re.search(r"<script|<iframe|javascript:", fragment, re.I):
        return "contains script"
    return None


async def _edit_section(section: dict, notes: list[str], ctx: email_checks.Context, allowed_imgs: list[str]) -> tuple[str, str]:
    prompt = f"""Notes from the reviewer for this section ("{section['id']}"):
{chr(10).join(f'- {n}' for n in notes)}

Context:
{_facts_text(ctx)}

Images you may use (exact URLs): {json.dumps(allowed_imgs)}

Current section HTML:
{section['inner']}

Respond in ONE of two ways:
1. Small changes (copy, colors, sizes, spacing, a link): {{"mode": "edits", "summary": "what you changed", "edits": [{{"find": "exact text copied from the section HTML, unique in it", "replace": "new text"}}]}}
2. Structural changes (reorder, add or remove blocks, swap image layout): {{"mode": "rewrite", "summary": "what you changed"}}
   then on the next line the complete new section HTML between <section_html> and </section_html>."""
    out_text = await _ask_raw(prompt)
    m = re.search(r"\{.*?\}(?=\s*(?:<section_html>|$))", out_text, re.S) or re.search(r"\{.*\}", out_text, re.S)
    meta = json.loads(m.group(0)) if m else {}
    summary = meta.get("summary") or "edited"
    if meta.get("mode") == "rewrite":
        hm = re.search(r"<section_html>(.*?)</section_html>", out_text, re.S)
        if not hm:
            raise ValueError("the rewrite came back without its HTML")
        new_inner = hm.group(1).strip()
    else:
        new_inner = section["inner"]
        for e in meta.get("edits") or []:
            find, replace = e.get("find", ""), e.get("replace", "")
            if not find or new_inner.count(find) != 1:
                raise ValueError(f"an edit didn't match the section exactly once: {find[:60]!r}")
            new_inner = new_inner.replace(find, replace)
    problem = _valid(new_inner, allowed_imgs)
    if problem:
        raise ValueError(f"the edited section was rejected ({problem})")
    return new_inner, summary


async def _ask_raw(prompt: str) -> str:
    import anthropic  # the CLI backends swap this for their own client

    msg = await anthropic.AsyncAnthropic().messages.create(
        model=bg.MODEL, max_tokens=12000, system=EDIT_SYSTEM, messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    )
    return "".join(getattr(b, "text", "") for b in msg.content)


async def apply_notes(run_id: str, notes: list[dict], allow_host: Callable[[str], None], log: Callable[[str], None] = print) -> dict:
    ensure_original(run_id)
    html = (export.run_dir(run_id) / "email.html").read_text()
    ctx = run_context(run_id, allow_host, log, fact_check=True)
    by_section: dict[str, list[str]] = defaultdict(list)
    for n in notes:
        if str(n.get("text", "")).strip():
            by_section[n.get("section") or ""].append(n["text"].strip())
    known = {s["id"] for s in email_doc.sections(html)}
    unknown = [s for s in by_section if s not in known]
    if unknown:
        raise LookupError(f"no section named {', '.join(unknown)} in this email")
    allowed = _allowed_images(html, ctx)
    summaries = []
    for sid, texts in by_section.items():
        log(f"Editing the {sid} section ({len(texts)} note{'s' if len(texts) > 1 else ''})")
        new_inner, summary = await _edit_section(email_doc.section(html, sid), texts, ctx, allowed)
        html = email_doc.replace_section(html, sid, new_inner)
        summaries.append(f"{sid}: {summary}")
    log("Re-checking the email")
    html, report = await email_checks.run_all(html, ctx)
    entry = save_version(run_id, html, "; ".join(summaries)[:300], report)
    return entry
