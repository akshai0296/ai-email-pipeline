"""Company knowledge graphs: search a brand once, reuse it in every email.

    "Allbirds"  →  resolve the official website
               →  fetch it, pull logo candidates, colors, fonts, links, footer facts
               →  a model picks the real logo and fills in name, palette roles, voice
               →  storage/brands/<id>/graph.json  (+ the logo files next to it)

A saved graph is found again by name, domain or any earlier search for it, so
the next run reuses it instead of rebuilding. ``email_inputs`` turns a graph
into what the email pipeline takes: a BrandSpec, the logo image, and brand rules
carrying the website, social links and footer details.

Graph shape (graph.json):
    nodes: {id, type, ..., source}   types: Company, Logo, Color, Font, Link,
                                      SocialProfile, Address, Product, Voice, Rule
    edges: {from, rel, to}           e.g. company -HAS_LOGO-> logo:primary
Every node records the page it came from.

Building runs in brand_worker.py, a separate process, because it has to open
the company's own website, which the local egress guard otherwise blocks.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urljoin, urlparse

from bootstrap import STORAGE_DIR, LocalStorageAdapter

BRANDS_DIR = STORAGE_DIR / "brands"
MODEL = os.environ.get("BRAND_GRAPH_MODEL", "claude-sonnet-4-6")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
SOCIAL_HOSTS = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "x.com": "x",
    "twitter.com": "x",
    "linkedin.com": "linkedin",
    "youtube.com": "youtube",
    "tiktok.com": "tiktok",
    "pinterest.com": "pinterest",
    "threads.net": "threads",
}
EMAIL_SAFE_FALLBACK = "Helvetica, Arial, sans-serif"
FOOTER_NETWORKS = ("instagram", "facebook", "x")  # the only social links an email footer shows, in this order


# ── store ─────────────────────────────────────────────────────────────────────


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _domain_of(text: str) -> Optional[str]:
    """'https://www.Allbirds.com/about' → 'allbirds.com'; None when text isn't a domain or URL."""
    t = (text or "").strip().lower()
    if not t or " " in t:
        return None
    host = urlparse(t if "://" in t else f"https://{t}").hostname or ""
    if "." not in host:
        return None
    return host[4:] if host.startswith("www.") else host


