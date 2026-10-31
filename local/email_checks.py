"""Checks that run on every finished email, fixing what they safely can.

    Layout       image-and-text rows are balanced: both columns top-aligned, side copy not far taller than its photo
    Facts        copy states only what the product graph and the brief say (one model call)
    Footer       sender, address, social links, website and unsubscribe are present
    Links        no placeholder or broken links; buttons go to real pages
    Alt text     every image has alt text
    Logo         readable on what is behind it (logo_check.py)
    Contrast     every text block is readable on its background
    Dark mode    the logo survives email apps that darken the background
    Inbox        size (Gmail clips over 102 KB), images, spam words, subject length

Each check reports one item: pass, fixed, warn or skipped, with a short detail.
A check that errors is reported as skipped and never costs the user the email.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from PIL import Image

import brand_graph as bg
import email_doc
import logo_check
import product_graph as pg
from bootstrap import STORAGE_DIR

UNSUBSCRIBE_TAG = "{{unsubscribe_link}}"
NETWORK_NAMES = {"x": "X", "tiktok": "TikTok", "youtube": "YouTube", "linkedin": "LinkedIn"}
MERGE_TAG = re.compile(r"^(\{\{.*\}\}|\*\|.*\|\*|%%.*%%|\{%.*%\}|\[.*\])$")
SPAM_PHRASES = [
    "100% free", "act now", "buy now!!!", "cash bonus", "click here", "congratulations", "dear friend", "double your",
    "earn money", "free gift", "guaranteed", "limited time only", "no cost", "no obligation", "risk-free", "winner",
    "you have been selected", "urgent", "$$$", "!!!",
]


@dataclass
class Context:
    brand_spec: Any  # pipeline.BrandSpec
    brief: str = ""
    brand: Optional[dict] = None  # brand graph
    product: Optional[dict] = None  # product graph
    image_descriptions: dict = field(default_factory=dict)  # storage key -> description
    subject_lines: list = field(default_factory=list)  # [{"subject", "preheader"}]
    allow_host: Callable[[str], None] = lambda h: None
    log: Callable[[str], None] = print
    fact_check: bool = True


def item(check: str, status: str, detail: str) -> dict:
    return {"check": check, "status": status, "detail": detail}


async def run_all(html: str, ctx: Context) -> tuple[str, list[dict]]:
    report: list[dict] = []
    steps = [
        ("Layout", balance_columns), ("Facts", check_facts), ("Footer", ensure_footer), ("Links", check_links), ("Alt text", fix_alt_text),
        ("Logo", check_logo), ("Contrast", fix_contrast), ("Dark mode", check_dark_mode), ("Email clients", check_clients),
        ("Inbox", check_inbox),
    ]
    for name, fn in steps:
        try:
            ctx.log(f"Checking: {name.lower()}")
            html, items = await fn(html, ctx)
            report.extend(items)
        except Exception as exc:  # one failed check never costs the email
            report.append(item(name, "skipped", f"check failed: {type(exc).__name__}: {str(exc)[:200]}"))
    return html, report


# ── layout ────────────────────────────────────────────────────────────────────

# Two-column rows with a photo on one side and copy on the other, measured at desktop width. Columns are
# numbered in document order so the same column can be found again in the HTML source.
_SPLIT_ROWS = """() => {
  const cols = [...document.querySelectorAll('div[class*="mj-column-per"], div[class*="mj-column-px"]')];
  const rows = new Map();
  cols.forEach((c, i) => { const p = c.parentElement; rows.set(p, [...(rows.get(p) || []), i]); });
  const kind = (c) => {
    const t = c.innerText.replace(/\\s+/g, ' ').trim(), imgs = c.querySelectorAll('img').length;
    return imgs && t.length < 30 ? 'image' : !imgs && t.length > 60 ? 'text' : null;
  };
  const out = [];
  for (const idx of rows.values()) {
    if (idx.length !== 2) continue;
    const [ka, kb] = idx.map((i) => kind(cols[i]));
    if (!ka || !kb || ka === kb) continue;
    const [ii, ti] = ka === 'image' ? idx : [idx[1], idx[0]];
    const img = cols[ii].getBoundingClientRect(), txt = cols[ti].getBoundingClientRect();
    if (img.right > txt.left + 1 && txt.right > img.left + 1) continue;  // stacked, not side by side
    // Body paragraphs: leaf blocks of ordinary-size text (eyebrows and headlines are left alone).
    const paras = [...cols[ti].querySelectorAll('div, p, td')]
      .filter((e) => ![...e.children].some((ch) => !getComputedStyle(ch).display.startsWith('inline')))
      .map((e) => ({ text: e.innerText.replace(/\\s+/g, ' ').trim(), size: parseFloat(getComputedStyle(e).fontSize), plain: !e.children.length }))
      .filter((p) => p.text.length > 40 && p.size <= 17);
    out.push({ img: ii, txt: ti, ih: Math.round(img.height), th: Math.round(txt.height), paras,
               valign: getComputedStyle(cols[ti]).verticalAlign, columns: cols.length,
               words: paras.reduce((n, p) => n + p.text.split(' ').length, 0) });
  }
  return out;
}"""

LAYOUT_SYSTEM = """You tighten marketing email copy so it fits its layout. Answer with JSON only."""


async def _evaluate(html: str, script: str, width: int = 700):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": width, "height": 1000})
            await page.set_content(html, wait_until="load")
            await page.wait_for_timeout(300)
            return await page.evaluate(script)
        finally:
            await browser.close()


def _align_top(html: str, indices: list[int]) -> str:
    """Top-align the given columns, in the browser markup and in Outlook's table cell just before each."""
    soup = BeautifulSoup(html, "html.parser")
    cols = soup.find_all("div", class_=re.compile(r"^mj-column-(per|px)-"))
    line_start = [0]
    for line in html.splitlines(keepends=True):
        line_start.append(line_start[-1] + len(line))
    starts = sorted((line_start[cols[i].sourceline - 1] + cols[i].sourcepos for i in indices if i < len(cols) and cols[i].sourceline), reverse=True)
    for start in starts:
        end = logo_check._tag_end(html, start)
        html = html[:start] + _set_style(html[start:end], "vertical-align", "top") + html[end:]
        cond = html.rfind("<!--[if mso", 0, start)
        td = html.rfind("<td", cond, start) if cond >= 0 and start - cond < 400 else -1
        if td >= 0:
            td_end = html.find(">", td) + 1
            html = html[:td] + re.sub(r"vertical-align:\s*\w+", "vertical-align:top", html[td:td_end]) + html[td_end:]
    return html


