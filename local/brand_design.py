"""The brand design layer: how one brand designs, imported from its own sources.

    website      the computed styles of real buttons, headings, body text, images and sections on
                 the brand's pages (rendered in a browser), plus a screenshot the model reads for
                 photo style and layout feel
    brand guide  the uploaded brand-guide.pdf: the button, type, spacing and photo rules it states
    past emails  the brand's own emails (email_import.py): the patterns it uses, its email tokens,
                 its best sections

Saved as a small graph in storage/brands/<id>/design.json (center "design"):

    DesignToken  one value per token in design_generic.TOKENS (radius, button_case, h1_size...)
    StyleNote    a short rule about the look (photography, layout, alignment)
    Preference   the brand favours or avoids a generic pattern (from past emails)
    Component    one of the brand's own email sections, kept as a reference (from past emails)

Imports are proposals: nothing is saved until you confirm (the same review as Modify). When two
sources disagree the stronger one wins: your edits > brand guide > past emails > website.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

import brand_graph as bg
import design_generic as dg

ORIGIN_RANK = {"website": 1, "emails": 2, "guide": 3, "you": 4}
ORIGIN_LABEL = {"website": "the website", "emails": "past emails", "guide": "the brand guide", "you": "your edit"}
NOTE_TOPICS = ("photography", "layout", "alignment", "decoration", "imagery", "typography", "buttons")


# ── storage ───────────────────────────────────────────────────────────────────


def design_path(brand_id: str) -> Path:
    return bg.graph_path(brand_id).parent / "design.json"  # graph_path checks the id


def load(brand_id: str) -> dict:
    p = design_path(brand_id)
    if p.exists():
        return json.loads(p.read_text())
    return {"brand_id": brand_id, "nodes": [{"id": "design", "type": "Design", "name": "Design"}], "edges": [], "imports": []}


def save(design: dict) -> None:
    design["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    design_path(design["brand_id"]).write_text(json.dumps(design, indent=1, ensure_ascii=False))


def tokens(design: dict) -> dict:
    return {n["name"]: n["value"] for n in bg.nodes(design, "DesignToken") if n.get("value")}


def _upsert(design: dict, node: dict, rel: str) -> None:
    design["nodes"] = [n for n in design["nodes"] if n["id"] != node["id"]] + [node]
    design["edges"] = [e for e in design["edges"] if e["to"] != node["id"]] + [{"from": "design", "rel": rel, "to": node["id"]}]


COMPARE_KEY = {"DesignToken": "value", "StyleNote": "text", "Preference": "stance", "Component": "description",
               "Logo": "file", "BrandColor": "hex"}
# Saved into the company graph, not the design layer: the brand's logo and its accent colours.
GRAPH_TYPES = ("Logo", "BrandColor")
REL = {"DesignToken": "HAS_TOKEN", "StyleNote": "HAS_NOTE", "Preference": "PREFERS", "Component": "HAS_COMPONENT"}


def node_id(type_: str, fields: dict) -> str:
    if type_ == "DesignToken":
        return f"token:{fields['name']}"
    if type_ == "StyleNote":
        return f"note:{re.sub(r'[^a-z0-9]+', '-', fields.get('topic', 'style').lower()).strip('-')}"
    if type_ == "Preference":
        return f"pref:{fields['pattern']}"
    if type_ == "Logo":
        return "logo:primary"
    if type_ == "BrandColor":
        return f"color:{fields['role']}"
    return f"component:{fields.get('slug') or uuid.uuid4().hex[:8]}"


# ── reading the website ───────────────────────────────────────────────────────

MEASURE_JS = r"""() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && r.top < 5000; };
  const px = (v) => parseFloat(v) || 0;
  const pick = (el, keys) => { const s = getComputedStyle(el); const o = {}; keys.forEach((k) => { o[k] = s[k]; });
    const r = el.getBoundingClientRect(); o.w = Math.round(r.width); o.h = Math.round(r.height);
    o.text = ((el.innerText || el.value || '') + '').trim().slice(0, 40); return o; };
  const inChrome = (el) => !!el.closest('[class*=cookie i],[id*=cookie i],[class*=consent i],[id*=onetrust i],[aria-modal=true]');
  const btnKeys = ['borderRadius', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'textTransform',
    'letterSpacing', 'fontWeight', 'fontSize', 'fontFamily', 'backgroundColor', 'color', 'borderTopWidth', 'borderTopColor'];
  const buttons = [...document.querySelectorAll('button, a, input[type=submit], [role=button]')].filter((el) => {
    if (!vis(el) || inChrome(el)) return false;
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el); const t = ((el.innerText || el.value || '') + '').trim();
    const filled = !/rgba\(0, 0, 0, 0\)|transparent/.test(s.backgroundColor); const bordered = px(s.borderTopWidth) >= 1;
    return t.length >= 2 && t.length <= 30 && r.width >= 60 && r.width <= 480 && r.height >= 28 && r.height <= 80 && (filled || bordered);
  }).slice(0, 80).map((el) => pick(el, btnKeys));
  const textKeys = ['fontSize', 'fontWeight', 'textTransform', 'letterSpacing', 'lineHeight', 'fontFamily', 'color'];
  const heads = {};
  ['h1', 'h2', 'h3'].forEach((t) => { heads[t] = [...document.querySelectorAll(t)].filter((el) => vis(el) && !inChrome(el)).slice(0, 25).map((el) => pick(el, textKeys)); });
  const body = [...document.querySelectorAll('p')].filter((el) => vis(el) && !inChrome(el) && (el.innerText || '').trim().length > 40).slice(0, 40).map((el) => pick(el, textKeys));
  const images = [...document.querySelectorAll('img')].filter((el) => vis(el) && el.getBoundingClientRect().width > 250).slice(0, 40).map((el) => pick(el, ['borderRadius', 'objectFit']));
  const sections = [...document.querySelectorAll('main section, main > div, section, [class*=section]')]
    .filter((el) => vis(el) && el.getBoundingClientRect().width > innerWidth * 0.9 && el.getBoundingClientRect().height > 120)
    .slice(0, 60).map((el) => pick(el, ['paddingTop', 'paddingBottom', 'backgroundColor']));
  return { buttons, heads, body, images, sections, pageBg: getComputedStyle(document.body).backgroundColor };
}"""


HIDE_POPUPS_JS = r"""() => {
  const area = innerWidth * innerHeight;
  for (const el of document.querySelectorAll('[role=dialog], [aria-modal=true], [class*=modal i], [class*=popup i], [id*=onetrust i], [class*=cookie i], [id*=cookie i], body *')) {
    const s = getComputedStyle(el);
    if (!['fixed', 'sticky'].includes(s.position) && !el.matches('[role=dialog], [aria-modal=true]')) continue;
    const r = el.getBoundingClientRect();
    const blocking = el.matches('[role=dialog], [aria-modal=true]') || (s.position === 'fixed' && r.width * r.height > area * 0.25);
    const banner = s.position === 'fixed' && /cookie|consent|onetrust/i.test(el.id + ' ' + el.className);
    if (blocking || banner) el.style.setProperty('display', 'none', 'important');
  }
  document.documentElement.style.overflow = 'auto';
  document.body.style.overflow = 'auto';
}"""


async def _measure_pages(urls: list[str], log) -> tuple[list[dict], Optional[bytes]]:
    """Computed styles from each page, and a screenshot of the first one."""
    from playwright.async_api import async_playwright

    out, shot = [], None
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            for url in urls:
                page = await browser.new_page(user_agent=bg.USER_AGENT, viewport={"width": 1280, "height": 900})
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(2500)
                    await page.keyboard.press("Escape")
                    await page.evaluate(HIDE_POPUPS_JS)  # newsletter pop-ups and cookie banners aren't the brand's design
                    out.append({"url": url, **await page.evaluate(MEASURE_JS)})
                    if shot is None:
                        shot = await page.screenshot(clip={"x": 0, "y": 0, "width": 1280, "height": 2400}, full_page=True)
                    log(f"Measured {url[:90]}")
                except Exception as exc:
                    log(f"  skipped {url[:80]}: {str(exc).splitlines()[0][:100]}")
                finally:
                    await page.close()
        finally:
            await browser.close()
    return out, shot


def _rgb_hex(v: str) -> Optional[str]:
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)", v or "")
    if not m or (m.group(4) is not None and float(m.group(4)) < 0.5):
        return None
    return "#" + "".join(f"{int(m.group(i)):02X}" for i in (1, 2, 3))


def _px(v: str) -> float:
    try:
        return float(re.sub(r"px$", "", str(v)))
    except ValueError:
        return 0.0


def _clamp(v: float, lo: float, hi: float) -> int:
    return int(round(max(lo, min(hi, v))))


def _mode(values: list, default=None) -> tuple:
    """(most common value, its share, how many there were)."""
    values = [v for v in values if v not in (None, "")]
    if not values:
        return default, 0.0, 0
    value, n = Counter(values).most_common(1)[0]
    return value, n / len(values), len(values)


def _site_tokens(pages: list[dict]) -> dict[str, dict]:
    """Email-sized design tokens from the measured styles, each with its evidence."""
    btns = [b for p in pages for b in p["buttons"]]
    found: dict[str, dict] = {}

    def put(name: str, value, evidence: str, share: float, n: int = 3) -> None:
        if value not in (None, ""):
            found[name] = {"value": str(value), "evidence": evidence, "confidence": round(share, 2) if n >= 3 else 0.0, "seen": n}

    if btns:
        # The primary button style is the most common filled one; outline buttons only if nothing is filled.
        filled = [b for b in btns if _rgb_hex(b["backgroundColor"])] or btns
        def radius(b):
            first = b["borderRadius"].split()[0]
            if first.endswith("%"):
                return "pill" if _px(first[:-1]) >= 50 else "0px"
            r = _px(first)
            return "pill" if r >= b["h"] / 2 - 1 else f"{int(r)}px"
        r, share, n = _mode([radius(b) for b in filled])
        put("radius", "24px" if r == "pill" else r, f"{n} buttons on the site; {share:.0%} have {'fully rounded (pill)' if r == 'pill' else r + ' corners'}", share, n)
        # At least 12px top and bottom, so an email button stays a comfortable tap target (~44px tall).
        pad, share, n = _mode([(_clamp(_px(b["paddingTop"]), 12, 20), _clamp(_px(b["paddingRight"]), 18, 48)) for b in filled])
        if pad:
            put("button_padding", f"{pad[0]}px {pad[1]}px", f"site buttons padded about {pad[0]}px × {pad[1]}px ({share:.0%} of {n})", share, n)
        case, share, n = _mode([b["textTransform"] for b in filled])
        put("button_case", "uppercase" if case == "uppercase" else "none", f"button labels are {case} on {share:.0%} of {n} buttons", share, n)
        ls, share, n = _mode(["0px" if b["letterSpacing"] == "normal" else f"{round(_px(b['letterSpacing']), 1):g}px" for b in filled])
        put("button_letter_spacing", ls, f"button letter-spacing {ls} ({share:.0%} of {n})", share, n)
        w, share, n = _mode([b["fontWeight"] for b in filled])
        put("button_weight", w, f"button labels weight {w} ({share:.0%} of {n})", share, n)

    heads = {t: [h for p in pages for h in p["heads"].get(t, [])] for t in ("h1", "h2", "h3")}
    body = [b for p in pages for b in p["body"]]
    limits = {"h1": (26, 40), "h2": (20, 30), "h3": (16, 22)}
    for t, (lo, hi) in limits.items():
        size, share, n = _mode([round(_px(h["fontSize"])) for h in heads[t]])
        if size:
            email = _clamp(size, lo, hi)
            put(f"{t}_size", f"{email}px", f"{t} is {size}px on the site ({n} seen)" + (f"; {email}px for email" if email != size else ""), share)
    hs = heads["h1"] + heads["h2"]
    if hs:
        case, share, n = _mode([h["textTransform"] for h in hs])
        put("heading_case", "uppercase" if case == "uppercase" else "none", f"headings are {case} ({share:.0%} of {n})", share, n)
        ls, share, n = _mode(["0px" if h["letterSpacing"] == "normal" else f"{round(_px(h['letterSpacing']), 1):g}px" for h in hs])
        put("heading_letter_spacing", ls, f"heading letter-spacing {ls} ({share:.0%} of {n})", share, n)
        w, share, n = _mode([h["fontWeight"] for h in hs])
        put("heading_weight", w, f"heading weight {w} ({share:.0%} of {n})", share, n)
    if body:
        size, share, n = _mode([round(_px(b["fontSize"])) for b in body])
        put("body_size", f"{_clamp(size, 14, 18)}px", f"body text {size}px ({n} paragraphs)", share, n)
        lh, share, n = _mode([round(_px(b["lineHeight"]) / max(_px(b["fontSize"]), 1), 1) for b in body if b["lineHeight"] != "normal"])
        if lh:
            put("line_height", f"{min(max(lh, 1.3), 1.8):g}", f"body line-height {lh:g} ({n} paragraphs)", share, n)
    imgs = [i for p in pages for i in p["images"]]
    if imgs:
        r, share, n = _mode([f"{int(_px(i['borderRadius'].split()[0]))}px" for i in imgs])
        put("image_radius", r, f"{n} large images; {share:.0%} have {r} corners", share, n)
    secs = [s for p in pages for s in p["sections"]]
    if secs:
        pad, share, n = _mode([_clamp(_px(s["paddingTop"]), 16, 48) for s in secs if _px(s["paddingTop"]) >= 8])
        if pad:
            put("section_padding", f"{pad}px", f"sections padded about {pad}px top ({n} seen)", share, n)
        page_bg = _rgb_hex(pages[0].get("pageBg", "")) or "#FFFFFF"
        tints = [h for h in (_rgb_hex(s["backgroundColor"]) for s in secs) if h and h != page_bg]
        tint, share, n = _mode(tints)
        if tint and n >= 2:
            put("surface", tint, f"{n} page sections use a {tint} background band", share, n)
    return found


SITE_SYSTEM = """You describe a brand's visual style for an email designer from a screenshot of its website and measured
styles. Describe only what you can see. Answer with JSON only."""


def _site_prompt(found: dict, urls: list[str]) -> str:
    return f"""Pages measured: {json.dumps(urls)}
