"""Import a brand's past emails into its design layer.

Sources: uploaded .eml / .html files, or links to the brand's public email archive or "view in
browser" pages (an archive index is followed to up to 10 of its emails). For each email:

    1. its HTML: the text/html part of an .eml (inline cid: images restored), or the fetched page
    2. rendered at 600px in a browser and split into sections: the stacked full-width blocks of
       the email's main column
    3. measured: buttons, headings, body text, image corners, section padding
    4. the model matches each section to a generic pattern (from a screenshot) and rates how
       clear an example it is

Across all the emails:
    patterns used in at least 30% of them   → Preference "prefers" (with counts)
    patterns never used, from 6+ emails     → Preference "avoids" (starts unticked)
    the most common measured values         → DesignTokens from "past emails"
    the clearest section of each favourite  → Component (a screenshot and a description)

Everything comes back as a proposal to review, like the other design imports.
"""

from __future__ import annotations

import asyncio
import base64
import email
import email.policy
import io
import json
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin

import bootstrap
import brand_design
import brand_graph as bg
import design_generic as dg

MAX_EMAILS = 20


def _emails_dir(brand_id: str) -> Path:
    d = bg.graph_path(brand_id).parent / "emails"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_upload(brand_id: str, filename: str, data: bytes) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).name).strip("-")[:80] or "email.html"
    name = f"{uuid.uuid4().hex[:8]}-{safe}"
    (_emails_dir(brand_id) / name).write_bytes(data)
    return name


# ── getting each email's HTML ─────────────────────────────────────────────────


def _eml_html(data: bytes) -> str:
    """The HTML body of an .eml, with inline (cid:) images turned into data URIs so they render."""
    msg = email.message_from_bytes(data, policy=email.policy.default)
    body = msg.get_body(preferencelist=("html",))
    if body is None:
        raise ValueError("this .eml has no HTML part")
    html = body.get_content()
    for part in msg.walk():
        cid = (part.get("Content-ID") or "").strip("<>")
        if cid and part.get_content_maintype() == "image":
            uri = f"data:{part.get_content_type()};base64,{base64.b64encode(part.get_payload(decode=True)).decode()}"
            html = html.replace(f"cid:{cid}", uri)
    return html


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


ARCHIVE_LINK = re.compile(r"campaign-archive\.com/\?|mailchi\.mp/|/archive/|view.?(in|online|web)|webversion|emailview", re.I)


async def _from_links(urls: list[str], fetcher, log) -> list[dict]:
    """Fetch each link; an archive index (a page listing many campaigns) is followed to up to 10 of them."""
    from bs4 import BeautifulSoup

    out: list[dict] = []
    for url in urls:
        try:
            html = (await fetcher.get(url)).text
        except Exception as exc:
            log(f"  skipped {url[:80]}: {str(exc).splitlines()[0][:80]}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        links = list(dict.fromkeys(urljoin(url, a["href"]) for a in soup.find_all("a", href=True)
                                   if ARCHIVE_LINK.search(a["href"]) and urljoin(url, a["href"]) != url))
        looks_like_email = bool(soup.find("table")) and len(soup.get_text(" ", strip=True)) > 200
        if len(links) >= 3 and not (looks_like_email and len(links) < 6):
            log(f"{url[:70]} lists {len(links)} emails; reading up to 10")
            for link in links[:10]:
                try:
                    out.append({"name": link, "source": link, "html": (await fetcher.get(link)).text})
                except Exception as exc:
                    log(f"  skipped {link[:80]}: {str(exc).splitlines()[0][:80]}")
        else:
            out.append({"name": url, "source": url, "html": html})
    return out


# ── rendering, splitting and measuring ────────────────────────────────────────

