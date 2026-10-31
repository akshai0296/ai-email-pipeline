"""The generic design layer: email section patterns, campaign layouts and email-client support.

Imported once from open-source projects (all MIT, pinned to a commit; see design/NOTICE.md) and
kept in the repo as local/design/generic.json:

    patterns     Cerberus's labelled sections and Foundation for Emails' template rows, merged into
                 one library by the model and rewritten as MJML with brand tokens ({{accent}}, {{radius}}...).
                 A pattern is kept only if it compiles under MJML's strict validation and fits a
                 375px phone screen without sideways scrolling.
    recipes      campaign layouts (launch, welcome, newsletter...) as ordered pattern ids, from
                 Foundation's template types and the pipeline's brief templates.
    constraints  which HTML/CSS features work in which email clients, from caniemail's data.

    .venv/bin/python local/design_generic.py patterns   # patterns + recipes (model calls, a few minutes)
    .venv/bin/python local/design_generic.py add "headline and text above a full-width image, with a button"
                                                         # one more pattern from the same sources
    .venv/bin/python local/design_generic.py clients    # constraints from caniemail (no model)

The brand layer (brand_design.py) fills the tokens; pipeline.build_design_bible turns both into
the Design Bible the generator reads.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

DESIGN_DIR = Path(__file__).resolve().parent / "design"
GENERIC_PATH = DESIGN_DIR / "generic.json"
THUMBS_DIR = DESIGN_DIR / "thumbs"
MJML_BIN = Path(__file__).resolve().parent.parent / "tools" / "node_modules" / ".bin" / "mjml"

SOURCES = {
    "cerberus": {
        "name": "Cerberus", "repo": "emailmonday/Cerberus", "commit": "af3cec9dd12fbbea0c6e9a1b0bf82a0479726957",
        "files": ["cerberus-hybrid.html"], "license": "MIT", "copyright": "Copyright (c) 2017 Ted Goas",
    },
    "foundation": {
        "name": "Foundation for Emails", "repo": "foundation/foundation-emails", "commit": "7a37ae4cad4f97068f8257ba92a67a261080e3c5",
        "files": [f"templates/{n}.html" for n in ("basic", "drip", "hero", "marketing", "newsletter", "newsletter-2",
                                                    "sidebar-hero", "sidebar", "welcome")],
        "license": "MIT", "copyright": "Copyright (c) 2017 Foundation, inc.",
    },
    "caniemail": {
        "name": "Can I email", "repo": "hteumeuleu/caniemail", "commit": "cb8109c3f8658e003aa064b47a71b232ddc28d39",
        "data_url": "https://www.caniemail.com/api/data.json", "license": "MIT", "copyright": "Copyright (c) 2019 Rémi Parmentier",
    },
}

# Every token a pattern may use, with the neutral value it gets when a brand has none.
TOKENS = {
    "background": "#FFFFFF", "surface": "#F4F4F4", "text": "#222222", "muted": "#666666",
    "accent": "#222222", "accent_text": "#FFFFFF",
    "font": "Helvetica, Arial, sans-serif", "heading_font": "Helvetica, Arial, sans-serif",
    "h1_size": "32px", "h2_size": "24px", "h3_size": "18px", "body_size": "16px", "small_size": "13px",
    "heading_weight": "700", "heading_case": "none", "heading_letter_spacing": "0px", "line_height": "1.5",
    "radius": "4px", "image_radius": "0px",
    "button_padding": "14px 28px", "button_case": "none", "button_letter_spacing": "0px", "button_weight": "700",
    "section_padding": "32px", "gutter": "24px",
}

# Cerberus sections that are plumbing, not content patterns.
SKIP = {"web font / @font-face", "css reset", "progressive enhancements", "visually hidden preheader text",
        "preview text spacing hack", "email body", "button"}


def raw_url(source: str, path: str) -> str:
    s = SOURCES[source]
    return f"https://raw.githubusercontent.com/{s['repo']}/{s['commit']}/{path}"


def load() -> dict:
    """The generic layer, or an empty one if it hasn't been built."""
    if not GENERIC_PATH.exists():
        return {"patterns": [], "recipes": [], "constraints": [], "sources": {}}
    return json.loads(GENERIC_PATH.read_text())