Design tokens measured from the pages' real elements (value, evidence, share of elements agreeing):
{json.dumps(found, indent=1)}

The first page's screenshot is attached.

1. Write 3 to 6 short style notes an email designer should follow, each on one topic from {list(NOTE_TOPICS)}:
   e.g. photography ("full-bleed lifestyle photos of people outdoors; products shot on plain warm-grey backgrounds"),
   layout ("generous white space; one idea per section"), alignment ("headlines left-aligned"), decoration ("no borders,
   no shadows, no icons"). Only what the screenshot clearly shows.
2. List measured tokens that look wrong for the brand's main style (e.g. measured from a cookie banner or a promo
   strip rather than the main buttons), with a reason.

Return {{"notes": [{{"topic": "...", "text": "..."}}], "doubtful": [{{"token": "name", "reason": "..."}}]}}"""


def _site_urls(brand: dict) -> list[str]:
    urls = [bg.link(brand, "website") or f"https://{brand['domain']}/"]
    prods = [n.get("url") for n in bg.nodes(brand, "Product") if n.get("url")]
    cols = [n.get("url") for n in bg.nodes(brand, "Collection") if n.get("url")]
    return urls + prods[:1] + cols[:1]


async def propose_from_website(brand_id: str, log: Callable[[str], None] = print) -> dict:
    brand = bg.load(brand_id)
    urls = _site_urls(brand)
    log("Opening the brand's pages in a browser")
    pages, shot = await _measure_pages(urls, log)
    if not pages:
        raise LookupError("couldn't open any of the brand's pages")
    found = _site_tokens(pages)
    log(f"Measured {len(found)} design tokens; reading the look from a screenshot")
    content = [{"type": "text", "text": _site_prompt(found, [p["url"] for p in pages])}]
    if shot:
        import base64
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(shot))
        img.thumbnail((900, 1700))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=80)
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}})
    out = await bg._ask_model(SITE_SYSTEM, content, max_tokens=2000)
    doubtful = {d.get("token"): d.get("reason", "") for d in out.get("doubtful") or [] if isinstance(d, dict)}
    source = pages[0]["url"]
    changes = [_token_change(name, t["value"], "website", source, t["evidence"], doubt=doubtful.get(name) or
                             (f"only seen {t['seen']} time{'s' if t['seen'] != 1 else ''}" if t["seen"] < 3 else f"only {t['confidence']:.0%} of elements agree" if t["confidence"] < 0.4 else ""))
               for name, t in found.items() if name in dg.TOKENS]
    changes += [_note_change(n["topic"], n["text"], "website", source, "seen in the homepage screenshot")
                for n in out.get("notes") or [] if isinstance(n, dict) and n.get("text")]
    return _proposal(brand_id, "website", f"Design read from {len(pages)} page{'s' if len(pages) != 1 else ''} of {brand['domain']}",
                     changes, [p["url"] for p in pages])


# ── reading the brand guide ───────────────────────────────────────────────────

GUIDE_SYSTEM = """You read a company's brand guidelines and pull out what an email designer needs about layout, buttons,
type and imagery. Use only what the guide states. Answer with JSON only."""


async def propose_from_guide(brand_id: str, log: Callable[[str], None] = print) -> dict:
    pdf = bg.graph_path(brand_id).parent / "brand-guide.pdf"
    if not pdf.exists():
        raise LookupError("upload the brand guide first (on the brand's graph page)")
    text = bg._pdf_text(pdf)
    if len(text.strip()) < 200:
        raise LookupError("couldn't read text from the brand guide (is it scanned images only?)")
    log("Reading the brand guide for design rules")
    names = {k: v for k, v in dg.TOKENS.items() if k not in ("background", "text", "accent", "accent_text", "font")}
    out = await bg._ask_model(GUIDE_SYSTEM, [{"type": "text", "text": f"""Brand guidelines text:
{text[:40000]}