SPLIT_JS = r"""() => {
  const box = (el) => el.getBoundingClientRect();
  const shown = (el) => { const r = box(el); const s = getComputedStyle(el); return r.height > 2 && r.width > 0 && s.display !== 'none' && s.visibility !== 'hidden'; };
  // The main column: the tallest element 480-720px wide.
  let main = document.body, best = 0;
  for (const el of document.body.querySelectorAll('table, div, center, td')) {
    const r = box(el);
    if (r.width >= 480 && r.width <= 720 && r.height > best) { best = r.height; main = el; }
  }
  const kids = (el) => [...el.children].filter(shown).filter((c) => !['STYLE', 'SCRIPT'].includes(c.tagName));
  const blocksOf = (el) => (el.tagName === 'TABLE' ? [...el.querySelectorAll(':scope > tbody > tr, :scope > tr')].filter(shown) : kids(el));
  // Go down while there's one block holding nearly everything.
  let node = main;
  for (let i = 0; i < 25; i++) {
    const blocks = blocksOf(node);
    const total = box(node).height || 1;
    const big = blocks.find((b) => box(b).height > total * 0.85);
    if (blocks.length === 1 || (big && blocks.length < 4)) node = big || blocks[0]; else break;
  }
  const y0 = window.scrollY;
  const sections = blocksOf(node).map((b) => {
    const r = box(b);
    const imgs = [...b.querySelectorAll('img')].filter(shown);
    const links = [...b.querySelectorAll('a')].filter(shown).map((a) => (a.innerText || '').trim()).filter(Boolean);
    const lefts = new Set(imgs.map((i) => Math.round(box(i).left / 40)));
    return { y: Math.round(r.top + y0), h: Math.round(r.height), x: Math.round(r.left), w: Math.round(r.width),
      text: (b.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 240), images: imgs.length, columns: Math.max(1, lefts.size),
      links: links.slice(0, 8) };
  }).filter((s) => s.h >= 16);
  // The header logo: the first small image near the top (a wordmark or mark, not a banner).
  const top = [...document.images].filter(shown).map((i) => ({ i, r: box(i) }))
    .filter(({ r }) => r.top + y0 < 320 && r.width >= 40 && r.width <= 360 && r.height >= 12 && r.height <= 160)
    .sort((a, b) => a.r.top - b.r.top)[0];
  const logo = top ? { src: top.i.currentSrc || top.i.src, w: Math.round(top.r.width), h: Math.round(top.r.height), alt: top.i.alt || '' } : null;
  return { width: Math.round(box(main).width), height: Math.round(document.documentElement.scrollHeight), sections, logo };
}"""

# Emails rarely use h1/h2: headings are whatever text is set large. Buttons are links with a fill (or in a filled cell).
MEASURE_JS = r"""() => {
  const shown = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.height > 2 && r.width > 0 && s.display !== 'none' && s.visibility !== 'hidden'; };
  const filled = (c) => c && !/rgba\(0, 0, 0, 0\)|transparent/.test(c);
  const buttons = [];
  for (const a of document.querySelectorAll('a')) {
    if (!shown(a)) continue;
    const t = (a.innerText || '').trim(); const r = a.getBoundingClientRect();
    if (t.length < 2 || t.length > 30 || r.width < 60 || r.width > 480) continue;
    let host = a, s = getComputedStyle(a);
    if (!filled(s.backgroundColor) && a.parentElement && ['TD', 'DIV', 'P'].includes(a.parentElement.tagName)) {
      const ps = getComputedStyle(a.parentElement);
      const pr = a.parentElement.getBoundingClientRect();
      if (filled(ps.backgroundColor) && pr.width < 500 && pr.height < 90) { host = a.parentElement; }
    }
    const hs = getComputedStyle(host); const hr = host.getBoundingClientRect();
    if (!filled(hs.backgroundColor) && parseFloat(hs.borderTopWidth) < 1) continue;
    if (hr.height < 28 || hr.height > 90) continue;
    buttons.push({ borderRadius: hs.borderRadius, paddingTop: Math.max(parseFloat(s.paddingTop), parseFloat(hs.paddingTop)) + 'px',
      paddingRight: Math.max(parseFloat(s.paddingRight), parseFloat(hs.paddingRight)) + 'px', textTransform: s.textTransform,
      letterSpacing: s.letterSpacing, fontWeight: s.fontWeight, fontSize: s.fontSize, h: Math.round(hr.height),
      // The label colour is on whatever element holds the text (often a span inside the link).
      backgroundColor: hs.backgroundColor, color: getComputedStyle([...a.querySelectorAll('*')].reverse()
        .find((el) => [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) || a).color, text: t });
  }
  const texts = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  while (walker.nextNode()) {
    const el = walker.currentNode.parentElement;
    if (!el || seen.has(el) || !shown(el) || walker.currentNode.textContent.trim().length < 3) continue;
    seen.add(el);
    const s = getComputedStyle(el);
    texts.push({ fontSize: s.fontSize, fontWeight: s.fontWeight, textTransform: s.textTransform, letterSpacing: s.letterSpacing,
      lineHeight: s.lineHeight, len: (el.innerText || '').trim().length });
  }
  const images = [...document.querySelectorAll('img')].filter((i) => shown(i) && i.getBoundingClientRect().width > 250).map((i) => ({ borderRadius: getComputedStyle(i).borderRadius }));
  return { buttons, texts, images };
}"""