def fill(mjml: str, tokens: dict) -> str:
    """Put token values into a pattern's MJML ({{accent}} -> #000000); unknown tokens get the generic default."""
    values = {**TOKENS, **{k: v for k, v in tokens.items() if v}}
    return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", lambda m: str(values.get(m.group(1), m.group(0))), mjml)


# ── fetching and splitting the sources ────────────────────────────────────────


def _fetch(url: str) -> str:
    import httpx

    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _cerberus_sections(html: str, path: str) -> list[dict]:
    """Top-level <!-- Name : BEGIN --> ... <!-- Name : END --> blocks inside the email body."""
    out, depth, start, name = [], 0, None, None
    for m in re.finditer(r"<!--\s*(.+?)\s*:\s*(BEGIN|END)\s*-->", html):
        label, kind = m.group(1).strip(), m.group(2)
        if label.lower() in SKIP:
            continue
        if kind == "BEGIN":
            if depth == 0:
                start, name = m.end(), label
            depth += 1
        else:
            depth = max(0, depth - 1)
            if depth == 0 and name == label:
                code = html[start:m.start()].strip()
                out.append({"id": f"cerberus:{len(out)}", "source": "cerberus", "file": path, "label": label, "code": code})
    return out


def _foundation_sections(html: str, path: str) -> list[dict]:
    """The top-level rows of an Inky template's container(s), in order."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out = []
    for container in soup.find_all(["container", "wrapper"]) or [soup]:
        if container.find_parent(["container", "wrapper"]):
            continue
        for child in container.find_all(["row", "callout", "wrapper", "menu", "hr"], recursive=False):
            text = " ".join(child.get_text(" ", strip=True).split())
            out.append({"id": f"foundation:{Path(path).stem}:{len(out)}", "source": "foundation", "file": path,
                        "label": text[:60] or child.name, "code": str(child)[:4000]})
    return out


# ── the model steps ───────────────────────────────────────────────────────────


async def _ask(system: str, prompt: str, max_tokens: int = 16000) -> str:
    import anthropic  # the CLI backends swap this for their own client

    import brand_graph as bg

    msg = await anthropic.AsyncAnthropic().messages.create(
        model=bg.MODEL, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    )
    return "".join(getattr(b, "text", "") for b in msg.content)


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in: {text[:300]}")
    return json.loads(m.group(0))


LIBRARY_SYSTEM = """You are an email design systems engineer. You turn open-source email templates into a reusable library of
section patterns for marketing emails. Answer with JSON only."""


def _library_prompt(sections: list[dict]) -> str:
    listing = "\n\n".join(f"### {s['id']} ({s['file']}, labelled “{s['label']}”)\n{s['code'][:3500]}" for s in sections)
    return f"""Below are the sections of two open-source email template libraries: Cerberus (plain HTML; the author labelled each
section) and Foundation for Emails (Inky markup: <row>, <columns>, <callout>, <button>, <spacer>).

Merge them into ONE library of 12 to 16 distinct section patterns for marketing emails. Merge near-duplicates (a Cerberus
"2 Even Columns" and a Foundation two-column row are one pattern). Skip transactional-only sections (order tables, password
resets) and pure spacers. Include header (logo), footer and a call-to-action band.

For each pattern return:
{{"id": "kebab-case, e.g. hero-image-flush", "name": "short name", "category": "header|hero|content|product|social-proof|cta|footer",
  "from_sources": ["source section ids it was merged from"],
  "anatomy": ["the elements, top to bottom, e.g. 'full-width image (600px, 2:1)', 'headline', 'body text', 'button'"],
  "when_to_use": "one sentence", "avoid_when": "one sentence",
  "slots": [{{"name": "headline", "limit": "e.g. up to 8 words"}}],
  "spec": {{"columns": 1, "image_ratio": "2:1 or none", "alignment": "center|left", "stacks_on_mobile": true}}}}