async def _shorten(html: str, row: dict, target: int) -> Optional[str]:
    """The row's body copy cut to about `target` words, using only what it already says; None if that can't be done cleanly."""
    paras = row["paras"]
    if not paras or not all(p["plain"] for p in paras):
        return None  # links or bold inside: leave the wording to the person
    prompt = f"""This copy sits next to a product photo in a two-column email row, and runs {row['th'] / max(row['ih'], 1):.1f}x taller than the photo,
which leaves a tall empty gap beside the photo. Shorten the body to about {target} words in total so the two columns look balanced.

Paragraphs, in order: {json.dumps([p['text'] for p in paras], ensure_ascii=False)}

Rules: write natural, complete sentences in the same tone, never a crammed list of specs; keep the one or two facts that matter
most to the headline and drop the rest rather than squeezing them in; use only facts already in these paragraphs and add nothing
new; keep numbers and product names exactly as written; you may merge everything into the first paragraph and return "" for the others.
Return {{"paragraphs": ["...", ...]}} with exactly {len(paras)} entries, in the same order."""
    out = await bg._ask_model(LAYOUT_SYSTEM, [{"type": "text", "text": prompt}], max_tokens=1500)
    new = out.get("paragraphs") if isinstance(out, dict) else None
    if not isinstance(new, list) or len(new) != len(paras) or not all(isinstance(t, str) for t in new) or not new[0].strip():
        return None
    if sum(len(t.split()) for t in new) > row["words"] * 0.85:
        return None  # barely shorter: not worth changing the copy
    for p, text in zip(paras, new):
        html, n = email_doc.replace_text(html, p["text"], text.strip())
        if not n:
            return None
    return html


async def balance_columns(html: str, ctx: Context) -> tuple[str, list[dict]]:
    """A photo beside much taller copy floats in the middle with empty space above and below it. Both columns
    are top-aligned (the photo lines up with the headline), and copy more than twice the photo's height is
    shortened to fit, keeping only what it already says."""
    rows = await _evaluate(html, _SPLIT_ROWS)
    if not rows:
        return html, [item("Layout", "pass", "no image-and-text rows to balance")]
    soup_cols = len(BeautifulSoup(html, "html.parser").find_all("div", class_=re.compile(r"^mj-column-(per|px)-")))
    if rows[0]["columns"] != soup_cols:
        return html, [item("Layout", "skipped", "couldn't match the rendered columns to the HTML")]
    items, align = [], []
    for r in rows:
        tall = r["th"] / max(r["ih"], 1)
        title = (r["paras"][0]["text"][:40] + "…") if r["paras"] else "image-and-text row"
        changes = []
        if tall > 2 and r["words"] > 40:
            target = max(25, round(r["words"] * 1.5 / tall))
            new_html = await _shorten(html, r, target)
            if new_html:
                html = new_html
                changes.append(f"copy shortened from {r['words']} to about {target} words")
            else:
                items.append(item("Layout", "warn", f"“{title}” is {tall:.1f}x taller than its photo; shorten it or give it a full-width row"))
        if r["valign"] != "top" and tall > 1.2:
            align += [r["img"], r["txt"]]
            changes.append("photo and copy top-aligned")
        if changes:
            items.append(item("Layout", "fixed", f"“{title}”: {', '.join(changes)} (copy was {tall:.1f}x the photo's height)"))
    if align:
        html = _align_top(html, align)
    if not items:
        return html, [item("Layout", "pass", f"all {len(rows)} image-and-text rows are balanced")]
    return html, items