async def _render(emails: list[dict], log) -> list[dict]:
    """Render each email: sections, measurements and a full screenshot."""
    from playwright.async_api import async_playwright

    out = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            for e in emails:
                page = await browser.new_page(viewport={"width": 700, "height": 900})
                try:
                    await page.set_content(e["html"], wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    split = await page.evaluate(SPLIT_JS)
                    measured = await page.evaluate(MEASURE_JS)
                    # The whole email: sections near the bottom of a long email are cropped from this too.
                    height = min(split["height"], 20000)
                    shot = await page.screenshot(clip={"x": 0, "y": 0, "width": 700, "height": height}, full_page=True)
                    if len(split["sections"]) < 2:
                        log(f"  {e['name'][:60]}: couldn't find its sections; skipped")
                        continue
                    out.append({**e, **split, "measured": measured, "shot": shot})
                    log(f"Read {e['name'][:60]}: {len(split['sections'])} sections")
                except Exception as exc:
                    log(f"  {e['name'][:60]}: {str(exc).splitlines()[0][:100]}")
                finally:
                    await page.close()
        finally:
            await browser.close()
    return out


def _email_tokens(rendered: list[dict]) -> dict[str, dict]:
    """The brand's email tokens: the most common values across all the emails."""
    from brand_design import _clamp, _mode, _px

    btns = [b for r in rendered for b in r["measured"]["buttons"]]
    texts = [t for r in rendered for t in r["measured"]["texts"]]
    imgs = [i for r in rendered for i in r["measured"]["images"]]
    n_emails = len(rendered)
    found: dict[str, dict] = {}

    def put(name, value, evidence, share, n=3):
        if value not in (None, ""):
            found[name] = {"value": str(value), "evidence": evidence, "confidence": round(share, 2) if n >= 3 else 0.0, "seen": n}

    if btns:
        def radius(b):
            first = b["borderRadius"].split()[0]
            r = _px(first[:-1]) * b["h"] / 100 if first.endswith("%") else _px(first)
            return "24px" if r >= b["h"] / 2 - 1 else f"{int(r)}px"
        v, share, n = _mode([radius(b) for b in btns])
        put("radius", v, f"{n} buttons across {n_emails} emails; {share:.0%} have {v} corners", share, n)
        v, share, n = _mode([(_clamp(_px(b["paddingTop"]), 12, 20), _clamp(_px(b["paddingRight"]), 18, 48)) for b in btns])
        if v:
            put("button_padding", f"{v[0]}px {v[1]}px", f"buttons padded about {v[0]}px × {v[1]}px ({share:.0%} of {n})", share, n)
        v, share, n = _mode([b["textTransform"] for b in btns])
        put("button_case", "uppercase" if v == "uppercase" else "none", f"button labels {v} on {share:.0%} of {n} buttons", share, n)
        v, share, n = _mode(["0px" if b["letterSpacing"] == "normal" else f"{round(_px(b['letterSpacing']), 1):g}px" for b in btns])
        put("button_letter_spacing", v, f"button letter-spacing {v} ({share:.0%} of {n})", share, n)
        v, share, n = _mode([b["fontWeight"] for b in btns])
        put("button_weight", v, f"button label weight {v} ({share:.0%} of {n})", share, n)
    big = [t for t in texts if _px(t["fontSize"]) >= 20 and t["len"] <= 120]
    if big:
        sizes = sorted({round(_px(t["fontSize"])) for t in big}, reverse=True)
        v, share, n = _mode([round(_px(t["fontSize"])) for t in big if round(_px(t["fontSize"])) >= sizes[0] - 2])
        put("h1_size", f"{_clamp(v, 22, 44)}px", f"largest headlines {v}px ({n} seen across {n_emails} emails)", share, n)
        v, share, n = _mode([t["textTransform"] for t in big])
        put("heading_case", "uppercase" if v == "uppercase" else "none", f"headlines {v} ({share:.0%} of {n})", share, n)
        v, share, n = _mode(["0px" if t["letterSpacing"] == "normal" else f"{round(_px(t['letterSpacing']), 1):g}px" for t in big])
        put("heading_letter_spacing", v, f"headline letter-spacing {v} ({share:.0%} of {n})", share, n)
        v, share, n = _mode([t["fontWeight"] for t in big])
        put("heading_weight", v, f"headline weight {v} ({share:.0%} of {n})", share, n)
    body = [t for t in texts if 12 <= _px(t["fontSize"]) <= 19 and t["len"] >= 60]
    if body:
        v, share, n = _mode([round(_px(t["fontSize"])) for t in body])
        put("body_size", f"{_clamp(v, 13, 18)}px", f"body text {v}px ({n} paragraphs)", share, n)
        v, share, n = _mode([round(_px(t["lineHeight"]) / max(_px(t["fontSize"]), 1), 1) for t in body if t["lineHeight"] != "normal"])
        if v:
            put("line_height", f"{min(max(v, 1.3), 1.8):g}", f"body line-height {v:g} ({n} paragraphs)", share, n)
    if imgs:
        v, share, n = _mode([f"{int(_px(i['borderRadius'].split()[0]))}px" for i in imgs])
        put("image_radius", v, f"{n} large images; {share:.0%} have {v} corners", share, n)
    return found


# ── matching sections to patterns ─────────────────────────────────────────────

MATCH_SYSTEM = """You analyse a marketing email's layout for a design system. Describe structure and visual style only;
never quote or reuse its copy. Answer with JSON only."""


def _match_prompt(e: dict, patterns: list[dict]) -> str:
    lib = [{"id": p["id"], "name": p["name"], "anatomy": p.get("anatomy")} for p in patterns]
    total = max(e["height"], 1)
    secs = [{"n": i, "starts_at": f"{s['y'] / total:.0%}", "height": f"{s['h'] / total:.0%}", "images": s["images"], "columns": s["columns"],
             "links": s["links"][:4], "text_start": s["text"][:120]} for i, s in enumerate(e["sections"])]
    return f"""The attached image is a screenshot of one whole marketing email (the email column is about {e['width']}px wide).
Its sections, top to bottom (where each starts and how tall it is, as a share of the email's height): {json.dumps(secs)}

Pattern library: {json.dumps(lib)}

For each section: the pattern it matches best ("other" if none fits; "spacer" for empty space), a one-line description of its
layout and visual style (no copy), and how clear an example of that pattern it is (1-5).
Return {{"sections": [{{"n": 0, "pattern": "<id|other|spacer>", "description": "...", "quality": 1-5}}]}}"""


def _jpeg(png: bytes, max_w: int = 600, max_h: int = 7000, crop: Optional[tuple] = None) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    if crop:
        img = img.crop(crop)
    img.thumbnail((max_w, max_h))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _slices(png: bytes, step: int = 1800, most: int = 8) -> list[bytes]:
    """A long email as consecutive slices, so each stays legible (one very tall image gets shrunk to a sliver)."""
    from PIL import Image

    h = Image.open(io.BytesIO(png)).height
    return [_jpeg(png, 600, step, crop=(0, y, 700, min(h, y + step))) for y in range(0, min(h, step * most), step)]


async def _match(e: dict, patterns: list[dict], sem: asyncio.Semaphore) -> list[dict]:
    slices = _slices(e["shot"])
    images = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(s).decode()}} for s in slices]
    note = f"\n(The screenshot is attached as {len(slices)} consecutive slices, top to bottom.)" if len(slices) > 1 else ""
    async with sem:
        out = await bg._ask_model(MATCH_SYSTEM, [{"type": "text", "text": _match_prompt(e, patterns) + note}, *images], max_tokens=3000)
    ids = {p["id"] for p in patterns}
    found = []
    for s in out.get("sections") or []:
        if isinstance(s, dict) and isinstance(s.get("n"), int) and 0 <= s["n"] < len(e["sections"]) and s.get("pattern") in ids:
            found.append({**s, **e["sections"][s["n"]]})
    return found