Return {{"patterns": [...]}}.

{listing}"""


MJML_SYSTEM = """You write production MJML 4 for marketing emails. Output must pass `mjml --validate strict`, render in Outlook,
Gmail and Apple Mail, and stack cleanly on a 375px phone. Answer with the MJML only, between <mjml_sections> tags."""


def _mjml_prompt(pattern: dict, sources: list[dict], error: str = "") -> str:
    code = "\n\n".join(f"--- {s['id']} ---\n{s['code'][:3500]}" for s in sources)
    tokens = ", ".join("{{" + k + "}}" for k in TOKENS)
    fix = f"\n\nYour previous attempt failed: {error}\nFix that." if error else ""
    return f"""Write the "{pattern['name']}" section pattern as MJML.

Pattern: {json.dumps({k: pattern[k] for k in ('anatomy', 'spec', 'slots', 'when_to_use')})}

Source sections it is based on (open-source, MIT; follow their structure, not their text):
{code}

Rules:
- Output one or more <mj-section> (or <mj-wrapper>) elements only: no <mjml>, <mj-head> or <mj-body>.
- Every color, font, size, radius and padding is a token, never a literal: {tokens}.
  Use {{{{accent}}}} for buttons and links, {{{{accent_text}}}} for button labels, {{{{surface}}}} for tinted bands.
  Headings: inside <mj-text>, e.g. <h1 style="margin:0;font-family:{{{{heading_font}}}};font-size:{{{{h1_size}}}};
  font-weight:{{{{heading_weight}}}};text-transform:{{{{heading_case}}}};letter-spacing:{{{{heading_letter_spacing}}}}">.
  Buttons also get text-transform="{{{{button_case}}}}" letter-spacing="{{{{button_letter_spacing}}}}" font-weight="{{{{button_weight}}}}".
- Images: src="{{{{image}}}}" (a placeholder), a meaningful alt="" describing the slot. mj-image width is in px only
  (e.g. width="600px"; never a percentage or a token), and mj-image has no text attributes.
- Copy: short placeholder text in [brackets] describing the slot, e.g. [Headline, up to 8 words].
- Buttons: <mj-button> with href="#" and border-radius="{{{{radius}}}}", inner-padding="{{{{button_padding}}}}".
- Columns must stack on mobile (MJML does this by default; don't disable it).{fix}"""


def _compile(mjml_sections: str, tokens: Optional[dict] = None) -> tuple[bool, str]:
    """Compile with sample values under strict validation. Returns (ok, html or error)."""
    sample = fill(mjml_sections, {**(tokens or {}), "image": _sample_image()})
    doc = (f'<mjml><mj-head><mj-attributes><mj-all font-family="{TOKENS["font"]}" /></mj-attributes></mj-head>'
           f'<mj-body width="600px" background-color="{TOKENS["background"]}">{sample}</mj-body></mjml>')
    p = subprocess.run([str(MJML_BIN), "-i", "-s", "--config.validationLevel=strict"], input=doc.encode(),
                       capture_output=True, timeout=60)
    if p.returncode != 0:
        return False, (p.stderr.decode(errors="replace") or p.stdout.decode(errors="replace"))[-800:]
    return True, p.stdout.decode()


_SAMPLE: dict = {}


def _sample_image() -> str:
    if "img" not in _SAMPLE:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (1200, 600), "#C9CDD2")
        d = ImageDraw.Draw(img)
        d.line([(0, 0), (1200, 600)], fill="#B0B5BB", width=6)
        d.line([(0, 600), (1200, 0)], fill="#B0B5BB", width=6)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        _SAMPLE["img"] = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return _SAMPLE["img"]