# ── facts ─────────────────────────────────────────────────────────────────────

FACT_SYSTEM = """You check marketing email copy against the facts the brand supplied. Answer with JSON only."""


async def check_facts(html: str, ctx: Context) -> tuple[str, list[dict]]:
    if not ctx.fact_check:
        return html, [item("Facts", "skipped", "turned off for this run")]
    if not ctx.product:
        return html, [item("Facts", "skipped", "no product picked, so there is no fact sheet to check the copy against")]
    copy = " ".join(s["text"] for s in email_doc.sections(html)) or email_doc.visible_text(html)
    v = pg.values(ctx.product)
    facts = {
        "product": v["name"], "price": v["price"], "about": v["description"], "facts": v["features"], "variants": v["variants"],
        "brand": bg.company(ctx.brand).get("name") if ctx.brand else ctx.brand_spec.name,
        "tagline": bg.company(ctx.brand).get("tagline") if ctx.brand else "",
        "what the product photos show": [i["description"] for i in v["images"] if i.get("description")],
        "more product facts by topic": v["branches"],
        "company facts by topic": bg.branches(ctx.brand) if ctx.brand else {},
    }
    prompt = f"""Fact sheet (from the brand's own product page and its photos; anything visible in a photo is supported): {json.dumps(facts)}
Brief from the marketer (also a valid source): {ctx.brief[:3000]}

Email copy: {copy[:6000]}

The brief counts as a source: anything the brief states or clearly implies (e.g. "a new spring colorway" supports "new colors")
is supported, even if the fact sheet doesn't list it.

Find statements in the email copy that assert a specific, checkable fact supported by NEITHER the fact sheet NOR the brief:
prices, discounts, numbers, materials, specs, shipping or return terms, guarantees, dates, awards, rankings, comparisons.
Ignore opinions, mood and generic marketing language ("feels amazing", "made for adventure").

Return {{"issues": [{{"quote": "exact text copied from the email copy", "problem": "what is unsupported", "fix": "replacement text for the quote that keeps the sentence natural and uses only supported facts (or drops the claim)"}}]}}
Return {{"issues": []}} if everything checks out."""
    out = await bg._ask_model(FACT_SYSTEM, [{"type": "text", "text": prompt}], max_tokens=2000)
    issues = [i for i in out.get("issues") or [] if isinstance(i, dict) and i.get("quote")]
    if not issues:
        return html, [item("Facts", "pass", f"every specific claim matches the {v['name']} fact sheet or the brief")]
    items = []
    for i in issues[:10]:
        new_html, n = email_doc.replace_text(html, i["quote"], i.get("fix") or "")
        if n:
            html = new_html
            items.append(item("Facts", "fixed", f"“{i['quote'][:90]}” → “{(i.get('fix') or '')[:90]}” ({i.get('problem', '')[:80]})"))
        else:
            items.append(item("Facts", "warn", f"unsupported: “{i['quote'][:90]}” ({i.get('problem', '')[:80]}); couldn't edit it automatically"))
    return html, items


# ── footer ────────────────────────────────────────────────────────────────────


def _mix(a: str, b: str, t: float) -> str:
    pa = [int(a.lstrip("#")[i: i + 2], 16) for i in (0, 2, 4)]
    pb = [int(b.lstrip("#")[i: i + 2], 16) for i in (0, 2, 4)]
    return "#%02X%02X%02X" % tuple(round(x + (y - x) * t) for x, y in zip(pa, pb))


def _muted(text: str, background: str) -> str:
    for t in (0.35, 0.25, 0.15, 0.0):
        c = _mix(text, background, t)
        if bg._contrast(c, background) >= 4.5:
            return c
    return text