# ── the brand's logo and button colours, as its emails use them ───────────────


async def _logo_change(brand_id: str, rendered: list[dict], log, batch: str) -> list[dict]:
    """The header logo most of the emails share, proposed as the brand's logo (it replaces the website's)."""

    n = len(rendered)
    keyed = [(re.sub(r"[?#].*$", "", e["logo"]["src"]) if e["logo"]["src"].startswith("http") else e["logo"]["src"][:200], e)
             for e in rendered if e.get("logo") and e["logo"]["src"].startswith(("http", "data:image/"))]
    if not keyed:
        return []
    key, count = Counter(k for k, _ in keyed).most_common(1)[0]
    if count / n < 0.5:
        return []
    e = next(e for k, e in keyed if k == key)
    src = e["logo"]["src"]
    try:
        if src.startswith("data:"):
            data = base64.b64decode(src.split(",", 1)[1])
        else:
            fetcher = bg.Fetcher(bootstrap.ALLOWED_HOSTS.add)
            try:
                data = (await fetcher.get(src, max_bytes=5_000_000)).content
            finally:
                await fetcher.close()
        img = bg._to_png(data)
    except Exception as exc:
        log(f"  couldn't fetch the emails' logo: {str(exc).splitlines()[0][:80]}")
        return []
    staged = f"brands/{brand_id}/design/{batch}-logo.png"
    buf = io.BytesIO()
    img.save(buf, "PNG")
    bootstrap.LocalStorageAdapter().upload_file_object(staged, buf.getvalue())
    current = bg.node(bg.load(brand_id), "logo:primary")
    return [{"op": "update" if current else "add", "type": "Logo", "fields": {"file": staged, "from": src[:300]},
             "origin": "emails", "source_url": e["source"],
             "evidence": f"the header logo in {count} of {n} emails (shown at {e['logo']['w']}×{e['logo']['h']}px)",
             "current": {"file": current["file"], "origin": "the company graph (from the website)"} if current else None,
             "doubt": "" if count >= 2 or n == 1 else "only in one email"}]