async def _render_checks(html: str, thumb: Path) -> Optional[str]:
    """Why the compiled pattern fails on screen, or None. Saves a thumbnail of the 600px render."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 375, "height": 800})
            await page.set_content(html, wait_until="load")
            overflow = await page.evaluate("document.documentElement.scrollWidth")
            if overflow > 380:
                return f"on a 375px phone the section is {overflow}px wide (it must stack to fit)"
            page = await browser.new_page(viewport={"width": 640, "height": 400})
            await page.set_content(html, wait_until="load")
            height = await page.evaluate("document.body.scrollHeight")
            if height < 30:
                return "it renders empty"
            thumb.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(thumb), full_page=True)
        finally:
            await browser.close()
    from PIL import Image

    img = Image.open(thumb)
    img.thumbnail((320, 1200))
    img.save(thumb)
    return None


async def _build_pattern(pattern: dict, by_id: dict, sem: asyncio.Semaphore, log) -> Optional[dict]:
    sources = [by_id[i] for i in pattern.get("from_sources", []) if i in by_id][:4]
    error = ""
    async with sem:
        for attempt in range(3):
            text = await _ask(MJML_SYSTEM, _mjml_prompt(pattern, sources, error), max_tokens=8000)
            m = re.search(r"<mjml_sections>(.*?)</mjml_sections>", text, re.S)
            mjml = (m.group(1) if m else text).strip()
            mjml = re.sub(r"^```\w*|```$", "", mjml).strip()
            ok, out = _compile(mjml)
            if not ok:
                error = f"MJML didn't compile: {out}"
            else:
                error = await _render_checks(out, THUMBS_DIR / f"{pattern['id']}.png") or ""
            if not error:
                log(f"  ✓ {pattern['id']}" + (f" (attempt {attempt + 1})" if attempt else ""))
                used = sorted(set(re.findall(r"\{\{\s*([a-z_]+)\s*\}\}", mjml)) - {"image"})
                return {**pattern, "mjml": mjml, "tokens": used, "thumbnail": f"thumbs/{pattern['id']}.png",
                        "sources": [{"source": by_id[i]["source"], "file": by_id[i]["file"], "section": by_id[i]["label"]}
                                    for i in pattern.get("from_sources", []) if i in by_id]}
            log(f"  {pattern['id']}: {error[:140]}")
    log(f"  ✗ {pattern['id']} dropped after 3 attempts")
    return None


RECIPE_SYSTEM = "You plan marketing email layouts from a library of section patterns. Answer with JSON only."


def _recipe_prompt(patterns: list[dict], foundation: dict[str, list[str]], briefs: dict[str, tuple[str, str]]) -> str:
    lib = [{"id": p["id"], "name": p["name"], "category": p["category"], "when_to_use": p["when_to_use"]} for p in patterns]
    return f"""Pattern library: {json.dumps(lib)}

Foundation for Emails template types and their rows (as source section labels): {json.dumps(foundation)}

The pipeline's campaign templates (id: name, layout): {json.dumps({k: list(v) for k, v in briefs.items()})}