async def ensure_footer(html: str, ctx: Context) -> tuple[str, list[dict]]:
    text = email_doc.visible_text(html).lower()
    if "unsubscribe" in text:
        missing = []
        if ctx.brand and (bg.node(ctx.brand, "address") or {}).get("text") and (bg.node(ctx.brand, "address")["text"].split(",")[0].lower() not in text):
            missing.append("postal address")
        return html, [item("Footer", "pass" if not missing else "warn",
                           "footer present" + (f", but missing: {', '.join(missing)}" if missing else ""))]
    spec = ctx.brand_spec
    background = spec.background if re.fullmatch(r"#[0-9A-Fa-f]{6}", spec.background or "") else "#FFFFFF"
    ink = spec.text if re.fullmatch(r"#[0-9A-Fa-f]{6}", spec.text or "") else "#111111"
    if bg._contrast(ink, background) < 4.5:
        ink = "#FFFFFF" if bg._luminance(background) < 0.35 else "#111111"
    muted = _muted(ink, background)
    esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")  # noqa: E731
    name = spec.name
    legal, address, website, socials = name, "", None, []
    if ctx.brand:
        c = bg.company(ctx.brand)
        name = c.get("name") or name
        legal = c.get("legal_name") or name
        address = (bg.node(ctx.brand, "address") or {}).get("text", "")
        website = bg.link(ctx.brand, "website")
        socials = [(n["network"], n["url"]) for n in bg.footer_socials(ctx.brand)]
    link_style = f"color:{ink};text-decoration:underline;"
    social_line = " &nbsp;·&nbsp; ".join(f'<a href="{esc(u)}" style="{link_style}">{esc(NETWORK_NAMES.get(net, net.title()))}</a>' for net, u in socials)
    site_link = f'<a href="{esc(website)}" style="{link_style}">{esc(urlparse(website).hostname.removeprefix("www."))}</a> &nbsp;·&nbsp; ' if website else ""
    mjml = f"""<mj-section background-color="{background}" padding="28px 24px 32px 24px">
  <mj-column>
    <mj-divider border-color="{muted}" border-width="1px" padding="0 0 20px 0" />
    {f'<mj-text align="center" color="{muted}" font-size="12px" line-height="20px" padding="0 0 10px 0">{social_line}</mj-text>' if social_line else ''}
    <mj-text align="center" color="{muted}" font-size="12px" line-height="18px" padding="0">
      You're receiving this email because you signed up for updates from {esc(name)}.<br />
      {esc(legal)}{(', ' + esc(address)) if address else ''}<br />
      {site_link}<a href="{UNSUBSCRIBE_TAG}" style="{link_style}">Unsubscribe</a>
    </mj-text>
  </mj-column>
</mj-section>"""
    block = await email_doc.compile_mjml_sections(mjml, spec.font_stack)
    html = email_doc.insert_after_sections(html, "footer", block)
    parts = [p for p, ok in (("socials", socials), ("address", address), ("website", website)) if ok]
    return html, [item("Footer", "fixed", f"added a footer with sender{', ' + ', '.join(parts) if parts else ''} and unsubscribe. "
                                          f"Replace {UNSUBSCRIBE_TAG} with your email platform's unsubscribe tag "
                                          "(Klaviyo {% unsubscribe_url %}, Mailchimp *|UNSUB|*)")]


# ── links ─────────────────────────────────────────────────────────────────────


def _is_placeholder(href: str) -> bool:
    h = href.strip().lower()
    return (not h or h in ("#", "/") or h.startswith(("javascript:", "#")) or "example.com" in h
            or "your-link" in h or "placeholder" in h or h in ("http://", "https://"))


def _best_url(text: str, ctx: Context) -> Optional[str]:
    t = text.lower()
    if ctx.product and not any(w in t for w in ("instagram", "facebook", "tiktok", "youtube", "twitter", "unsubscribe")):
        return ctx.product.get("url")
    if ctx.brand:
        return bg.link(ctx.brand, "shop") or bg.link(ctx.brand, "website")
    return None