Design tokens an email designer uses (name: generic default): {json.dumps(names)}
Give a value only for tokens the guide states (e.g. "buttons have square corners" → radius 0px; "headlines in all caps"
→ heading_case uppercase). Sizes in px. Colors and the brand font are handled elsewhere; skip them.

Also give 2 to 6 style notes the guide states about photography, layout, alignment, decoration or buttons.

Every item needs "quote": a short exact quote from the guide that states it.
Return {{"tokens": [{{"name": "...", "value": "...", "quote": "..."}}], "notes": [{{"topic": "...", "text": "...", "quote": "..."}}]}}"""}],
                                max_tokens=3000)
    norm = lambda s: re.sub(r"\s+", " ", s or "").strip().lower()  # noqa: E731
    flat = norm(text)
    changes = []
    for t in out.get("tokens") or []:
        if isinstance(t, dict) and t.get("name") in names and t.get("value"):
            ok = len(norm(t.get("quote"))) >= 6 and norm(t.get("quote")) in flat
            changes.append(_token_change(t["name"], t["value"], "guide", "brand-guide.pdf", f"“{t.get('quote', '')}”",
                                         doubt="" if ok else "this quote wasn't found in the guide's text"))
    for n in out.get("notes") or []:
        if isinstance(n, dict) and n.get("text"):
            ok = len(norm(n.get("quote"))) >= 6 and norm(n.get("quote")) in flat
            ch = _note_change(n.get("topic", "style"), n["text"], "guide", "brand-guide.pdf", f"“{n.get('quote', '')}”")
            if not ok:
                ch.update(checked=False, note="this quote wasn't found in the guide's text")
            changes.append(ch)
    return _proposal(brand_id, "guide", "Design rules read from the brand guide", changes, ["brand-guide.pdf"])


# ── proposals: review, then save ──────────────────────────────────────────────


def _token_change(name: str, value: str, origin: str, source: str, evidence: str, doubt: str = "") -> dict:
    return {"op": "add", "type": "DesignToken", "fields": {"name": name, "value": str(value)}, "origin": origin,
            "source_url": source, "evidence": evidence, "doubt": doubt}


def _note_change(topic: str, text: str, origin: str, source: str, evidence: str) -> dict:
    topic = topic if topic in NOTE_TOPICS else "layout"
    return {"op": "add", "type": "StyleNote", "fields": {"topic": topic, "text": text}, "origin": origin,
            "source_url": source, "evidence": evidence}


def _proposal(brand_id: str, origin: str, summary: str, changes: list[dict], pages: list[str]) -> dict:
    """Compare each change with what's saved, decide what starts ticked, and store the proposal."""
    import graph_modify

    design = load(brand_id)
    out = []
    for c in changes:
        nid = node_id(c["type"], c["fields"])
        cur = bg.node(design, nid)
        key = COMPARE_KEY[c["type"]]
        if cur and cur.get(key) == c["fields"][key]:
            continue  # nothing new
        stronger = cur and ORIGIN_RANK.get(cur.get("origin"), 0) > ORIGIN_RANK[origin]
        note = (f"Kept unticked: {ORIGIN_LABEL[cur['origin']]} says {cur.get(key)}, and it outranks {ORIGIN_LABEL[origin]}."
                if stronger else c.get("doubt") or "")
        current = {"type": c["type"], key: cur.get(key), "origin": cur.get("origin")} if cur else c.get("current")
        if c["type"] == "DesignToken":
            default = dg.TOKENS.get(c["fields"]["name"])
            if not cur and c["fields"]["value"] == default:
                note = note or "Same as the generic default."
        out.append({**{k: v for k, v in c.items() if k != "doubt"}, "op": "update" if current else "add", "node_id": nid,
                    "current": current, "verified": True, "checked": not note and not stronger, "note": note})
    proposal = {
        "id": uuid.uuid4().hex[:12], "graph": "design", "brand_id": brand_id, "product_id": None, "origin": origin,
        "request": f"Import design from {ORIGIN_LABEL[origin]}", "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": summary, "not_found": "", "pages": pages, "web_pages": [], "searched_web": True, "topics": [],
        "changes": out, "applied": None,
    }
    (graph_modify._proposals_dir(brand_id) / f"{proposal['id']}.json").write_text(json.dumps(proposal, indent=2))
    return proposal