Make one recipe per campaign template above, plus "newsletter" and "announcement". Each recipe is an ordered list of
pattern ids from the library (header first, footer last), with optional extras.
Return {{"recipes": [{{"id": "<campaign template id>", "name": "...", "sequence": ["pattern ids"], "optional": ["pattern ids"],
"notes": "one sentence on why this order works"}}]}}. Use only ids from the library."""


def _source_sections() -> list[dict]:
    sections: list[dict] = []
    for name in ("cerberus", "foundation"):
        for path in SOURCES[name]["files"]:
            html = _fetch(raw_url(name, path))
            sections += _cerberus_sections(html, path) if name == "cerberus" else _foundation_sections(html, path)
    return sections


async def add_pattern(description: str, log=print) -> Optional[dict]:
    """Add one pattern, described in words, built from the same open-source sections."""
    sections = _source_sections()
    by_id = {s["id"]: s for s in sections}
    generic = load()
    have = [p["id"] for p in generic["patterns"]]
    prompt = _library_prompt(sections).replace(
        "Merge them into ONE library of 12 to 16 distinct section patterns for marketing emails.",
        f"Make ONE new pattern: {description}. The library already has {have}; don't repeat one of those.",
    ).replace('Return {"patterns": [...]}.', 'Return {"patterns": [<the one pattern>]}.')
    pattern = _json(await _ask(LIBRARY_SYSTEM, prompt))["patterns"][0]
    if pattern["id"] in have:
        pattern["id"] += "-2"
    built = await _build_pattern(pattern, by_id, asyncio.Semaphore(1), log)
    if built:
        generic["patterns"].append(built)
        # Offer it in every recipe that already uses a pattern of its kind (another hero, another footer...).
        same = {p["id"] for p in generic["patterns"] if p.get("category") == built.get("category") and p["id"] != built["id"]}
        for r in generic["recipes"]:
            if same & set(r["sequence"]) and built["id"] not in r["optional"]:
                r["optional"].append(built["id"])
        _save(generic)
    return built


async def build_patterns(log=print) -> dict:
    import brief_writer

    log("Fetching Cerberus and Foundation for Emails (pinned commits)")
    sections = _source_sections()
    by_id = {s["id"]: s for s in sections}
    log(f"{len(sections)} source sections; merging them into a pattern library")
    library = _json(await _ask(LIBRARY_SYSTEM, _library_prompt(sections)))["patterns"]
    log(f"{len(library)} patterns proposed; writing and testing MJML for each")
    sem = asyncio.Semaphore(3)
    built = [p for p in await asyncio.gather(*[_build_pattern(p, by_id, sem, log) for p in library]) if p]
    foundation_rows = {}
    for s in sections:
        if s["source"] == "foundation":
            foundation_rows.setdefault(Path(s["file"]).stem, []).append(s["label"])
    log("Planning campaign recipes")
    recipes = _json(await _ask(RECIPE_SYSTEM, _recipe_prompt(built, foundation_rows, brief_writer.TEMPLATES), 6000))["recipes"]
    ids = {p["id"] for p in built}
    for r in recipes:
        r["sequence"] = [i for i in r.get("sequence", []) if i in ids]
        r["optional"] = [i for i in r.get("optional", []) if i in ids]
    generic = load()
    generic.update(patterns=built, recipes=[r for r in recipes if r["sequence"]], tokens=TOKENS,
                   built=time.strftime("%Y-%m-%dT%H:%M:%S"))
    _save(generic)
    return generic


# ── email-client support (caniemail) ──────────────────────────────────────────

# The clients most subscribers read on, and the weight each gets.
KEY_CLIENTS = {
    ("apple-mail", "ios"): "Apple Mail iPhone", ("apple-mail", "macos"): "Apple Mail Mac",
    ("gmail", "desktop-webmail"): "Gmail web", ("gmail", "ios"): "Gmail iPhone", ("gmail", "android"): "Gmail Android",
    ("outlook", "windows"): "Outlook Windows", ("outlook", "outlook-com"): "Outlook.com", ("outlook", "macos"): "Outlook Mac",
    ("outlook", "ios"): "Outlook iPhone", ("outlook", "android"): "Outlook Android",
    ("yahoo", "desktop-webmail"): "Yahoo web", ("samsung-email", "android"): "Samsung Email",
}


# Detectors for features whose caniemail title can't be turned into one automatically.
EXTRA_DETECT = {
    "css-gap": r"(?<![a-z-])(row-|column-)?gap\s*:", "css-variables": r"var\(--|(?<![a-z-])--[a-z][\w-]*\s*:",
    "html-svg": r"<svg\b", "css-pseudo-class-hover": r":hover\b", "css-at-font-face": r"@font-face",
    "css-at-media": r"@media\b", "image-webp": r"\.webp\b", "image-svg": r"src=[\"'][^\"']+\.svg\b",
}

# What a designer reaches for on the web, and the email-safe way instead. These become the
# designer's constraints, with the clients that don't support each one taken from the data.
DESIGN_RELEVANT = {
    "css-display-flex": "lay out side by side with MJML columns (mj-column / mj-group)",
    "css-display-grid": "lay out with MJML sections and columns",
    "css-gap": "space columns with padding",
    "css-position": "don't overlap elements; put text on an image with a section background image",
    "css-transform": "use images that are already rotated or scaled",
    "css-box-shadow": "use borders or background tints, not shadows",
    "css-text-shadow": "make text legible with contrast, not a shadow",
    "css-linear-gradient": "use a solid background color (a gradient may only be an extra on top of it)",
    "css-background-image": "always set a solid background color under it, legible on its own",
    "css-animation": "use a static image or an animated GIF",
    "css-transition": "leave it out",
    "css-filter": "edit the image itself",
    "css-aspect-ratio": "set image width and let the height follow",
    "css-object-fit": "crop images to the right shape before using them",
    "css-variables": "write the values out",
    "css-function-clamp": "use fixed px sizes",
    "html-svg": "use PNG or JPG images",
    "html-video": "use a still image (or GIF) that links to the video",
    "css-pseudo-class-hover": "hover styles may only be extras; the email must work without them",
    "css-at-font-face": "a web font is an extra; always give the email-safe font stack",
    "image-webp": "use PNG or JPG images",
}


def designer_constraints(generic: Optional[dict] = None) -> list[dict]:
    """Constraint rules for the Design Bible: each feature, who doesn't support it, and what to do instead."""
    by_id = {c["id"]: c for c in (generic or load()).get("constraints", [])}
    out = []
    for slug, instead in DESIGN_RELEVANT.items():
        c = by_id.get(slug)
        if not c or c["level"] == "safe":
            continue
        missing = c["unsupported"] or c["partial"]
        who = ", ".join(missing[:4]) + (f" and {len(missing) - 4} more" if len(missing) > 4 else "")
        out.append({"rule": f"Don't rely on {c['title']} ({'not supported' if c['unsupported'] else 'only partly supported'} in {who}): {instead}.",
                    "id": slug, "level": c["level"], "url": c["url"]})
    return out