def _color_changes(brand_id: str, rendered: list[dict]) -> list[dict]:
    """The button fill and label colours the emails use, proposed as the brand's accent colours.

    One vote per email (its most common button colour), so an email with many buttons doesn't
    outvote the rest. When the emails split, the colour starts unticked and the split is shown.
    """
    from brand_design import _mode, _rgb_hex

    graph = bg.load(brand_id)
    out = []
    for role, key, label in (("accent", "backgroundColor", "button fill"), ("accent_text", "color", "button label")):
        votes = []
        for r in rendered:
            hexes = [_rgb_hex(b.get(key, "")) for b in r["measured"]["buttons"]]
            if role == "accent":
                hexes = [h for h in hexes if h != "#FFFFFF"]  # white buttons are outline / secondary ones
            top, _, n = _mode(hexes)
            if top:
                votes.append(top)
        if not votes:
            continue
        hex_, share, n = _mode(votes)
        cur = bg.color(graph, role, "")
        if cur.upper() == hex_.upper():
            continue
        split = Counter(votes).most_common()
        doubt = "" if share >= 0.6 else "The emails split: " + ", ".join(f"{h} in {c} of {n}" for h, c in split) + ". Pick the one you want (you can edit the value)."
        out.append({"op": "update" if cur else "add", "type": "BrandColor", "fields": {"role": role, "hex": hex_},
                    "origin": "emails", "source_url": f"{len(rendered)} imported emails",
                    "evidence": f"the main {label} in {split[0][1]} of {n} emails",
                    "current": {"role": role, "hex": cur, "origin": "the company graph (from the website)"} if cur else None,
                    "doubt": doubt})
    return out


# ── the import ────────────────────────────────────────────────────────────────