async def check_links(html: str, ctx: Context) -> tuple[str, list[dict]]:
    import httpx

    hrefs: dict[str, str] = {}
    for l in email_doc.links(html):
        hrefs.setdefault(l["href"], l["text"])
    items, ok, unverified = [], 0, 0
    fetcher = bg.Fetcher(ctx.allow_host)  # public HTTPS only, each host let through as it is reached
    try:
        for href, text in hrefs.items():
            h = href.strip()
            if MERGE_TAG.match(h) or h.lower().startswith(("mailto:", "tel:", "sms:")):
                continue
            problem = None
            if _is_placeholder(h):
                problem = "a placeholder"
            elif h.startswith(("http://", "https://")):
                try:
                    await fetcher.get(h, max_bytes=50_000_000)
                    ok += 1
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    if code in (404, 410):
                        problem = f"broken ({code} page not found)"
                    else:
                        unverified += 1  # many sites refuse automated requests (400/403/429…); not proof it's broken
                except ValueError as exc:
                    if "larger than" in str(exc):
                        ok += 1
                    else:
                        problem = f"not loadable ({str(exc).splitlines()[0][:60]})"
                except Exception as exc:
                    problem = f"not loadable ({type(exc).__name__})"
            else:
                problem = "not a web link"
            if not problem:
                continue
            host = (urlparse(h).hostname or "").removeprefix("www.")
            if any(host == k or host.endswith("." + k) for k in bg.SOCIAL_HOSTS):
                items.append(item("Links", "warn", f"social link “{text[:40]}” is {problem}: {h[:80]}"))
                continue  # never swap a social profile for a shop page
            target = _best_url(text, ctx)
            if target and target != h:
                html = email_doc.replace_href(html, h, target)
                items.append(item("Links", "fixed", f"“{text[:40] or 'link'}” was {problem} ({h[:60]}) → {target}"))
            else:
                items.append(item("Links", "warn", f"“{text[:40] or 'link'}” is {problem}: {h[:80]}"))
    finally:
        await fetcher.close()
    if not items:
        items.append(item("Links", "pass", f"{ok} link{'s' if ok != 1 else ''} checked and loading"
                                          + (f"; {unverified} on sites that block automated checks" if unverified else "")))
    return html, items


# ── alt text ──────────────────────────────────────────────────────────────────


async def fix_alt_text(html: str, ctx: Context) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    line_start = [0]
    for line in html.splitlines(keepends=True):
        line_start.append(line_start[-1] + len(line))
    edits, missing = [], 0
    name = ctx.brand_spec.name
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        if alt and not re.fullmatch(r"[\w.-]+\.(png|jpe?g|webp|gif)", alt, re.I):
            continue
        src = img.get("src", "")
        desc = next((d for k, d in ctx.image_descriptions.items() if k and src.endswith(k)), "")
        if not desc and "/logo-" in src:
            desc = f"{name} logo"
        if not desc:
            missing += 1
            continue
        if img.sourceline:
            edits.append((line_start[img.sourceline - 1] + img.sourcepos, desc))
    for start, desc in sorted(edits, reverse=True):
        end = logo_check._tag_end(html, start)
        tag = html[start:end]
        safe = desc[:150].replace('"', "&quot;").replace("<", "&lt;")
        tag = re.sub(r'\salt\s*=\s*(["\']).*?\1', "", tag, count=1)
        tag = re.sub(r"^<img\b", f'<img alt="{safe}"', tag)
        html = html[:start] + tag + html[end:]
    if edits:
        return html, [item("Alt text", "fixed", f"added alt text to {len(edits)} image{'s' if len(edits) != 1 else ''}"
                                               + (f"; {missing} still without" if missing else ""))]
    if missing:
        return html, [item("Alt text", "warn", f"{missing} image{'s' if missing != 1 else ''} without alt text")]
    return html, [item("Alt text", "pass", "every image has alt text")]


# ── logo ──────────────────────────────────────────────────────────────────────


async def check_logo(html: str, ctx: Context) -> tuple[str, list[dict]]:
    placements = _logo_placements(ctx)
    if not placements:
        return html, [item("Logo", "skipped", "no saved company graph, so no logo to check")]
    out = []
    for graph, place in placements:
        label = {"product": "Product logo", "signoff": "Brand sign-off"}.get(place, "Logo")
        html, notes = await logo_check.check_and_fix(html, graph, ctx.brand_spec.background, log=ctx.log)
        added = False
        if any("no brand logo" in n for n in notes):
            # The designer left this logo out: add it as its own row, then check it like any other.
            html = await _add_logo_row(html, graph, place, ctx)
            html, notes = await logo_check.check_and_fix(html, graph, ctx.brand_spec.background, log=ctx.log)
            added = True
            where = {"header": "a header row at the top", "product": "a row above the product",
                     "signoff": "a small sign-off above the footer"}[place]
            what = {"header": "logo", "product": "product logo", "signoff": "brand logo"}[place]
            out.append(item(label, "fixed", f"the email had no {what}; added it as {where}"))
        for n in notes:
            n = n.removeprefix("Logo check: ")
            status = "fixed" if ("swapped" in n or "band" in n) else "warn" if ("still" in n or "no brand logo" in n) else "pass"
            if added and status == "pass":
                continue  # the added row is readable; one line says so
            out.append(item(label, status, n))
    return html, out