def designer_rule(generic: Optional[dict] = None) -> str:
    """The client constraints as one compact rule, grouped by what to do instead."""
    by_id = {c["id"]: c for c in (generic or load()).get("constraints", [])}
    groups: dict[str, list[str]] = {}
    for slug, instead in DESIGN_RELEVANT.items():
        c = by_id.get(slug)
        if not c or c["level"] == "safe":
            continue
        who = c["unsupported"] or c["partial"]
        label = re.sub(r"\s*\((Custom Properties)\)|^Embedded |\s*(image format|element)$", "", c["title"])
        groups.setdefault(instead, []).append(f"{label} ({who[0]}{' +' + str(len(who) - 1) if len(who) > 1 else ''})")
    return ("Email apps ignore some web code (caniemail data; in brackets, the main app that doesn't support it). "
            + " ".join(f"{', '.join(feats)}: {instead}." for instead, feats in groups.items()))


def _latest(results: dict) -> str:
    """The newest test result for one client and platform: y, a (partial), n or u (unknown)."""
    if not results:
        return "u"
    key = sorted(results, key=lambda v: [int(x) if x.isdigit() else x for x in re.split(r"[.\-]", v)])[-1]
    return (results[key] or "u").split()[0][:1]


def _detector(feature: dict) -> Optional[str]:
    """A regex that finds the feature in email HTML, when that can be done reliably from its title."""
    title = feature["title"].strip()
    if feature["category"] == "css":
        m = re.fullmatch(r"([a-z-]+)\s*:\s*([a-z-]+)", title)
        if m:
            return rf"{re.escape(m.group(1))}\s*:\s*{re.escape(m.group(2))}\b"
        m = re.fullmatch(r"([a-z-]+)(\(\))?", title)
        if m:
            return rf"{re.escape(m.group(1))}\(" if m.group(2) else rf"(?<![a-z-]){re.escape(m.group(1))}\s*:"
    if feature["category"] == "html":
        m = re.fullmatch(r"<([a-z0-9]+)>(?: element)?", title)
        if m:
            return rf"<{m.group(1)}\b"
    return None