def slug_for(domain: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")


def graph_path(brand_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]+", brand_id or ""):
        raise ValueError(f"bad brand id {brand_id!r}")
    return BRANDS_DIR / brand_id / "graph.json"


def load(brand_id: str) -> dict:
    graph = json.loads(graph_path(brand_id).read_text())
    for n in graph.get("nodes", []):
        # Graphs saved before product graphs existed list the site's menu links as products.
        if n.get("type") == "Product" and not n.get("graph"):
            old_id = n["id"]
            n.update(type="Collection", id=old_id.replace("product:", "collection:", 1))
            for e in graph.get("edges", []):
                if e.get("to") == old_id:
                    e.update(to=n["id"], rel="HAS_COLLECTION")
    return graph


def save(graph: dict) -> None:
    path = graph_path(graph["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    graph["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(graph, indent=2))
    tmp.replace(path)


def list_graphs() -> list[dict]:
    out = []
    for path in sorted(BRANDS_DIR.glob("*/graph.json")):
        try:
            out.append(summary(json.loads(path.read_text())))
        except (ValueError, KeyError):
            continue
    return sorted(out, key=lambda g: g["updated"], reverse=True)


def find(query: str) -> Optional[dict]:
    """A saved graph matching this name, domain or an earlier search for it."""
    q, domain = _norm(query), _domain_of(query)
    if not q:
        return None
    for path in BRANDS_DIR.glob("*/graph.json"):
        try:
            g = json.loads(path.read_text())
        except ValueError:
            continue
        names = {_norm(g.get("domain", "")), _norm(company(g).get("name", ""))} | {_norm(a) for a in g.get("aliases", [])}
        if q in names or (domain and domain == g.get("domain")):
            return g
    return None


def remember_alias(graph: dict, query: str) -> None:
    aliases = graph.setdefault("aliases", [])
    if query.strip() and _norm(query) not in {_norm(a) for a in aliases}:
        aliases.append(query.strip())
        save(graph)


# ── graph helpers ─────────────────────────────────────────────────────────────


def nodes(graph: dict, type_: str) -> list[dict]:
    return [n for n in graph.get("nodes", []) if n.get("type") == type_]


def node(graph: dict, node_id: str) -> Optional[dict]:
    return next((n for n in graph.get("nodes", []) if n.get("id") == node_id), None)


def company(graph: dict) -> dict:
    return node(graph, "company") or {}


def color(graph: dict, role: str, default: str) -> str:
    n = node(graph, f"color:{role}")
    return n["hex"] if n and re.fullmatch(r"#[0-9A-Fa-f]{6}", n.get("hex", "")) else default


def link(graph: dict, kind: str) -> Optional[str]:
    n = node(graph, f"link:{kind}")
    return n["url"] if n else None


def branches(graph: dict) -> dict[str, list[str]]:
    """Facts added under their own topics ({"Sustainability": [...]}), in the order they were added."""
    names = {n["id"]: n["name"] for n in nodes(graph, "Topic")}
    out: dict[str, list[str]] = {name: [] for name in names.values()}
    parent = {e["to"]: e["from"] for e in graph.get("edges", []) if e.get("rel") == "INCLUDES"}
    for f in nodes(graph, "Fact"):
        name = names.get(parent.get(f["id"], "")) or f.get("topic") or "Other"
        out.setdefault(name, []).append(f["text"])
    return {k: v for k, v in out.items() if v}


def branches_text(graph: dict, heading: str) -> str:
    b = branches(graph)
    if not b:
        return ""
    return heading + "\n" + "\n".join(f"- {topic}: {fact}" for topic, facts in b.items() for fact in facts)


def summary(graph: dict) -> dict:
    url = LocalStorageAdapter().url
    logo = node(graph, "logo:primary")
    return {
        "id": graph["id"],
        "name": company(graph).get("name") or graph.get("domain"),
        "domain": graph.get("domain"),
        "logo_url": url(logo["file"]) if logo else None,
        "logo_background": logo.get("background") if logo else None,
        "updated": graph.get("updated", ""),
    }


def form_values(graph: dict) -> dict:
    """What the UI's brand form shows for this graph."""
    c, font = company(graph), node(graph, "font:primary") or {}
    url = LocalStorageAdapter().url
    return {
        **summary(graph),
        "brand_name": c.get("name", ""),
        "tagline": c.get("tagline", ""),
        "background": color(graph, "background", "#FFFFFF"),
        "text_color": color(graph, "text", "#111111"),
        "accent": color(graph, "accent", "#111111"),
        "accent_text": color(graph, "accent_text", "#FFFFFF"),
        "font": font.get("name", "Helvetica Neue"),
        "voice": (node(graph, "voice") or {}).get("text", ""),
        "rules": "\n".join(n["text"] for n in nodes(graph, "Rule")),
        "website": link(graph, "website"),
        "socials": [{"network": n["network"], "url": n["url"]} for n in nodes(graph, "SocialProfile")],
        "address": (node(graph, "address") or {}).get("text", ""),
        "logos": [{**n, "url": url(n["file"])} for n in nodes(graph, "Logo")],
        "collections": [{"name": n["name"], "url": n.get("url")} for n in nodes(graph, "Collection")],
        "branches": branches(graph),
        "products": [{"id": n["id"].split(":", 1)[1], "name": n["name"], "url": n.get("url")} for n in nodes(graph, "Product")],
    }


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def footer_socials(graph: dict) -> list[dict]:
    """Instagram, Facebook and X profiles, in that order; other networks stay in the graph but out of emails."""
    by_net = {n.get("network"): n for n in nodes(graph, "SocialProfile")}
    return [by_net[k] for k in FOOTER_NETWORKS if k in by_net]


def pick_logo(graph: dict, background: str) -> Optional[dict]:
    """The logo variant that reads best on this background color."""
    logos = nodes(graph, "Logo")
    if not logos:
        return None
    bg_dark = _luminance(background) < 0.35

    def score(n: dict) -> tuple:
        fits = n.get("background") in ("any", "dark" if bg_dark else "light")
        ink = n.get("ink_hex")
        return (fits, _contrast(ink, background) if ink else 0, n["id"] == "logo:primary")

    return max(logos, key=score)


def email_inputs(graph: dict, background: str, logo_graph: Optional[dict] = None) -> dict:
    """The header logo and extra brand rules a run gets from this graph.

    logo_graph: take the header logo (and its rules) from another graph, e.g. a product
    that runs as its own brand; links, footer and sender still come from ``graph``.
    """
    c = company(graph)
    lg = logo_graph or graph
    name = company(lg).get("name") or (node(lg, "product") or {}).get("name") or graph.get("domain")
    logo = pick_logo(lg, background)
    rules: list[str] = []
    image = None
    if logo:
        image = {
            "key": logo["file"],
            "role": "design_asset",
            "description": (
                f"Official {name} logo. Place it at the top of the email in the header, about 120-160px wide, "
                "centered or left-aligned, on the email background with clear space around it. "
                "Never crop, stretch, recolor or redraw it, and use it only once at the top."
            ),
        }
        rules.append(
            f"The email opens with the official {name} logo (the uploaded design_asset image) in the header. "
            "Do not type the brand name as a text logo in its place."
        )
        if logo.get("background") not in ("any", "dark" if _luminance(background) < 0.35 else "light"):
            band = logo.get("tile_hex") or ("#FFFFFF" if logo.get("background") == "light" else "#111111")
            rules.append(
                f"The logo needs a {logo.get('background')} backdrop: put it on its own full-width header band "
                f"colored {band} so it stays legible."
            )
    website = link(graph, "website")
    if website:
        rules.append(f"Main call-to-action links go to {link(graph, 'shop') or website} (never a placeholder link).")
    socials = footer_socials(graph)
    footer = []
    if socials:
        footer.append("social links as small text links, only these: " + ", ".join(f"{n['network']} {n['url']}" for n in socials))
    address = (node(graph, "address") or {}).get("text")
    legal = c.get("legal_name") or name
    footer.append(f"the sender line '{legal}" + (f", {address}'" if address else "'"))
    if website:
        footer.append(f"a link to {website}")
    rules.append("The footer includes " + "; ".join(footer) + ", plus an unsubscribe link.")
    for p in nodes(graph, "Collection")[:8]:
        if p.get("url"):
            rules.append(f"If the email features {p['name']}, link it to {p['url']}.")
    facts = branches_text(graph, f"About {c.get('name') or name} (from the company graph; use only if it fits the email):")
    return {"image": image, "rules": rules, "brief": facts}


KEY_FIELDS = {"Variant": "name", "Feature": "text", "Fact": "text", "Rule": "text", "Collection": "name",
              "SocialProfile": "network", "Link": "kind", "Product": "name", "Topic": "name"}


def node_key(n: dict) -> Optional[str]:
    """What makes two nodes the same thing (a Variant by its name, a Feature by its text), whatever their ids."""
    value = n.get(KEY_FIELDS.get(n.get("type"), ""))
    return f"{n['type']}:{_norm(value)}" if isinstance(value, str) and value.strip() else None


def keep_user_edits(new: dict, old: dict, center: str) -> dict:
    """On rebuild: hand-edited nodes win, hand-deleted nodes stay deleted.

    Matched by id and by content, so a variant you added by hand that the site now lists too
    isn't kept twice, and a fact you deleted doesn't come back under a new id.
    """
    deleted = set(old.get("deleted_by_user", []))
    deleted_keys = set(old.get("deleted_keys", []))
    mine = [n for n in old.get("nodes", []) if n.get("edited")]
    mine_ids = {n["id"] for n in mine}
    taken = {node_key(n) for n in mine} | deleted_keys
    dupes = {n["id"] for n in new["nodes"] if n["id"] not in mine_ids and node_key(n) in taken - {None}}
    new["nodes"] = [n for n in new["nodes"] if n["id"] not in dupes]
    new["edges"] = [e for e in new["edges"] if e["to"] not in dupes]
    if any(n["type"] == "Logo" for n in mine):  # an uploaded logo replaces every scraped version
        dropped = {n["id"] for n in new["nodes"] if n["type"] == "Logo"} - mine_ids
        new["nodes"] = [n for n in new["nodes"] if n["id"] not in dropped]
        new["edges"] = [e for e in new["edges"] if e["to"] not in dropped]
    new["nodes"] = [n for n in new["nodes"] if n["id"] not in mine_ids and n["id"] not in deleted] + mine
    new["edges"] = [e for e in new["edges"] if e["to"] not in mine_ids and e["to"] not in deleted]
    old_edges = {e["to"]: e for e in old.get("edges", [])}
    new["edges"] += [old_edges.get(i, {"from": center, "rel": "HAS", "to": i}) for i in mine_ids if i != center]
    if deleted:
        new["deleted_by_user"] = sorted(deleted)
    if deleted_keys:
        new["deleted_keys"] = sorted(deleted_keys)
    return new


# ── building: fetch the site ──────────────────────────────────────────────────


class Fetcher:
    """HTTP GETs for one build. Each host is let through the egress guard only as
    it is reached, and private or loopback addresses are refused."""

    def __init__(self, allow_host: Callable[[str], None]):
        import httpx

        self._allow = allow_host
        self._client = httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
        self.fetched: list[str] = []

    async def get(self, url: str, max_bytes: int = 6_000_000, headers: Optional[dict] = None):
        from src.shared.utils.url_security import validate_fetch_url

        for _ in range(6):
            if url.startswith("http://"):
                url = "https://" + url[7:]  # only HTTPS is fetched; most sites serve both
            host = urlparse(url).hostname or ""
            self._allow(host)
            validate_fetch_url(url)
            resp = await self._client.get(url, headers=headers)
            if resp.is_redirect and resp.headers.get("location"):
                url = urljoin(url, resp.headers["location"])
                continue
            resp.raise_for_status()
            if len(resp.content) > max_bytes:
                raise ValueError(f"{url} is larger than {max_bytes} bytes")
            self.fetched.append(str(resp.url))
            return resp
        raise ValueError(f"too many redirects from {url}")

    async def close(self) -> None:
        await self._client.aclose()


async def _rendered_html(url: str) -> str:
    """The page after JavaScript runs, for sites that build their header client-side."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)
            return await page.content()
        finally:
            await browser.close()


def _json_ld(soup) -> list[dict]:
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                out.append(item)
                stack.extend(v for v in item.get("@graph", []) if isinstance(v, dict))
    return out


def _attrs_text(tag) -> str:
    parts = [tag.name or ""]
    for key in ("class", "id", "alt", "title", "aria-label", "src", "data-src", "href"):
        val = tag.get(key)
        parts.append(" ".join(val) if isinstance(val, list) else str(val or ""))
    return " ".join(parts).lower()


def _logo_candidates(soup, base: str, brand_hint: str) -> list[dict]:
    """Possible logos on the page, best guesses first."""
    found: dict[str, dict] = {}
    hint = _norm(brand_hint)

    def add(kind: str, score: int, url: Optional[str] = None, svg: Optional[str] = None, note: str = "") -> None:
        key = url or (svg or "")[:200]
        if not key or (url and url.startswith("data:") and "svg" not in url[:40]):
            return
        if key not in found or found[key]["score"] < score:
            found[key] = {"kind": kind, "score": score, "url": url, "svg": svg, "note": note[:160]}

    for item in _json_ld(soup):
        types = item.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(t in ("Organization", "Corporation", "Brand", "OnlineStore", "Store") for t in types):
            logo = item.get("logo")
            logo = logo.get("url") or logo.get("contentUrl") if isinstance(logo, dict) else logo
            if isinstance(logo, str):
                add("json-ld", 55, urljoin(base, logo), note="schema.org Organization logo")

    home_links = [a for a in soup.find_all("a", href=True) if urlparse(urljoin(base, a["href"])).path in ("", "/")]
    in_header = set()
    for container in soup.find_all(["header", "nav"]) + [a for a in home_links]:
        for tag in container.find_all(["img", "svg"]):
            in_header.add(id(tag))

    for tag in soup.find_all(["img", "svg"]):
        text = _attrs_text(tag)
        parent_text = " ".join(_attrs_text(p) for p in list(tag.parents)[:3])
        logo_word = "logo" in text or "logo" in parent_text or "brand" in text
        named = bool(hint) and hint in _norm(text + parent_text)
        score = (40 if logo_word else 0) + (20 if id(tag) in in_header else 0) + (10 if named else 0)
        if score < 20:
            continue
        if tag.name == "img":
            src = tag.get("src") or tag.get("data-src") or ""
            srcset = tag.get("srcset") or tag.get("data-srcset") or ""
            if srcset:  # largest candidate in the srcset
                src = srcset.split(",")[-1].strip().split(" ")[0] or src
            if src:
                add("img", score + (5 if ".svg" in src.lower() else 0), urljoin(base, src), note=text)
        else:
            if tag.find("use") and not tag.find(["path", "g", "rect", "circle", "polygon"]):
                continue  # a sprite reference; nothing to render on its own
            add("inline-svg", score + 5, svg=str(tag), note=text)

    for rel, score in (("apple-touch-icon", 12), ("icon", 8)):
        for tag in soup.find_all("link", rel=lambda r: r and rel in [x.lower() for x in (r if isinstance(r, list) else [r])]):
            if tag.get("href"):
                add(f"{rel}", score, urljoin(base, tag["href"]), note=f"{rel} {tag.get('sizes', '')}")
    return sorted(found.values(), key=lambda c: -c["score"])


def _css_facts(css_texts: list[str]) -> dict:
    colors: Counter = Counter()
    fonts: Counter = Counter()
    for css in css_texts:
        for m in re.finditer(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", css):
            h = m.group(1)
            h = "".join(c * 2 for c in h) if len(h) == 3 else h
            colors[f"#{h.upper()}"] += 1
        for m in re.finditer(r"rgba?\(\s*(\d{1,3})[ ,]+(\d{1,3})[ ,]+(\d{1,3})", css):
            colors["#%02X%02X%02X" % tuple(min(255, int(x)) for x in m.groups())] += 1
        for m in re.finditer(r"font-family\s*:\s*([^;}{]+)", css):
            first = m.group(1).split(",")[0].strip().strip("'\"")
            if first and not first.startswith(("var(", "inherit", "-apple", "system-ui", "ui-")):
                fonts[first] += 1
    return {"colors": colors.most_common(16), "fonts": fonts.most_common(8)}


def _page_facts(html: str, base: str, brand_hint: str) -> dict:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    meta = lambda **kw: (soup.find("meta", attrs=kw) or {}).get("content")  # noqa: E731
    socials, links = {}, []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        host = (urlparse(href).hostname or "").removeprefix("www.")
        net = next((v for k, v in SOCIAL_HOSTS.items() if host == k or host.endswith("." + k)), None)
        if net and net not in socials and urlparse(href).path.strip("/"):
            socials[net] = href.split("?")[0]
        label = " ".join(a.get_text(" ", strip=True).split())[:60]
        if label and urlparse(href).hostname and _domain_of(href) == _domain_of(base) and len(links) < 80:
            links.append({"text": label, "url": href.split("#")[0]})
    footer = soup.find("footer")
    org = next((i for i in _json_ld(soup) if i.get("@type") in ("Organization", "Corporation")), {})
    return {
        "soup": soup,
        "title": (soup.title.string or "").strip() if soup.title else "",
        "description": meta(name="description") or meta(property="og:description") or "",
        "site_name": meta(property="og:site_name") or org.get("name") or "",
        "theme_color": meta(name="theme-color") or "",
        "og_image": urljoin(base, meta(property="og:image")) if meta(property="og:image") else None,
        "headings": [" ".join(h.get_text(" ", strip=True).split())[:120] for h in soup.find_all(["h1", "h2"])][:12],
        "links": links,
        "socials": socials,
        "footer_text": " ".join(footer.get_text(" ", strip=True).split())[:1500] if footer else "",
        "org": {k: org.get(k) for k in ("name", "legalName", "address", "sameAs", "slogan") if org.get(k)},
        "style_blocks": [t.get_text() for t in soup.find_all("style")] + [t["style"] for t in soup.find_all(style=True)][:400],
        "stylesheets": [urljoin(base, l["href"]) for l in soup.find_all("link", rel="stylesheet", href=True)][:6],
        "google_fonts": [l["href"] for l in soup.find_all("link", href=True) if "fonts.googleapis.com" in l["href"]],
        "logos": _logo_candidates(soup, base, brand_hint),
    }


# ── building: logo files ──────────────────────────────────────────────────────


async def _rasterize_svgs(svgs: list[str]) -> list[Optional[bytes]]:
    """PNG bytes for each SVG (transparent background), drawn by headless Chromium."""
    if not svgs:
        return []
    from playwright.async_api import async_playwright

    out: list[Optional[bytes]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1000, "height": 1000}, device_scale_factor=1)
            for svg in svgs:
                # Scripts and external references never run: the page is offline content only.
                svg = re.sub(r"<script.*?</script>", "", svg, flags=re.S | re.I)
                try:
                    await page.set_content(
                        '<html><body style="margin:0;background:transparent;color:#000">'
                        f'<div id="w" style="width:800px;display:inline-block">{svg}</div></body></html>'
                    )
                    await page.evaluate(
                        """() => { const s = document.querySelector('#w svg'); if (!s) return;
                        if (!s.getAttribute('viewBox') && s.getAttribute('width') && s.getAttribute('height'))
                          s.setAttribute('viewBox', `0 0 ${parseFloat(s.getAttribute('width'))} ${parseFloat(s.getAttribute('height'))}`);
                        s.removeAttribute('width'); s.removeAttribute('height');
                        s.style.width = '800px'; s.style.height = 'auto'; s.style.display = 'block'; }"""
                    )
                    el = await page.query_selector("#w svg")
                    out.append(await el.screenshot(omit_background=True) if el else None)
                except Exception:
                    out.append(None)
        finally:
            await browser.close()
    return out


def _to_png(data: bytes):
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img.load()
    if getattr(img, "n_frames", 1) > 1:
        img.seek(0)
    img = img.convert("RGBA")
    bbox = img.getchannel("A").getbbox()
    if bbox:
        img = img.crop(bbox)
    if img.width > 1200:
        img = img.resize((1200, round(img.height * 1200 / img.width)))
    return img


def _logo_background(lums: list[float]) -> str:
    """Which backgrounds a logo reads on, judged by how much of its ink stands out (2:1 or better) on each.

    Not by its average color: a red icon with light grey lettering averages to a mid tone but its
    lettering only reads on dark (Dota). And not by its single faintest color either: Google's
    four-color G has a yellow arc that is faint on white, yet the logo is made for white.
    """
    on_white = sum((1.05 / (l + 0.05)) >= 2 for l in lums) / len(lums)
    on_black = sum(((l + 0.05) / 0.05) >= 2 for l in lums) / len(lums)
    if on_white >= 0.7 and on_black >= 0.7:
        return "any"
    return "light" if on_white >= on_black else "dark"


def _analyze(img) -> dict:
    """Ink color of the logo and which backgrounds it can sit on."""
    small = img.copy()
    small.thumbnail((200, 200))
    px = [p for p in small.getdata()]
    opaque = [p for p in px if p[3] > 200]
    transparent_share = 1 - len(opaque) / max(1, len(px))
    if not opaque:
        return {"background": "any", "ink_hex": None, "transparent": True, "mono": True}
    if transparent_share < 0.05:
        # A logo on its own solid tile: it belongs on a background of that tile's color.
        w, h = small.size
        edge = [small.getpixel((x, y)) for x in range(w) for y in (0, h - 1)] + [small.getpixel((x, y)) for y in range(h) for x in (0, w - 1)]
        tile = Counter("#%02X%02X%02X" % p[:3] for p in edge).most_common(1)[0]
        if tile[1] / len(edge) < 0.6:
            return {"background": "any", "ink_hex": None, "transparent": False, "mono": False}
        return {"background": "light" if _luminance(tile[0]) > 0.5 else "dark", "ink_hex": None,
                "tile_hex": tile[0], "transparent": False, "mono": False}
    r, g, b = (sum(p[i] for p in opaque) / len(opaque) for i in range(3))
    ink = "#%02X%02X%02X" % (round(r), round(g), round(b))
    spread = max(max(p[:3]) - min(p[:3]) for p in opaque)
    return {
        "background": _logo_background([_luminance("#%02X%02X%02X" % p[:3]) for p in opaque]),
        "ink_hex": ink,
        "transparent": True,
        "mono": spread < 40,
    }


def _recolor(img, hex_color: str):
    from PIL import Image

    h = hex_color.lstrip("#")
    solid = Image.new("RGBA", img.size, tuple(int(h[i : i + 2], 16) for i in (0, 2, 4)) + (255,))
    solid.putalpha(img.getchannel("A"))
    return solid


def add_mono_variants(graph_nodes: list[dict], edges: list[dict], brand_id: str) -> None:
    """A one-color mark becomes a clean all-dark primary plus an all-white reversed copy.

    Its parts can come out in different shades when the site colors the SVG from a
    stylesheet (dark lettering next to a white "2"), which reads on no background.
    """
    storage = LocalStorageAdapter()
    primary = next(n for n in graph_nodes if n["id"] == "logo:primary")
    img = _to_png((STORAGE_DIR / primary["file"]).read_bytes())
    graph_nodes[:] = [n for n in graph_nodes if n["id"] != "logo:reversed"]
    edges[:] = [e for e in edges if e["to"] != "logo:reversed"]
    small = img.copy()
    small.thumbnail((200, 200))
    lums = [_luminance("#%02X%02X%02X" % p[:3]) for p in small.getdata() if p[3] > 200]
    mixed = sum(l > 0.6 for l in lums) > 0.05 * len(lums) and sum(l < 0.15 for l in lums) > 0.05 * len(lums)
    if not mixed:  # a clean mark: keep it, add the opposite-color copy
        variants = [("logo:reversed", "#FFFFFF" if primary["background"] == "light" else "#111111",
                     "dark" if primary["background"] == "light" else "light")]
    else:
        variants = [("logo:primary", "#111111", "light"), ("logo:reversed", "#FFFFFF", "dark")]
    for node_id, ink, bg in variants:
        key = f"brands/{brand_id}/{node_id.replace(':', '-')}.png"
        buf = io.BytesIO()
        _recolor(img, ink).save(buf, "PNG")
        storage.upload_file_object(key, buf.getvalue())
        if node_id == "logo:primary":
            primary.update(file=key, ink_hex=ink, background=bg)
        else:
            graph_nodes.append({"id": node_id, "type": "Logo", "variant": "reversed", "file": key, "width": primary["width"],
                                "height": primary["height"], "background": bg, "ink_hex": ink, "transparent": True,
                                "mono": True, "source": primary["source"], "derived_from": "logo:primary"})
            edges.append({"from": "company", "rel": "HAS_LOGO", "to": node_id})


def _png_b64(img, max_side: int = 400) -> str:
    from PIL import Image

    view = img.copy()
    view.thumbnail((max_side, max_side))
    # Show transparent logos on mid-grey so both light and dark marks are visible.
    canvas = Image.new("RGBA", view.size, (150, 150, 150, 255))
    canvas.alpha_composite(view)
    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ── building: the model step ──────────────────────────────────────────────────


async def _ask_model(system: str, content: list[dict], max_tokens: int = 4000) -> dict:
    import anthropic  # the CLI backends swap this for their own client

    client = anthropic.AsyncAnthropic()
    msg = await client.messages.create(
        model=MODEL, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": content}]
    )
    text = "".join(getattr(b, "text", "") for b in msg.content)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"model returned no JSON: {text[:300]}")
    return json.loads(m.group(0))


async def resolve_domain(query: str) -> dict:
    domain = _domain_of(query)
    if domain:
        return {"domain": domain, "name": None}
    out = await _ask_model(
        "You map a company or brand name to its official website. Answer with JSON only.",
        [{"type": "text", "text": (
            f'Brand searched: "{query}"\n\n'
            'Return {"domain": "<the brand\'s own official website domain, e.g. allbirds.com>", '
            '"name": "<the brand name as the company writes it>", "confident": true|false}. '
            'If you do not know the brand or it is ambiguous, return {"domain": null, "confident": false}.'
        )}],
        max_tokens=300,
    )
    if not out.get("domain") or not out.get("confident"):
        raise LookupError(f'Could not tell which company "{query}" is. Search with its website instead, e.g. brand.com')
    return {"domain": _domain_of(out["domain"]) or out["domain"], "name": out.get("name")}


EXTRACT_SYSTEM = """You build a brand profile for an email designer from facts scraped from a company's website.
Use only what the facts and images show; leave a field empty rather than guess. Answer with JSON only."""


def _extract_prompt(domain: str, name_hint: str, facts: dict, css: dict, candidates: list[dict]) -> str:
    cand_lines = "\n".join(
        f"  [{i}] {c['kind']}, {c['width']}x{c['height']}px, ink {c['analysis'].get('ink_hex')}, "
        f"suits {c['analysis']['background']} backgrounds, from: {c['note'][:100]}"
        for i, c in enumerate(candidates)
    )
    return f"""Website: https://{domain}
Name hint: {name_hint or "(none)"}
Page title: {facts['title']}
Site name: {facts['site_name']}
Meta description: {facts['description']}
Theme color: {facts['theme_color']}
Headings: {json.dumps(facts['headings'])}
Structured data: {json.dumps(facts['org'], default=str)[:1500]}
Footer text: {facts['footer_text']}
Same-site links (text → url): {json.dumps(facts['links'][:60])}
Most used CSS colors (hex, count): {json.dumps(css['colors'])}
Most used fonts (name, count): {json.dumps(css['fonts'])}
Google Fonts loaded: {json.dumps(facts['google_fonts'])}

Logo candidates (the images attached, in this order, shown on grey):
{cand_lines or "  (none found)"}

Return:
{{
  "name": "brand name as the company writes it",
  "legal_name": "legal entity from the footer/copyright, or empty",
  "tagline": "the brand's own tagline or slogan if the site states one, else empty",
  "description": "one sentence: what the company sells and to whom",
  "logo_index": <index of the candidate that is the company's own primary logo/wordmark, or null if none is>,
  "icon_index": <index of a square icon/monogram version, or null>,
  "palette": {{
    "background": "#RRGGBB email body background, from the site's real palette",
    "text": "#RRGGBB body text, high contrast on background",
    "accent": "#RRGGBB brand color for buttons/highlights",
    "accent_text": "#RRGGBB label color on accent buttons, high contrast"
  }},
  "font": "primary brand font name from the CSS",
  "voice": "how the site's copy sounds, in one or two sentences",
  "rules": ["3-5 short visual/copy style rules an email designer should follow, drawn from the site"],
  "address": "postal address if shown, else empty",
  "links": {{"shop": "url or empty", "about": "url or empty", "contact": "url or empty"}},
  "collections": [{{"name": "product line or collection name", "url": "its url from the links list"}}]
}}
Collections: up to 6, only from the links list. Palette colors must come from the CSS colors, theme color or logo."""


async def build(query: str, allow_host: Callable[[str], None], log: Callable[[str], None] = print) -> dict:
    """Build and save the graph for a brand search. Returns the saved graph."""
    from bs4 import BeautifulSoup  # noqa: F401  (fail early if missing)

    log(f'Finding the website for "{query}"')
    resolved = await resolve_domain(query)
    domain = resolved["domain"]
    brand_id = slug_for(domain)
    log(f"Website: {domain}")

    fetcher = Fetcher(allow_host)
    try:
        home_url = f"https://{domain}/"
        try:
            resp = await fetcher.get(home_url)
        except Exception:
            home_url = f"https://www.{domain}/"
            try:
                resp = await fetcher.get(home_url)
            except Exception as exc:
                raise LookupError(f"Couldn't open {domain} ({str(exc).splitlines()[0][:120]}). Check the address and try again.") from exc
        base = str(resp.url)
        html = resp.text
        facts = _page_facts(html, base, resolved.get("name") or query)
        if not [c for c in facts["logos"] if c["score"] >= 40]:
            log("No logo in the page source; rendering the page in a browser")
            try:
                html = await _rendered_html(base)
                rendered = _page_facts(html, base, resolved.get("name") or query)
                facts.update({k: v for k, v in rendered.items() if v and k in ("logos", "links", "socials", "footer_text", "headings")})
            except Exception as exc:
                log(f"Browser render failed: {exc}")

        css_texts = list(facts["style_blocks"])
        for sheet in facts["stylesheets"]:
            try:
                css_texts.append((await fetcher.get(sheet, max_bytes=3_000_000)).text)
            except Exception as exc:
                log(f"Skipped stylesheet {sheet[:80]}: {exc}")
        css = _css_facts(css_texts)

        log(f"Checking {min(len(facts['logos']), 6)} logo candidates")
        raws: list[tuple[dict, bytes, bool]] = []
        for cand in facts["logos"][:6]:
            try:
                if cand["svg"]:
                    raws.append((cand, cand["svg"].encode(), True))
                elif cand["url"].startswith("data:"):
                    head, body = cand["url"].split(",", 1)
                    raws.append((cand, base64.b64decode(body) if ";base64" in head else unquote(body).encode(), True))
                else:
                    r = await fetcher.get(cand["url"], max_bytes=4_000_000)
                    is_svg = "svg" in r.headers.get("content-type", "") or cand["url"].lower().split("?")[0].endswith(".svg")
                    raws.append((cand, r.content, is_svg))
            except Exception as exc:
                log(f"  skipped {(cand['url'] or 'inline svg')[:80]}: {str(exc).splitlines()[0][:120]}")
        svg_idx = [i for i, (_c, _raw, is_svg) in enumerate(raws) if is_svg]
        drawn = await _rasterize_svgs([raws[i][1].decode(errors="replace") for i in svg_idx])
        for i, png in zip(svg_idx, drawn):
            raws[i] = (raws[i][0], png or b"", False)
        candidates = []
        for cand, raw, _ in raws:
            try:
                img = _to_png(raw)
            except Exception as exc:
                log(f"  skipped {(cand['url'] or 'inline svg')[:80]}: {str(exc).splitlines()[0][:120] or 'not an image'}")
                continue
            if img.width < 32 or img.height < 16:
                continue
            candidates.append({**cand, "img": img, "width": img.width, "height": img.height, "analysis": _analyze(img)})

        log("Reading the brand (logo, palette, voice)")
        content: list[dict] = [{"type": "text", "text": _extract_prompt(domain, resolved.get("name") or "", facts, css, candidates)}]
        for c in candidates:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _png_b64(c["img"])}})
        profile = await _ask_model(EXTRACT_SYSTEM, content)
    finally:
        await fetcher.close()

    return _assemble(query, domain, brand_id, base, facts, profile, candidates, fetcher.fetched, log)


def _assemble(query, domain, brand_id, base, facts, profile, candidates, fetched, log) -> dict:
    storage = LocalStorageAdapter()
    graph_nodes: list[dict] = []
    edges: list[dict] = []

    def add(n: dict, rel: Optional[str] = None) -> None:
        graph_nodes.append({**n, "source": n.get("source", base)})
        if rel:
            edges.append({"from": "company", "rel": rel, "to": n["id"]})

    name = profile.get("name") or facts["site_name"] or query
    add({"id": "company", "type": "Company", "name": name, "legal_name": profile.get("legal_name", ""),
         "tagline": profile.get("tagline", ""), "description": profile.get("description", "")})

    def save_logo(idx: Any, node_id: str, variant: str) -> Optional[dict]:
        if not isinstance(idx, int) or not 0 <= idx < len(candidates):
            return None
        c = candidates[idx]
        key = f"brands/{brand_id}/{node_id.replace(':', '-')}.png"
        buf = io.BytesIO()
        c["img"].save(buf, "PNG")
        storage.upload_file_object(key, buf.getvalue())
        n = {"id": node_id, "type": "Logo", "variant": variant, "file": key, "width": c["width"], "height": c["height"],
             **c["analysis"], "source": c["url"] or base, "found_as": c["kind"]}
        add(n, "HAS_LOGO")
        return n

    primary = save_logo(profile.get("logo_index"), "logo:primary", "primary")
    if not primary and candidates and candidates[0]["score"] >= 40:
        log("Model did not pick a logo; keeping the strongest candidate")
        primary = save_logo(0, "logo:primary", "primary")
    save_logo(profile.get("icon_index") if profile.get("icon_index") != profile.get("logo_index") else None, "logo:icon", "icon")
    if primary and primary.get("mono") and primary.get("transparent"):
        add_mono_variants(graph_nodes, edges, brand_id)
    if not primary:
        log("No logo found on the site")

    for role, hex_ in (profile.get("palette") or {}).items():
        if isinstance(hex_, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", hex_):
            add({"id": f"color:{role}", "type": "Color", "role": role, "hex": hex_.upper()}, "HAS_COLOR")
    if profile.get("font"):
        add({"id": "font:primary", "type": "Font", "name": profile["font"]}, "USES_FONT")
    if profile.get("voice"):
        add({"id": "voice", "type": "Voice", "text": profile["voice"]}, "HAS_VOICE")
    for i, rule in enumerate(profile.get("rules") or []):
        if isinstance(rule, str) and rule.strip():
            add({"id": f"rule:{i}", "type": "Rule", "text": rule.strip()}, "FOLLOWS")
    add({"id": "link:website", "type": "Link", "kind": "website", "url": base}, "HAS_LINK")
    for kind, url in (profile.get("links") or {}).items():
        if isinstance(url, str) and url.startswith("http") and _domain_of(url) == _domain_of(base):
            add({"id": f"link:{kind}", "type": "Link", "kind": kind, "url": url}, "HAS_LINK")
    for net, url in facts["socials"].items():
        add({"id": f"social:{net}", "type": "SocialProfile", "network": net, "url": url}, "ON_SOCIAL")
    address = profile.get("address") or ""
    if address.strip():
        add({"id": "address", "type": "Address", "text": address.strip()}, "LOCATED_AT")
    known_links = {l["url"] for l in facts["links"]}
    for i, p in enumerate(profile.get("collections") or profile.get("products") or []):
        if isinstance(p, dict) and p.get("name") and p.get("url") in known_links:
            add({"id": f"collection:{i}", "type": "Collection", "name": p["name"], "url": p["url"]}, "HAS_COLLECTION")

    graph = {
        "id": brand_id,
        "version": 1,
        "domain": domain,
        "aliases": [query.strip()],
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "nodes": graph_nodes,
        "edges": edges,
        "sources": fetched,
        "model": MODEL,
    }
    old = graph_path(brand_id)
    if old.exists():
        try:
            prev = json.loads(old.read_text())
            graph["created"] = prev.get("created", graph["created"])
            graph["aliases"] = sorted(set(prev.get("aliases", [])) | set(graph["aliases"]), key=str.lower)
            linked = [n for n in prev.get("nodes", []) if n.get("type") == "Product" and n.get("graph")]
            graph["nodes"] += linked
            # Keep each product under its category. Collection ids follow the menu's order, which can change,
            # so match the old collection to the new one by name.
            new_by_name = {_norm(c["name"]): c["id"] for c in graph["nodes"] if c.get("type") == "Collection"}
            old_names = {c["id"]: _norm(c["name"]) for c in prev.get("nodes", []) if c.get("type") == "Collection"}
            for n in linked:
                kept = [{**e, "from": new_by_name[old_names[e["from"]]]} for e in prev.get("edges", [])
                        if e["to"] == n["id"] and old_names.get(e["from"]) in new_by_name]
                graph["edges"] += kept or [{"from": "company", "rel": "SELLS", "to": n["id"]}]
            graph = keep_user_edits(graph, prev, "company")
        except ValueError:
            pass
    save(graph)
    log(f"Saved {name}: {len(graph_nodes)} facts, logo {'found' if primary else 'missing'}")
    return graph


# ── brand guide (PDF) ─────────────────────────────────────────────────────────

GUIDE_SYSTEM = """You read a company's brand guidelines and pull out what an email designer needs. Use only what the
document states; leave a field empty rather than guess. Answer with JSON only."""


def _pdf_text(path: Path, max_chars: int = 40000) -> str:
    import pymupdf

    out = []
    with pymupdf.open(path) as doc:
        for page in doc:
            out.append(page.get_text())
            if sum(map(len, out)) > max_chars:
                break
    return "\n".join(out)[:max_chars]


async def apply_brand_guide(brand_id: str, pdf_path: Path, log: Callable[[str], None] = print) -> list[str]:
    """Read an uploaded brand guide and write what it states into the graph as your own edits."""
    text = _pdf_text(pdf_path)
    if len(text.strip()) < 200:
        raise LookupError("couldn't read text from that PDF (is it scanned images only?)")
    log("Reading the brand guide")
    out = await _ask_model(GUIDE_SYSTEM, [{"type": "text", "text": f"""Brand guidelines text:
{text}

Return:
{{
  "palette": {{"background": "#RRGGBB or empty", "text": "#RRGGBB or empty", "accent": "#RRGGBB or empty", "accent_text": "#RRGGBB or empty"}},
  "font": "primary typeface name or empty",
  "voice": "the tone of voice the guide describes, one or two sentences, or empty",
  "tagline": "official tagline or empty",
  "rules": ["up to 8 concrete rules for emails or digital use: logo clear space, color usage, typography, words to avoid"]
}}
Palette roles: background = main light/dark ground color, text = body text, accent = primary brand color for buttons, accent_text = text on the accent."""}], max_tokens=2000)
    graph = load(brand_id)
    applied = []

    def put(node: dict, rel: str) -> None:
        graph["nodes"] = [n for n in graph["nodes"] if n["id"] != node["id"]]
        graph["edges"] = [e for e in graph["edges"] if e["to"] != node["id"]]
        graph["nodes"].append({**node, "edited": True, "source": "brand guide (uploaded)"})
        graph["edges"].append({"from": "company", "rel": rel, "to": node["id"]})

    for role, hex_ in (out.get("palette") or {}).items():
        if role in ("background", "text", "accent", "accent_text") and re.fullmatch(r"#[0-9A-Fa-f]{6}", str(hex_ or "")):
            put({"id": f"color:{role}", "type": "Color", "role": role, "hex": hex_.upper()}, "HAS_COLOR")
            applied.append(f"{role} color {hex_.upper()}")
    if out.get("font"):
        put({"id": "font:primary", "type": "Font", "name": out["font"]}, "USES_FONT")
        applied.append(f"font {out['font']}")
    if out.get("voice"):
        put({"id": "voice", "type": "Voice", "text": out["voice"]}, "HAS_VOICE")
        applied.append("voice")
    if out.get("tagline"):
        company_node = node(graph, "company")
        if company_node:
            company_node.update(tagline=out["tagline"], edited=True)
            applied.append("tagline")
    for i, rule in enumerate(out.get("rules") or []):
        if isinstance(rule, str) and rule.strip():
            put({"id": f"rule:guide-{i}", "type": "Rule", "text": rule.strip()}, "FOLLOWS")
    if out.get("rules"):
        applied.append(f"{len(out['rules'])} rules")
    where = str(pdf_path.resolve().relative_to(STORAGE_DIR.resolve())) if pdf_path.resolve().is_relative_to(STORAGE_DIR.resolve()) else pdf_path.name
    graph["brand_guide"] = {"file": where, "applied": applied, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    save(graph)
    return applied