def _logo_placements(ctx: Context) -> list[tuple[dict, str]]:
    """Which logos this email should carry, per the product's logo setting: (graph, "header" | "product")."""
    mode = pg.logo_mode(ctx.product)
    if mode == "product":
        # The product leads; the brand still signs off small in the footer.
        return [(ctx.product, "header")] + ([(ctx.brand, "signoff")] if ctx.brand else [])
    out = [(ctx.brand, "header")] if ctx.brand else []
    if mode == "both":
        out.append((ctx.product, "product"))
    return out


async def _add_logo_row(html: str, graph: dict, place: str, ctx: Context) -> str:
    from bootstrap import LocalStorageAdapter

    spec = ctx.brand_spec
    background = spec.background if re.fullmatch(r"#[0-9A-Fa-f]{6}", spec.background or "") else "#FFFFFF"
    logo = bg.pick_logo(graph, background)
    if not logo:
        return html
    name = bg.company(graph).get("name") or (bg.node(graph, "product") or {}).get("name") or spec.name
    url = LocalStorageAdapter().url(logo["file"])
    wide = logo.get("width", 1) / max(1, logo.get("height", 1)) > 2
    width = {"header": 160 if wide else 90, "product": 220 if wide else 110, "signoff": 96 if wide else 56}[place]
    alt = f"{name} logo".replace('"', "&quot;")
    padding = {"header": "28px 24px 20px 24px", "product": "8px 24px 16px 24px", "signoff": "24px 24px 4px 24px"}[place]
    mjml = f"""<mj-section background-color="{background}" padding="{padding}">
  <mj-column><mj-image src="{url}" alt="{alt}" width="{width}px" align="center" padding="0" /></mj-column>
</mj-section>"""
    block = await email_doc.compile_mjml_sections(mjml, spec.font_stack)
    if place == "header":
        return email_doc.insert_before_sections(html, "logo_header", block)
    if place == "signoff":
        # Just above the footer: our added footer, or whichever section holds the unsubscribe link.
        foot = next((x["id"] for x in email_doc.sections(html) if x["id"] == "footer" or "unsubscribe" in x["text"].lower()), "footer")
        return email_doc.insert_before_section(html, foot, "brand_signoff", block)
    # The product logo goes after the brand logo: after the section holding it, or after the added header row.
    brand_files = [n["file"] for n in bg.nodes(ctx.brand, "Logo")] if ctx.brand else []
    holder = next((s["id"] for s in email_doc.sections(html) if any(f"/files/{f}" in s["inner"] for f in brand_files)), None)
    return email_doc.insert_after_section(html, holder, "product_logo", block) if holder else email_doc.insert_before_sections(html, "product_logo", block)


# ── text contrast ─────────────────────────────────────────────────────────────

_MARK_TEXT = """() => {
  const out = [];
  [...document.body.querySelectorAll('*')].forEach((e, i) => {
    const own = [...e.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(' ').replace(/\\s+/g, ' ').trim();
    if (!own) return;
    const cs = getComputedStyle(e), r = e.getBoundingClientRect();
    if (r.width < 4 || r.height < 6 || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.2 || parseFloat(cs.fontSize) < 8) return;
    let p = e, hidden = false;
    while (p) { const s = getComputedStyle(p); if (s.display === 'none' || s.maxHeight === '0px' || parseFloat(s.opacity) === 0) { hidden = true; break; } p = p.parentElement; }
    if (hidden) return;
    e.setAttribute('data-cc', String(i));
    out.push({ i, tag: e.tagName.toLowerCase(), text: own, color: cs.color, size: parseFloat(cs.fontSize),
               weight: parseInt(cs.fontWeight) || 400, x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height });
  });
  return out;
}"""


def _rgb(css: str) -> Optional[str]:
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?", css or "")
    if not m or (m.group(4) is not None and float(m.group(4)) < 0.5):
        return None
    return "#%02X%02X%02X" % tuple(int(m.group(k)) for k in (1, 2, 3))


def _median_color(img: Image.Image) -> str:
    small = img.convert("RGB")
    small.thumbnail((60, 60))
    px = sorted(small.getdata(), key=lambda p: p[0] * 0.3 + p[1] * 0.59 + p[2] * 0.11)
    return "#%02X%02X%02X" % px[len(px) // 2]


async def _measure_text(html: str) -> list[dict]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 700, "height": 1000})
            await page.set_content(html, wait_until="load")
            await page.wait_for_timeout(300)
            spots = await page.evaluate(_MARK_TEXT)
            await page.add_style_tag(content="* { color: transparent !important; -webkit-text-fill-color: transparent !important; text-shadow: none !important; }")
            shot = Image.open(io.BytesIO(await page.screenshot(full_page=True)))
        finally:
            await browser.close()
    for s in spots:
        box = (int(s["x"]), int(s["y"]), int(s["x"] + s["w"]), int(s["y"] + s["h"]))
        box = (max(0, box[0]), max(0, box[1]), min(shot.width, max(box[0] + 1, box[2])), min(shot.height, max(box[1] + 1, box[3])))
        s["bg"] = _median_color(shot.crop(box))
        s["fg"] = _rgb(s["color"])
        large = s["size"] >= 24 or (s["size"] >= 18.66 and s["weight"] >= 700)
        s["need"] = 3.0 if large else 4.5
        s["ratio"] = bg._contrast(s["fg"], s["bg"]) if s["fg"] else 21
    return spots