def apply(brand_id: str, proposal_id: str, picks: list[dict]) -> dict:
    import graph_modify

    proposal = graph_modify.load_proposal(brand_id, proposal_id)
    if proposal.get("graph") != "design":
        raise ValueError("that proposal isn't a design import")
    if proposal.get("applied") is not None:
        raise ValueError("this import was already saved; run it again for a fresh one")
    design = load(brand_id)
    done = []
    for pick in picks:
        i = pick.get("index")
        if not isinstance(i, int) or not 0 <= i < len(proposal["changes"]):
            continue
        c = proposal["changes"][i]
        fields = {**c["fields"], **{k: str(v) for k, v in (pick.get("fields") or {}).items() if k in c["fields"]}}
        if c["type"] in GRAPH_TYPES:
            _apply_to_graph(brand_id, c, fields)
            done.append(i)
            continue
        changed = fields != c["fields"]
        node = {"id": node_id(c["type"], fields), "type": c["type"], **fields, "origin": "you" if changed else c["origin"],
                "source": c.get("source_url", ""), "evidence": "edited by you" if changed else c.get("evidence", "")}
        _upsert(design, node, REL[c["type"]])
        done.append(i)
    design.setdefault("imports", []).insert(0, {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "origin": proposal["origin"], "saved": len(done)})
    save(design)
    proposal["applied"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "done": done, "failed": []}
    (graph_modify._proposals_dir(brand_id) / f"{proposal_id}.json").write_text(json.dumps(proposal, indent=2))
    return proposal["applied"]