def build_constraints(log=print) -> list[dict]:
    import httpx

    data = httpx.get(SOURCES["caniemail"]["data_url"], timeout=60).json()
    out = []
    for f in data["data"]:
        if f["category"] not in ("css", "html", "image"):
            continue
        results = {name: _latest((f.get("stats") or {}).get(c, {}).get(p, {})) for (c, p), name in KEY_CLIENTS.items()}
        known = {k: v for k, v in results.items() if v in "yan"}
        if len(known) < 6:
            continue
        support = sum(1 if v == "y" else 0.5 if v == "a" else 0 for v in known.values()) / len(known)
        level = "safe" if support >= 0.95 else "partial" if support >= 0.6 else "avoid"
        out.append({
            "id": f["slug"], "title": f["title"], "category": f["category"], "level": level, "support": round(support, 2),
            "unsupported": sorted(k for k, v in known.items() if v == "n"),
            "partial": sorted(k for k, v in known.items() if v == "a"),
            "detect": EXTRA_DETECT.get(f["slug"]) or _detector(f), "url": f["url"], "tested": f.get("last_test_date"),
        })
    generic = load()
    generic.update(constraints=out, constraints_updated=data.get("last_update_date"))
    # What MJML itself writes (with its own fallbacks) isn't the designer's doing: the client check skips it.
    detectable = [c for c in out if c["detect"]]
    baseline: set[str] = set()
    for p in generic.get("patterns", []):
        ok, html = _compile(p["mjml"])
        if ok:
            baseline |= {c["id"] for c in detectable if re.search(c["detect"], html, re.I)}
    generic["mjml_baseline"] = sorted(baseline)
    _save(generic)
    counts = {lvl: sum(c["level"] == lvl for c in out) for lvl in ("safe", "partial", "avoid")}
    log(f"{len(out)} features: {counts}")
    return out


def _save(generic: dict) -> None:
    DESIGN_DIR.mkdir(parents=True, exist_ok=True)
    generic["sources"] = {k: {kk: vv for kk, vv in v.items() if kk != "files"} for k, v in SOURCES.items()}
    GENERIC_PATH.write_text(json.dumps(generic, indent=1, ensure_ascii=False))
    notice = ["# Third-party sources in the generic design layer", "",
              "Section patterns and campaign recipes in generic.json are adapted from these MIT-licensed projects;",
              "email-client support data comes from caniemail. Each is used under the MIT License:", ""]
    for s in SOURCES.values():
        notice.append(f"- **{s['name']}** — https://github.com/{s['repo']} at `{s['commit'][:12]}` — {s['copyright']}. MIT License.")
    notice += ["", "MIT License: Permission is hereby granted, free of charge, to any person obtaining a copy of this software and "
               "associated documentation files (the \"Software\"), to deal in the Software without restriction, including without "
               "limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, "
               "and to permit persons to whom the Software is furnished to do so, subject to the following conditions: The above "
               "copyright notice and this permission notice shall be included in all copies or substantial portions of the Software. "
               "THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED."]
    (DESIGN_DIR / "NOTICE.md").write_text("\n".join(notice) + "\n")


if __name__ == "__main__":
    import bootstrap  # noqa: F401  (model backend and egress guard)

    bootstrap.ALLOWED_HOSTS.update({"raw.githubusercontent.com", "www.caniemail.com"})
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "patterns":
        g = asyncio.run(build_patterns())
        print(f"{len(g['patterns'])} patterns, {len(g['recipes'])} recipes → {GENERIC_PATH}")
    elif cmd == "add" and len(sys.argv) > 2:
        p = asyncio.run(add_pattern(sys.argv[2]))
        print(f"added {p['id']}" if p else "couldn't build that pattern")
    elif cmd == "clients":
        build_constraints()
    else:
        print(__doc__)