def _set_style(tag_src: str, prop: str, value: str) -> str:
    m = re.search(r'\sstyle\s*=\s*(["\'])(.*?)\1', tag_src, re.S)
    decl = f"{prop}:{value} !important"
    if m:
        style = re.sub(rf"(^|;)\s*{prop}\s*:[^;]*", r"\1", m.group(2)).strip().strip(";")
        return tag_src[: m.start()] + f' style="{style};{decl}"' + tag_src[m.end():] if style else tag_src[: m.start()] + f' style="{decl}"' + tag_src[m.end():]
    return re.sub(r"^<(\w+)", lambda mm: f'<{mm.group(1)} style="{decl}"', tag_src)


async def fix_contrast(html: str, ctx: Context) -> tuple[str, list[dict]]:
    spots = await _measure_text(html)
    bad = [s for s in spots if s["fg"] and s["ratio"] < s["need"]]
    if not bad:
        return html, [item("Contrast", "pass", f"all {len(spots)} text blocks are readable on their backgrounds")]
    spec = ctx.brand_spec
    palette = [c for c in (spec.text, spec.accent_text, spec.accent, spec.background) if re.fullmatch(r"#[0-9A-Fa-f]{6}", c or "")] + ["#111111", "#FFFFFF"]
    soup = BeautifulSoup(html, "html.parser")
    line_start = [0]
    for line in html.splitlines(keepends=True):
        line_start.append(line_start[-1] + len(line))
    edits, unfixed = [], []
    for s in bad:
        new = next((c for c in palette if bg._contrast(c, s["bg"]) >= s["need"]), max(palette, key=lambda c: bg._contrast(c, s["bg"])))
        matches = [t for t in soup.find_all(s["tag"])
                   if " ".join(" ".join(x for x in t.find_all(string=True, recursive=False)).split()) == s["text"]]
        if len(matches) == 1 and matches[0].sourceline:
            edits.append((line_start[matches[0].sourceline - 1] + matches[0].sourcepos, new, s))
        else:
            unfixed.append(s)
    for start, new, s in sorted(edits, key=lambda e: -e[0]):
        end = logo_check._tag_end(html, start)
        html = html[:start] + _set_style(html[start:end], "color", new) + html[end:]
    items = [item("Contrast", "fixed", f"“{s['text'][:50]}” was {s['ratio']:.1f}:1 on {s['bg']} → now {new}")
             for _, new, s in edits]
    items += [item("Contrast", "warn", f"“{s['text'][:50]}” is {s['ratio']:.1f}:1 on {s['bg']} (needs {s['need']:.1f}:1); couldn't edit it automatically")
              for s in unfixed]
    return html, items


# ── dark mode ─────────────────────────────────────────────────────────────────


async def check_dark_mode(html: str, ctx: Context) -> tuple[str, list[dict]]:
    """Email apps with a dark mode (Outlook, the Gmail app) darken light backgrounds but leave
    images alone, so a dark logo on a transparent background can disappear."""
    placements = _logo_placements(ctx)
    if not placements:
        return html, [item("Dark mode", "skipped", "no logo to check")]
    from playwright.async_api import async_playwright

    logos: dict[str, dict] = {}
    for graph, _ in placements:
        for n in bg.nodes(graph, "Logo"):
            for found in re.findall(r'https?://[^"\'\s)]*?/files/' + re.escape(n["file"]), html):
                logos[found] = n
    if not logos:
        return html, [item("Dark mode", "skipped", "no logo in the email")]
    worst = 1.0
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 700, "height": 1000})
            for url, node in logos.items():
                if node.get("transparent") is False:
                    continue
                for spot in await logo_check._measure(page, html, url):
                    light_bg = bg._luminance(_median_color(spot["behind"])) > 0.5
                    if not light_bg:
                        continue  # dark backgrounds are left as they are in dark mode
                    darkened = Image.eval(spot["behind"].convert("RGB"), lambda v: 255 - v)
                    size = (max(1, round(spot["w"])), max(1, round(spot["h"])))
                    worst = min(worst, logo_check.legibility(logo_check._logo_pixels(node, size), darkened))
        finally:
            await browser.close()
    if worst >= logo_check.MIN_VISIBLE_SHARE:
        return html, [item("Dark mode", "pass", "the logo stays readable when an email app switches to dark mode")]
    return html, [item("Dark mode", "warn", f"in dark mode (Outlook, Gmail app) the background darkens but the logo doesn't; only {worst:.0%} of its edge "
                                          "would stand out. Use a logo version with a light outline, or a header band in a solid brand color")]