def _apply_to_graph(brand_id: str, c: dict, fields: dict) -> None:
    """A logo or accent colour from the brand's emails goes into its company graph (kept on rebuild)."""
    import asyncio

    import graph_edit
    from bootstrap import STORAGE_DIR

    source = f"from the brand's emails ({c.get('source_url', '')})"
    if c["type"] == "Logo":
        data = (STORAGE_DIR / fields["file"]).read_bytes()
        asyncio.run(graph_edit.upload_logo(brand_id, None, data, "logo-from-emails.png", source=source))
    else:
        graph_edit.add_node(brand_id, None, "Color", {"role": fields["role"], "hex": fields["hex"]}, source=source)


def set_token(brand_id: str, name: str, value: str) -> dict:
    if name not in dg.TOKENS:
        raise ValueError(f"unknown token {name!r}")
    if not str(value).strip() or len(str(value)) > 80:
        raise ValueError("give a value")
    design = load(brand_id)
    node = {"id": f"token:{name}", "type": "DesignToken", "name": name, "value": str(value).strip(), "origin": "you",
            "source": "", "evidence": "set by you"}
    _upsert(design, node, "HAS_TOKEN")
    save(design)
    return node


def edit_node(brand_id: str, nid: str, fields: dict) -> dict:
    design = load(brand_id)
    node = bg.node(design, nid)
    if not node or node["type"] == "Design":
        raise KeyError(nid)
    key = {"DesignToken": "value", "StyleNote": "text"}.get(node["type"])
    if not key or not str(fields.get(key, "")).strip():
        raise ValueError(f"give a {key or 'value'}")
    node.update({key: str(fields[key]).strip(), "origin": "you", "evidence": "edited by you"})
    save(design)
    return node