async def propose_from_emails(brand_id: str, req: dict, log: Callable[[str], None] = print) -> dict:
    """req: {"files": [names saved by save_upload], "urls": [links]}."""
    bg.load(brand_id)
    emails: list[dict] = []
    for name in (req.get("files") or [])[:MAX_EMAILS]:
        path = _emails_dir(brand_id) / Path(name).name
        data = path.read_bytes()
        try:
            html = _eml_html(data) if name.lower().endswith(".eml") or data[:200].lower().find(b"mime-version") >= 0 else _decode(data)
        except Exception as exc:
            log(f"  {name}: {exc}")
            continue
        emails.append({"name": name.split("-", 1)[-1], "source": f"uploaded {name.split('-', 1)[-1]}", "html": html})
    if req.get("urls"):
        fetcher = bg.Fetcher(bootstrap.ALLOWED_HOSTS.add)
        try:
            emails += await _from_links(req["urls"], fetcher, log)
        finally:
            await fetcher.close()
    emails = emails[:MAX_EMAILS]
    if not emails:
        raise LookupError("none of those could be read as an email")
    log(f"Rendering {len(emails)} email{'s' if len(emails) != 1 else ''}")
    rendered = await _render(emails, log)
    if not rendered:
        raise LookupError("couldn't find the sections of any of those emails")
    patterns = dg.load()["patterns"]
    log("Matching each email's sections to the pattern library")
    sem = asyncio.Semaphore(3)
    matches = await asyncio.gather(*[_match(e, patterns, sem) for e in rendered])
    n = len(rendered)

    changes = []
    # Which patterns the brand uses: count emails, not sections.
    used = Counter(p for m in matches for p in {s["pattern"] for s in m})
    for pid, count in used.most_common():
        if count / n >= 0.3:
            changes.append({"op": "add", "type": "Preference", "fields": {"pattern": pid, "stance": "prefers", "count": str(count), "of": str(n)},
                            "origin": "emails", "source_url": f"{n} imported emails", "evidence": f"used in {count} of {n} emails"})
    if n >= 6:
        for p in patterns:
            if p["id"] not in used and p.get("category") not in ("header", "footer"):
                changes.append({"op": "add", "type": "Preference", "fields": {"pattern": p["id"], "stance": "avoids", "count": "0", "of": str(n)},
                                "origin": "emails", "source_url": f"{n} imported emails", "evidence": f"not used in any of {n} emails",
                                "doubt": "Inferred from absence; tick it only if the brand really avoids this layout."})
    # The clearest example of each favourite pattern becomes a brand component. Crops get this import's own
    # file names, so an import that isn't saved never changes the pictures of components already saved.
    storage = bootstrap.LocalStorageAdapter()
    batch = uuid.uuid4().hex[:6]
    for pid, count in used.most_common(8):
        if count / n < 0.3:
            continue
        best = max(((e, s) for e, m in zip(rendered, matches) for s in m if s["pattern"] == pid), key=lambda es: es[1].get("quality", 0))
        e, s = best
        key = f"brands/{brand_id}/design/{batch}-{pid}.jpg"
        storage.upload_file_object(key, _jpeg(e["shot"], 600, 1200, crop=(max(0, s["x"]), s["y"], min(700, s["x"] + s["w"]), s["y"] + s["h"])))
        changes.append({"op": "add", "type": "Component",
                        "fields": {"pattern": pid, "slug": pid, "description": s.get("description") or pid, "thumbnail": key},
                        "origin": "emails", "source_url": e["source"], "evidence": f"clearest {pid} section (rated {s.get('quality', '?')}/5) in {e['name'][:60]}"})
    changes += await _logo_change(brand_id, rendered, log, batch)
    changes += _color_changes(brand_id, rendered)
    for name, t in _email_tokens(rendered).items():
        changes.append(brand_design._token_change(name, t["value"], "emails", f"{n} imported emails", t["evidence"],
                                                  doubt=(f"only seen {t['seen']} time{'s' if t['seen'] != 1 else ''}" if t["seen"] < 3 else f"only {t['confidence']:.0%} agree" if t["confidence"] < 0.4 else "")))
    return brand_design._proposal(brand_id, "emails", f"Design read from {n} of the brand's emails", changes,
                                  [e["source"] for e in rendered])