# ── email clients: code that some email apps ignore (caniemail data) ──────────


def client_features(html: str) -> list[dict]:
    """Features in the email that major email apps don't fully support, beyond what MJML itself writes.

    Hidden elements (the preheader) and Outlook-only conditional code are left out: they're
    deliberate, and hidden content never renders anyway.
    """
    import design_generic

    generic = design_generic.load()
    # MJML's own output, plus extras that only add polish where supported (hover states, media queries).
    baseline = set(generic.get("mjml_baseline", [])) | {"css-pseudo-class-hover", "css-at-media"}
    visible = re.sub(r"<!--\[if [^\]]*\]>.*?<!\[endif\]-->", "", html, flags=re.S)
    soup = BeautifulSoup(visible, "html.parser")
    for el in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        el.decompose()
    code = str(soup)
    found = []
    for c in generic.get("constraints", []):
        if c["level"] == "safe" or not c.get("detect") or c["id"] in baseline:
            continue
        if re.search(c["detect"], code, re.I):
            found.append(c)
    return found


async def check_clients(html: str, ctx: Context) -> tuple[str, list[dict]]:
    found = client_features(html)
    if not found:
        return html, [item("Email clients", "pass", "uses only code that works across Apple Mail, Gmail, Outlook, Yahoo and Samsung Email")]
    import design_generic

    bad = [c for c in found if c["level"] == "avoid" or c["id"] in design_generic.DESIGN_RELEVANT]
    def say(c: dict) -> str:
        who = c["unsupported"] or c["partial"]
        return f"{c['title']} (not in {', '.join(who[:3])}{'…' if len(who) > 3 else ''})"
    status = "warn" if bad else "pass"
    lead = "uses code some email apps ignore: " if bad else "works broadly; a few details differ in some apps: "
    return html, [item("Email clients", status, lead + "; ".join(say(c) for c in (bad or found)[:6]))]


# ── inbox: size, images, spam words, subject lines ───────────────────────────


async def check_inbox(html: str, ctx: Context) -> tuple[str, list[dict]]:
    items = []
    size_kb = len(html.encode()) / 1024
    if size_kb > 102:
        items.append(item("Inbox", "warn", f"HTML is {size_kb:.0f} KB; Gmail clips emails over 102 KB (hides the rest behind “View entire message”)"))
    else:
        items.append(item("Inbox", "pass", f"HTML is {size_kb:.0f} KB (Gmail clips over 102 KB)"))
    total, count = 0, 0
    for src in re.findall(r'<img[^>]+src="([^"]+)"', html):
        count += 1
        if "/files/" in src:
            path = STORAGE_DIR / src.split("/files/", 1)[1]
            if path.exists():
                total += path.stat().st_size
    if total > 1_500_000:
        items.append(item("Inbox", "warn", f"{count} images totalling {total / 1e6:.1f} MB; over ~1.5 MB loads slowly on mobile data"))
    text = email_doc.visible_text(html)
    if count >= 3 and len(text) < 250:
        items.append(item("Inbox", "warn", f"mostly images ({count}) and little text ({len(text)} characters); spam filters prefer more text"))
    corpus = (text + " " + " ".join(f"{s.get('subject', '')} {s.get('preheader', '')}" for s in ctx.subject_lines)).lower()
    hits = sorted({p for p in SPAM_PHRASES if p in corpus})
    caps = re.findall(r"\b[A-Z]{5,}\b", " ".join(s.get("subject", "") for s in ctx.subject_lines))
    if hits:
        items.append(item("Inbox", "warn", "spam-trigger phrases: " + ", ".join(f"“{h}”" for h in hits)))
    if caps:
        items.append(item("Inbox", "warn", "ALL-CAPS words in subject lines: " + ", ".join(sorted(set(caps))[:5])))
    long = [s["subject"] for s in ctx.subject_lines if len(s.get("subject", "")) > 60]
    if long:
        items.append(item("Inbox", "warn", f"{len(long)} subject line{'s are' if len(long) > 1 else ' is'} over 60 characters and will be cut off on phones"))
    if len(items) == 1 and items[0]["status"] == "pass":
        items[0]["detail"] += "; no spam phrases; subject lengths fine"
    return html, items