def delete_node(brand_id: str, nid: str) -> None:
    design = load(brand_id)
    if not bg.node(design, nid) or nid == "design":
        raise KeyError(nid)
    design["nodes"] = [n for n in design["nodes"] if n["id"] != nid]
    design["edges"] = [e for e in design["edges"] if nid not in (e["from"], e["to"])]
    save(design)


# ── what a run gets ───────────────────────────────────────────────────────────


def summary(brand_id: str) -> dict:
    d = load(brand_id)
    return {"tokens": len(bg.nodes(d, "DesignToken")), "notes": len(bg.nodes(d, "StyleNote")),
            "preferences": len(bg.nodes(d, "Preference")), "components": len(bg.nodes(d, "Component")),
            "imports": d.get("imports", [])[:5], "updated": d.get("updated")}


def bible_parts(brand_id: Optional[str], colors: dict, font_stack: str, template: str = "") -> dict:
    """The Design Bible sections the two design layers fill for one run.

    colors: the run's background/text/accent/accent_text (from the brand or product graph).
    """
    generic = dg.load()
    design = load(brand_id) if brand_id else {"nodes": [], "edges": []}
    t = {**dg.TOKENS, **{k: v for k, v in colors.items() if v}, "font": font_stack, "heading_font": font_stack, **tokens(design)}
    case = lambda v: "upper" if v == "uppercase" else None  # noqa: E731
    scale = [
        {"token": "h1", "size": t["h1_size"], "weight": t["heading_weight"], "case": case(t["heading_case"]),
         "letter_spacing": t["heading_letter_spacing"], "line_height": "1.2", "source": "inferred"},
        {"token": "h2", "size": t["h2_size"], "weight": t["heading_weight"], "case": case(t["heading_case"]),
         "letter_spacing": t["heading_letter_spacing"], "line_height": "1.25", "source": "inferred"},
        {"token": "h3", "size": t["h3_size"], "weight": t["heading_weight"], "line_height": "1.3", "source": "inferred"},
        {"token": "body", "size": t["body_size"], "line_height": t["line_height"], "source": "inferred"},
        {"token": "button", "size": t["body_size"], "weight": t["button_weight"], "case": case(t["button_case"]),
         "letter_spacing": t["button_letter_spacing"], "source": "inferred"},
        {"token": "legal", "size": t["small_size"], "line_height": "1.5", "source": "inferred"},
    ]
    scale = [{k: v for k, v in s.items() if v is not None} for s in scale]
    prefs = {n["pattern"]: n for n in bg.nodes(design, "Preference")}
    avoided = {p for p, n in prefs.items() if n.get("stance") == "avoids"}
    favoured = [p for p, n in sorted(prefs.items(), key=lambda kv: -int(kv[1].get("count", 0))) if n.get("stance") == "prefers"]
    patterns = sorted((p for p in generic.get("patterns", []) if p["id"] not in avoided),
                      key=lambda p: (p["id"] not in favoured, favoured.index(p["id"]) if p["id"] in favoured else 0))
    items = []
    for p in patterns:
        anatomy = list(p.get("anatomy") or [])
        if p["id"] in prefs:
            n = prefs[p["id"]]
            anatomy.insert(0, f"This brand uses this section in {n.get('count')} of {n.get('of')} of its emails.")
        items.append({
            "id": p["id"], "anatomy": anatomy,
            "spec": {**(p.get("spec") or {}), "when_to_use": p.get("when_to_use"), "avoid_when": p.get("avoid_when"),
                     "slots": p.get("slots")},
            "fixed_style": {k: t[k] for k in p.get("tokens", []) if k in t and k not in ("image",)},
            "mjml": dg.fill(p["mjml"], t), "source": "inferred",
        })
    for c in bg.nodes(design, "Component"):
        items.append({"id": f"brand-{c['id'].split(':', 1)[1]}", "anatomy": [c.get("description", "")],
                      "spec": {"based_on_pattern": c.get("pattern"), "from": "the brand's own past email"},
                      "fixed_style": c.get("style") or {}, "source": "inferred"})
    rules = [{"category": "design", "rule": f"{n['topic'].capitalize()}: {n['text']}", "source": "inferred"}
             for n in bg.nodes(design, "StyleNote")]
    recipe = next((r for r in generic.get("recipes", []) if r["id"] == template), None)
    if recipe:
        rules.append({"category": "layout", "source": "inferred",
                      "rule": f"A proven layout for this kind of email: {' → '.join(recipe['sequence'])} (component ids). "
                              f"{recipe.get('notes', '')} Adapt it to the brief."})
    if favoured:
        rules.append({"category": "layout", "source": "inferred",
                      "rule": "Sections this brand uses most in its own emails: " + ", ".join(favoured[:5]) + "."})
    if avoided:
        rules.append({"category": "layout", "source": "inferred",
                      "rule": "Section types this brand doesn't use in its emails: " + ", ".join(sorted(avoided)) + "."})
    rules.append({"category": "email-clients", "rule": dg.designer_rule(generic), "source": "explicit"})
    constraints: list[dict] = []
    return {
        "scale": scale,
        "spacing": {"section_padding_vertical": t["section_padding"], "side_gutter": t["gutter"]},
        "button_shape": {"radius": t["radius"], "padding": t["button_padding"], "text_transform": t["button_case"],
                         "letter_spacing": t["button_letter_spacing"], "font_weight": str(t["button_weight"])},
        "components": items,
        "constraints": constraints,
        "rules": rules,
        "surface": t.get("surface"),
    }
