"""Product knowledge graphs, one per product, linked to their brand's graph.

    brand "allbirds-com" + "Tree Runner"
        →  find the product page (store search, the site's links, its sitemap)
        →  read it: structured Product data, price, facts, variants, photos
        →  a model keeps the real product photos and the facts the page states
        →  storage/brands/<brand>/products/<product>.json  (+ its images in <product>/)
        →  the brand graph gets a Product node pointing at the file

A product stores only what is its own (images, facts, price, variants, page);
logo, palette, voice and footer come from the brand. A saved product is found
again by its name, page URL or any earlier search, like brand graphs are.

Graph shape (<product>.json):
    nodes: Product (center), Brand, Offer, ProductImage, Feature, Variant, Link
    edges: product -MADE_BY-> brand, -HAS_IMAGE-> image:0, -HAS_FEATURE-> ...
"""

from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urljoin, urlparse

import brand_graph as bg
import page_reader
from bootstrap import LocalStorageAdapter

# Where a product's own logo goes in its emails (only matters once the product has one):
#   brand    the brand's logo in the header; the product appears by name and photos     (default)
#   both     the brand's logo in the header and the product's logo above its hero image
#   product  the product's logo replaces the brand's (products run as their own brand, e.g. Dota 2 under Valve)
LOGO_MODES = ("brand", "both", "product")

IMAGE_ROLES = {"hero": "hero_image", "product": "product_image", "detail": "product_image", "lifestyle": "lifestyle_image"}


# ── store ─────────────────────────────────────────────────────────────────────


def _check_id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9-]+", value or ""):
        raise ValueError(f"bad id {value!r}")
    return value


def product_path(brand_id: str, product_id: str) -> Path:
    return bg.BRANDS_DIR / _check_id(brand_id) / "products" / f"{_check_id(product_id)}.json"


def load(brand_id: str, product_id: str) -> dict:
    return json.loads(product_path(brand_id, product_id).read_text())


def save(graph: dict) -> None:
    path = product_path(graph["brand_id"], graph["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    graph["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(graph, indent=2))
    tmp.replace(path)


def _all(brand_id: str) -> list[dict]:
    out = []
    for path in (bg.BRANDS_DIR / _check_id(brand_id) / "products").glob("*.json"):
        try:
            out.append(json.loads(path.read_text()))
        except ValueError:
            continue
    return out


def list_products(brand_id: str) -> list[dict]:
    return sorted((summary(g) for g in _all(brand_id)), key=lambda p: p["updated"], reverse=True)


def _clean_url(url: str) -> str:
    p = urlparse(url)
    return f"{(p.hostname or '').removeprefix('www.')}{p.path.rstrip('/')}".lower()


def find(brand_id: str, query: str) -> Optional[dict]:
    """A saved product of this brand matching the name, page URL or an earlier search."""
    q = bg._norm(query)
    as_url = _clean_url(query) if "/" in query.strip().rstrip("/") else None
    if not q:
        return None
    for g in _all(brand_id):
        p = product(g)
        names = {bg._norm(p.get("name", "")), bg._norm(g["id"])} | {bg._norm(a) for a in g.get("aliases", [])}
        if q in names or (as_url and as_url == _clean_url(g.get("url", ""))):
            return g
    return None


def remember_alias(graph: dict, query: str) -> None:
    aliases = graph.setdefault("aliases", [])
    if query.strip() and bg._norm(query) not in {bg._norm(a) for a in aliases}:
        aliases.append(query.strip())
        save(graph)


# ── graph helpers ─────────────────────────────────────────────────────────────


def product(graph: dict) -> dict:
    return bg.node(graph, "product") or {}


def summary(graph: dict) -> dict:
    url = LocalStorageAdapter().url
    p, offer = product(graph), bg.node(graph, "offer") or {}
    images = bg.nodes(graph, "ProductImage")
    return {
        "id": graph["id"],
        "brand_id": graph["brand_id"],
        "name": p.get("name") or graph["id"],
        "url": graph.get("url"),
        "price": _price_text(offer),
        "image_url": url(images[0]["file"]) if images else None,
        "updated": graph.get("updated", ""),
    }


def logo_mode(graph: Optional[dict]) -> str:
    if not graph or not bg.nodes(graph, "Logo"):
        return "brand"
    mode = graph.get("logo_mode", "brand")
    return mode if mode in LOGO_MODES else "brand"


def identity(graph: dict) -> dict:
    """The product's own look, when it has one (overrides the brand's in emails about it)."""
    url = LocalStorageAdapter().url
    colors = {n["role"]: n["hex"] for n in bg.nodes(graph, "Color") if n.get("role") and n.get("hex")}
    font = (bg.node(graph, "font:primary") or {}).get("name")
    voice = (bg.node(graph, "voice") or {}).get("text")
    logos = [{"url": url(n["file"]), "background": n.get("background")} for n in bg.nodes(graph, "Logo")]
    return {k: v for k, v in {"colors": colors, "font": font, "voice": voice, "logos": logos}.items() if v}


def values(graph: dict) -> dict:
    """What the UI's product card shows."""
    url = LocalStorageAdapter().url
    history = graph.get("check_history") or []
    return {
        **summary(graph),
        "identity": identity(graph),
        "logo_mode": graph.get("logo_mode", "brand") if graph.get("logo_mode") in LOGO_MODES else "brand",
        "last_checked": graph.get("last_checked"),
        "recent_changes": [c for h in history[:5] for c in h.get("changes", [])][:6],
        "description": product(graph).get("description", ""),
        "images": [{"url": url(n["file"]), "role": n.get("role"), "description": n.get("description")} for n in bg.nodes(graph, "ProductImage")],
        "features": [n["text"] for n in bg.nodes(graph, "Feature")],
        "variants": [n["name"] for n in bg.nodes(graph, "Variant")],
        "branches": bg.branches(graph),
        "placement": placement(graph),
    }


def _price_text(offer: dict) -> str:
    if not offer.get("price"):
        return ""
    cur = offer.get("currency") or ""
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹", "CAD": "CA$", "AUD": "A$"}.get(cur.upper())
    return f"{symbol}{offer['price']}" if symbol else f"{offer['price']} {cur}".strip()


def email_inputs(graph: dict, max_images: int = 4) -> dict:
    """Images, a facts block for the brief, and link rules a run gets from a product graph."""
    p, offer = product(graph), bg.node(graph, "offer") or {}
    name = p.get("name") or graph["id"]
    images = []
    for n in bg.nodes(graph, "ProductImage")[:max_images]:
        images.append({
            "key": n["file"],
            "role": IMAGE_ROLES.get(n.get("role", ""), "product_image"),
            "description": f"{name}: {n.get('description') or 'product photo'} (official photo from the product page)",
        })
    lines = [f"Product: {name}", f"Product page: {graph.get('url')}"]
    if _price_text(offer):
        lines.append(f"Price: {_price_text(offer)}" + (f" ({offer['availability']})" if offer.get("availability") else ""))
    if p.get("description"):
        lines.append(f"About it: {p['description']}")
    variants = [n["name"] for n in bg.nodes(graph, "Variant")]
    if variants:
        lines.append("Available in: " + ", ".join(variants[:12]))
    features = [n["text"] for n in bg.nodes(graph, "Feature")]
    if features:
        lines.append("Facts from the product page:\n" + "\n".join(f"- {f}" for f in features))
    more = bg.branches_text(graph, "More facts about it, by topic:")
    if more:
        lines.append(more)
    lines.append("Use only these product facts; do not invent specs, prices or materials.")
    rules = [f"Product call-to-action buttons link to the {name} page: {graph.get('url')} (never a placeholder link)."]
    cat = placement(graph)["category"]
    if cat and cat.get("url"):
        rules.append(f"A secondary link to browse more like it (e.g. 'See all {cat['name']}') goes to {cat['url']}.")
    return {"images": images, "brief": "\n".join(lines), "rules": rules}


# ── building: find the product page ───────────────────────────────────────────


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1 or t.isdigit()]


def _score(query_tokens: list[str], url: str, text: str = "") -> float:
    if not query_tokens:
        return 0
    words = set(_tokens(urlparse(url).path)) | set(_tokens(text))
    hit = sum(t in words for t in query_tokens) / len(query_tokens)
    path = urlparse(url).path.lower()
    return hit + (0.15 if re.search(r"/(products?|p|item|shop)/", path) else 0) - (0.2 if re.search(r"/(collections?|category|categories|blog|pages?)/[^/]*/?$", path) else 0)


async def _sitemap_urls(fetcher: bg.Fetcher, site: str, log: Callable[[str], None]) -> list[str]:
    from bs4 import BeautifulSoup

    roots = []
    try:
        robots = (await fetcher.get(urljoin(site, "/robots.txt"), max_bytes=500_000)).text
        roots = [line.split(":", 1)[1].strip() for line in robots.splitlines() if line.lower().startswith("sitemap:")]
    except Exception:
        pass
    roots = roots[:3] or [urljoin(site, "/sitemap.xml")]
    urls: list[str] = []
    queue, seen = list(roots), set()
    while queue and len(seen) < 8:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            soup = BeautifulSoup((await fetcher.get(sm, max_bytes=8_000_000)).text, "xml")
        except Exception as exc:
            log(f"  sitemap {sm[:80]} skipped: {str(exc).splitlines()[0][:80]}")
            continue
        if soup.find("sitemapindex"):
            children = [l.get_text(strip=True) for l in soup.find_all("loc")]
            queue.extend(sorted(children, key=lambda c: "product" not in c.lower())[:4])
        else:
            urls.extend(l.get_text(strip=True) for l in soup.find_all("loc"))
    return urls[:50000]


async def find_product_url(fetcher: bg.Fetcher, brand: dict, query: str, log: Callable[[str], None]) -> str:
    q = query.strip()
    if q.startswith(("http://", "https://")) or (bg._domain_of(q.split("/")[0]) and "/" in q.strip("/")):
        return q if q.startswith("http") else f"https://{q}"
    site = bg.link(brand, "website") or f"https://{brand['domain']}/"
    host = urlparse(site).hostname
    brand_words = set(_tokens(bg.company(brand).get("name", "")))
    toks = [t for t in _tokens(q) if t not in brand_words] or _tokens(q)
    found: dict[str, float] = {}

    def consider(url: str, text: str = "") -> None:
        url = urljoin(site, url).split("#")[0].split("?")[0]
        if (urlparse(url).hostname or "").removeprefix("www.") != (host or "").removeprefix("www."):
            return
        found[url] = max(found.get(url, 0), _score(toks, url, text))

    log(f'Looking for "{q}" on {host}')
    try:  # Shopify stores answer their own product search
        r = await fetcher.get(f"https://{host}/search/suggest.json?q={quote(q)}&resources[type]=product&resources[limit]=6", max_bytes=1_000_000)
        for item in r.json().get("resources", {}).get("results", {}).get("products", []):
            u = urljoin(site, item.get("url", "")).split("#")[0].split("?")[0]
            consider(u, item.get("title", ""))
            if u in found:
                found[u] += 0.3  # the store's own search ranked it
    except Exception:
        pass
    if not found or max(found.values()) < 0.9:
        pages = [site] + [n["url"] for n in bg.nodes(brand, "Collection")][:4]
        for page in pages:
            try:
                html = (await fetcher.get(page)).text
            except Exception:
                continue
            from bs4 import BeautifulSoup

            for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
                consider(a["href"], a.get_text(" ", strip=True))
    if not found or max(found.values()) < 0.9:
        log("Searching the sitemap")
        for url in await _sitemap_urls(fetcher, site, log):
            consider(url)
    ranked = sorted(found.items(), key=lambda kv: -kv[1])
    if not ranked or ranked[0][1] < 0.6:
        near = ", ".join(u for u, s in ranked[:3] if s > 0.3)
        raise LookupError(f'Couldn\'t find a page for "{q}" on {host}. Paste the product page link instead.' + (f" Closest: {near}" if near else ""))
    log(f"Product page: {ranked[0][0]}")
    return ranked[0][0]


# ── building: product photos ──────────────────────────────────────────────────


def subject_fill(img) -> Optional[float]:
    """How much of a plain-background photo its subject fills (0-1), or None when there's no plain background."""
    from collections import Counter

    from PIL import Image, ImageChops

    small = img.convert("RGB")
    small.thumbnail((400, 400))
    w, h = small.size
    px = small.load()
    edge = [px[x, y] for x in range(w) for y in (0, h - 1)] + [px[x, y] for y in range(h) for x in (0, w - 1)]
    (bg_key, count), = Counter((r // 8, g // 8, b // 8) for r, g, b in edge).most_common(1)
    if count / len(edge) < 0.8:
        return None
    mask = ImageChops.difference(small, Image.new("RGB", small.size, tuple(c * 8 + 4 for c in bg_key))).convert("L").point(lambda v: 255 if v > 24 else 0)
    return mask.histogram()[255] / (w * h)


def crop_to_subject(img, fill_target: float = 0.6, margin: float = 0.08):
    """A product shot on a plain background, cropped to the product (plus a margin) when the product fills too
    little of the frame; store photos often leave half the picture empty, which makes the product tiny in an email.
    Photos without a plain background (lifestyle shots) are left as they are. Returns (image, cropped?)."""
    from collections import Counter

    from PIL import Image, ImageChops

    rgb = img.convert("RGB")
    small = rgb.copy()
    small.thumbnail((400, 400))
    w, h = small.size
    px = small.load()
    edge = [px[x, y] for x in range(w) for y in (0, h - 1)] + [px[x, y] for y in range(h) for x in (0, w - 1)]
    (bg_key, count), = Counter((r // 8, g // 8, b // 8) for r, g, b in edge).most_common(1)
    if count / len(edge) < 0.8:
        return img, False  # no plain background
    bg_rgb = tuple(c * 8 + 4 for c in bg_key)
    mask = ImageChops.difference(small, Image.new("RGB", small.size, bg_rgb)).convert("L").point(lambda v: 255 if v > 24 else 0)
    box = mask.getbbox()
    if not box or (box[2] - box[0]) * (box[3] - box[1]) / (w * h) >= fill_target:
        return img, False
    sx, sy = img.width / w, img.height / h
    pad = margin * max((box[2] - box[0]) * sx, (box[3] - box[1]) * sy)
    left, top = max(0, box[0] * sx - pad), max(0, box[1] * sy - pad)
    right, bottom = min(img.width, box[2] * sx + pad), min(img.height, box[3] * sy + pad)
    if right - left < 300 or bottom - top < 200:
        return img, False  # too small to stand on its own
    return img.crop((round(left), round(top), round(right), round(bottom))), True


# ── building: read the product page ───────────────────────────────────────────


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else ([] if v is None else [v])


def _image_urls(v: Any, base: str) -> list[str]:
    out = []
    for item in _as_list(v):
        u = item.get("url") or item.get("contentUrl") if isinstance(item, dict) else item
        if isinstance(u, str) and u.strip():
            out.append(urljoin(base, u.strip()))
    return out


def _page_facts(html: str, base: str) -> dict:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    meta = lambda **kw: (soup.find("meta", attrs=kw) or {}).get("content")  # noqa: E731
    ld = None
    for item in bg._json_ld(soup):
        types = _as_list(item.get("@type"))
        if any(t in ("Product", "ProductGroup", "IndividualProduct", "VideoGame", "SoftwareApplication") for t in types):
            ld = item
            break
    images: list[str] = []
    variants = []
    if ld:
        images += _image_urls(ld.get("image"), base)
        for v in _as_list(ld.get("hasVariant"))[:30]:
            if isinstance(v, dict):
                images += _image_urls(v.get("image"), base)[:1]
                variants.append({k: v.get(k) for k in ("name", "color", "size", "sku") if v.get(k)})
    if meta(property="og:image"):
        images.append(urljoin(base, meta(property="og:image")))
    details = page_reader.details(soup)  # swatches and alt text, before the svgs (swatch colors) are removed
    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    for img in main.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        srcset = img.get("srcset") or img.get("data-srcset") or ""
        if srcset:
            src = srcset.split(",")[-1].strip().split(" ")[0] or src
        if not src or src.startswith("data:") or re.search(r"(icon|logo|sprite|badge|payment|flag)", src, re.I):
            continue
        width = int(re.sub(r"\D", "", str(img.get("width") or "")) or 0)
        if width and width < 250:
            continue
        images.append(urljoin(base, src))
    # <picture><source srcset="..."> holds the large photo on many brand sites (the <img> is a small fallback).
    for source in main.find_all("source"):
        srcset = source.get("srcset") or source.get("data-srcset") or ""
        if not srcset or (source.get("type") and not source["type"].startswith("image/")) or re.search(r"(icon|logo|sprite|badge)", srcset, re.I):
            continue
        best = srcset.split(",")[-1].strip().split(" ")[0]
        if best and not best.startswith("data:"):
            images.append(urljoin(base, best))
    seen, uniq = set(), []
    for u in images:
        key = re.sub(r"([?&](width|w|height|h|v)=\d+)|(_\d+x\d*)", "", u)
        if key not in seen:
            seen.add(key)
            uniq.append(u)
    for tag in main.find_all(["nav", "header", "footer"]):
        tag.decompose()
    items = [" ".join(li.get_text(" ", strip=True).split()) for li in main.find_all("li")]
    offers = [o for o in _as_list((ld or {}).get("offers")) if isinstance(o, dict)]
    return {
        "ld": {k: (ld or {}).get(k) for k in ("name", "description", "sku", "color", "material", "category", "brand", "gtin13", "model") if (ld or {}).get(k)},
        "offers": [{k: o.get(k) for k in ("price", "lowPrice", "highPrice", "priceCurrency", "availability") if o.get(k)} for o in offers[:3]],
        "variants": variants,
        "details": details,
        "title": (soup.title.string or "").strip() if soup.title else "",
        "h1": " ".join(soup.find("h1").get_text(" ", strip=True).split()) if soup.find("h1") else "",
        "og_title": meta(property="og:title") or "",
        "description": meta(name="description") or meta(property="og:description") or "",
        "meta_price": meta(property="product:price:amount") or meta(property="og:price:amount") or "",
        "meta_currency": meta(property="product:price:currency") or meta(property="og:price:currency") or "",
        "list_items": [i for i in items if 8 <= len(i) <= 200][:50],
        "text": " ".join(main.get_text(" ", strip=True).split())[:6000],
        "images": uniq[:16],
    }


SHOP_PATH = re.compile(r"/(shop|buy|store|cart|checkout|configure|configurator)(/|$)|/buy-|[?&](config|configure)=", re.I)
OVERVIEW_TEXT = re.compile(r"^(overview|learn more|explore|discover|why [a-z]|see (all )?features|features|about this product|product details)\b", re.I)


def overview_candidates(html: str, url: str) -> list[str]:
    """For a shop / buy / configurator page, the product's own overview page: big brands keep the purchase options
    on one page and what the product does (features, specs, photos) on another. Linked "Overview" / "Learn more"
    pages first, then the product's slug at the site root (apple.com/shop/buy-iphone/iphone-18-pro → apple.com/iphone-18-pro/)."""
    from bs4 import BeautifulSoup

    if not SHOP_PATH.search(urlparse(url).path + "?" + urlparse(url).query):
        return []
    host = urlparse(url).hostname or ""
    slug = [s for s in urlparse(url).path.split("/") if s][-1:] or [""]
    slug_words = set(_tokens(slug[0])) - {"buy", "shop"}
    out: list[str] = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        text = " ".join(a.get_text(" ", strip=True).split())
        p = urlparse(href)
        if p.hostname != host or SHOP_PATH.search(p.path) or href.rstrip("/") == url.rstrip("/"):
            continue
        if OVERVIEW_TEXT.search(text) and slug_words & set(_tokens(p.path)):
            out.append(href)
    if slug[0]:
        out.append(f"{urlparse(url).scheme}://{host}/{slug[0]}/")
    return list(dict.fromkeys(out))[:3]


def _merge_overview(facts: dict, over: dict, url: str) -> dict:
    """Add an overview page's text, list items, photos and details to the shop page's facts."""
    return {
        **facts,
        "text": (facts["text"][:2500] + f"\n\nThe product's overview page ({url}): " + over["text"])[:7000],
        "list_items": (facts["list_items"] + over["list_items"])[:70],
        "images": list(dict.fromkeys(over["images"][:10] + facts["images"]))[:16],  # the overview's photos show the product best
        "details": facts.get("details", []) + over.get("details", [])[:40],
        "overview_url": url,
    }


PRODUCT_SYSTEM = """You turn a scraped product page into a product profile for an email designer.
Use only what the page and its photos show. Never invent prices, specs, materials or claims. Answer with JSON only."""


def _product_prompt(brand_name: str, url: str, facts: dict, photos: list[dict]) -> str:
    photo_lines = "\n".join(f"  [{i}] {p['width']}x{p['height']}px from {p['url'][:110]}" for i, p in enumerate(photos))
    return f"""Brand: {brand_name}
Page: {url}
Title: {facts['title']}
H1: {facts['h1']}
Structured product data: {json.dumps(facts['ld'], default=str)[:2500]}
Offers: {json.dumps(facts['offers'])}
Variants: {json.dumps(facts['variants'][:20])}
Details on the page that plain text leaves out (option buttons with their colors, dropdowns, image alt text):
{chr(10).join(facts.get('details') or [])[:5000] or '(none)'}
Meta price: {facts['meta_price']} {facts['meta_currency']}
Meta description: {facts['description']}
{f"The page is a shop page, so the product's overview page was read too: {facts['overview_url']}. Its text follows the shop page's text below." if facts.get('overview_url') else ''}
List items on the page: {json.dumps(facts['list_items'][:40])}
Page text: {facts['text'][:5000]}

Photos found on the page (attached in this order):
{photo_lines or "  (none)"}

Return:
{{
  "is_product_page": true if this page is about one specific product (not a category, search or home page),
  "name": "product name as the page writes it",
  "description": "one or two sentences describing the product, from the page",
  "category": "short product category, e.g. running shoe",
  "price": "price as a number string, e.g. 98 (empty if not shown)",
  "currency": "ISO code, e.g. USD (empty if not shown)",
  "availability": "in stock / sold out / preorder, or empty",
  "features": ["up to 10 concrete facts the page states: what the product does and is made of first (materials, specs, key features, benefits, care, sustainability), then buying details (sizes, storage, trade-in, financing) only if room is left"],
  "variants": [{{"name": "color or edition name", "hex": "#RRGGBB if the photo or name makes it clear, else empty"}}],
  "photos": [{{"index": <photo index>, "role": "hero" | "product" | "detail" | "lifestyle", "description": "what the photo shows, one line"}}]
}}
Photos: keep only real photos of THIS product (up to 6, best first, one "hero"); drop logos, icons, banners, other products and duplicates.
Variants: up to 12, distinct. Take them from the structured data, the option buttons and the image alt text, not just the
page text. A page can show option groups for several products (e.g. a comparison table): use only this product's group, judging
by its position and the alt text. Use the hex an option button shows; otherwise only when the photo or name makes it clear."""


async def build(brand_id: str, query: str, allow_host: Callable[[str], None], log: Callable[[str], None] = print) -> dict:
    """Build and save the graph for a product of a saved brand. Returns the saved product graph."""
    brand = bg.load(brand_id)
    brand_name = bg.company(brand).get("name") or brand["domain"]
    fetcher = bg.Fetcher(allow_host)
    try:
        url = await find_product_url(fetcher, brand, query, log)
        resp = await fetcher.get(url)
        base = str(resp.url)
        facts = _page_facts(resp.text, base)
        if not facts["ld"] and (len(facts["text"]) < 800 or len(facts["images"]) < 2):
            log("Rendering the product page in a browser")
            try:
                rendered = _page_facts(await bg._rendered_html(base), base)
                if len(rendered["text"]) > len(facts["text"]) or len(rendered["images"]) > len(facts["images"]):
                    facts = rendered
            except Exception as exc:
                log(f"Browser render failed: {str(exc).splitlines()[0][:120]}")
        for over_url in overview_candidates(resp.text, base):
            over = await _read_overview(fetcher, over_url, facts, log)
            if over:
                facts = _merge_overview(facts, over, over_url)
                break

        log(f"Checking {min(len(facts['images']), 12)} photos")
        photos = []
        for img_url in facts["images"][:12]:
            try:
                img = bg._to_png((await fetcher.get(img_url, max_bytes=12_000_000)).content)
            except Exception as exc:
                log(f"  skipped {img_url[:80]}: {str(exc).splitlines()[0][:80] or 'not an image'}")
                continue
            if img.width < 300 or img.height < 200:
                continue
            fill = subject_fill(img)
            if fill is not None and fill < 0.03:
                log(f"  skipped {img_url[:70]}: almost empty (a plain frame with little in it)")
                continue
            photos.append({"url": img_url, "img": img, "width": img.width, "height": img.height})
            if len(photos) == 10:
                break

        log("Reading the product (facts, price, photos)")
        content: list[dict] = [{"type": "text", "text": _product_prompt(brand_name, base, facts, photos)}]
        for p in photos:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": bg._png_b64(p["img"], 320)}})
        profile = await bg._ask_model(PRODUCT_SYSTEM, content)
    finally:
        await fetcher.close()

    if not profile.get("is_product_page", True):
        raise LookupError(f"{base} doesn't look like a single product's page. Paste the link to the product itself.")
    graph = _assemble(brand, query, base, facts, profile, photos, fetcher.fetched, log)
    if facts.get("overview_url"):
        graph["overview_url"] = facts["overview_url"]
        save(graph)
    try:
        await place_products(brand_id, [graph["id"]], allow_host, log)
        graph = load(brand_id, graph["id"])
    except Exception as exc:  # placing it in the menu never costs the product
        log(f"Couldn't place it under a category: {str(exc).splitlines()[0][:100]}")
    return graph


async def _read_overview(fetcher: bg.Fetcher, url: str, shop: dict, log) -> Optional[dict]:
    """An overview page's facts, if it is about the same product (rendered in a browser if its photos load late)."""
    try:
        resp = await fetcher.get(url)
    except Exception as exc:
        log(f"  no overview page at {url[:80]}: {str(exc).splitlines()[0][:60]}")
        return None
    over = _page_facts(resp.text, str(resp.url))
    name = shop["ld"].get("name") or shop["h1"] or shop["og_title"] or shop["title"]
    want = [t for t in _tokens(name) if len(t) > 1][:6]
    have = set(_tokens(over["title"] + " " + over["h1"] + " " + over["text"][:3000]))
    if want and sum(t in have for t in want) / len(want) < 0.6:
        log(f"  {url[:80]} isn't about {name}; skipped")
        return None
    if len(over["images"]) < 3:
        try:
            rendered = _page_facts(await bg._rendered_html(str(resp.url)), str(resp.url))
            if len(rendered["images"]) > len(over["images"]):
                over = rendered
        except Exception as exc:
            log(f"  overview render failed: {str(exc).splitlines()[0][:80]}")
    log(f"This is a shop page, so the product's overview page was read too: {str(resp.url)[:90]}")
    return over


def _product_id(url: str, name: str) -> str:
    segment = [s for s in urlparse(url).path.split("/") if s][-1:] or [name]
    pid = re.sub(r"[^a-z0-9]+", "-", re.sub(r"\.[a-z]{2,4}$", "", segment[0].lower())).strip("-")[:60]
    return pid or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "product"


def _assemble(brand, query, base, facts, profile, photos, fetched, log) -> dict:
    storage = LocalStorageAdapter()
    name = (profile.get("name") or facts["ld"].get("name") or facts["h1"] or query).strip()
    pid = _product_id(base, name)
    brand_id = brand["id"]
    graph_nodes: list[dict] = []
    edges: list[dict] = []

    def add(n: dict, rel: Optional[str] = None) -> None:
        graph_nodes.append({**n, "source": n.get("source", base)})
        if rel:
            edges.append({"from": "product", "rel": rel, "to": n["id"]})

    add({"id": "product", "type": "Product", "name": name, "description": profile.get("description", ""),
         "category": profile.get("category", ""), "sku": str(facts["ld"].get("sku") or "")})
    add({"id": "brand", "type": "Brand", "name": bg.company(brand).get("name") or brand["domain"], "brand_id": brand_id,
         "source": bg.link(brand, "website") or base}, "MADE_BY")
    add({"id": "link:product", "type": "Link", "kind": "product page", "url": base}, "HAS_PAGE")
    price = str(profile.get("price") or "").strip()
    if price:
        add({"id": "offer", "type": "Offer", "price": price, "currency": profile.get("currency", ""),
             "availability": profile.get("availability", "")}, "HAS_OFFER")
    for i, text in enumerate(profile.get("features") or []):
        if isinstance(text, str) and text.strip():
            add({"id": f"feature:{i}", "type": "Feature", "text": text.strip()}, "HAS_FEATURE")
    for i, v in enumerate(profile.get("variants") or []):
        if isinstance(v, dict) and v.get("name"):
            hex_ = v.get("hex") if re.fullmatch(r"#[0-9A-Fa-f]{6}", v.get("hex") or "") else ""
            add({"id": f"variant:{i}", "type": "Variant", "name": v["name"], "hex": hex_.upper()}, "COMES_IN")

    kept = 0
    folder = f"brands/{brand_id}/products/{pid}"
    for item in profile.get("photos") or []:
        idx = item.get("index") if isinstance(item, dict) else None
        if not isinstance(idx, int) or not 0 <= idx < len(photos) or kept >= 6:
            continue
        img, cropped = crop_to_subject(photos[idx]["img"])
        if cropped:
            log(f"  photo {kept + 1}: cropped to the product (it filled little of the frame)")
        if img.width > 1200:
            img = img.resize((1200, round(img.height * 1200 / img.width)))
        has_alpha = img.getchannel("A").getextrema()[0] < 250
        buf = io.BytesIO()
        if has_alpha:
            img.save(buf, "PNG", optimize=True)
            key = f"{folder}/image-{kept}.png"
        else:
            img.convert("RGB").save(buf, "JPEG", quality=86, optimize=True)
            key = f"{folder}/image-{kept}.jpg"
        storage.upload_file_object(key, buf.getvalue())
        role = item.get("role") if item.get("role") in IMAGE_ROLES else "product"
        add({"id": f"image:{kept}", "type": "ProductImage", "file": key, "role": "hero" if kept == 0 and role == "product" else role,
             "description": item.get("description", ""), "width": img.width, "height": img.height,
             "source": photos[idx]["url"], **({"cropped_from": [photos[idx]["width"], photos[idx]["height"]]} if cropped else {})}, "HAS_IMAGE")
        kept += 1
    if not kept:
        log("No usable product photos on the page")

    graph = {
        "id": pid,
        "brand_id": brand_id,
        "version": 1,
        "url": base,
        "aliases": [query.strip()],
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "nodes": graph_nodes,
        "edges": edges,
        "sources": fetched,
        "model": bg.MODEL,
    }
    path = product_path(brand_id, pid)
    if path.exists():
        try:
            prev = json.loads(path.read_text())
            graph["created"] = prev.get("created", graph["created"])
            graph["aliases"] = sorted(set(prev.get("aliases", [])) | set(graph["aliases"]), key=str.lower)
            graph = bg.keep_user_edits(graph, prev, "product")
            for k in ("last_checked", "check_history"):
                if k in prev:
                    graph[k] = prev[k]
        except ValueError:
            pass
    save(graph)
    _link_into_brand(brand_id, graph)
    log(f"Saved {name}: {len(graph_nodes)} facts, {kept} photos")
    return graph


def _link_into_brand(brand_id: str, graph: dict) -> None:
    """Put (or refresh) this product's node in the brand graph."""
    brand = bg.load(brand_id)
    node_id = f"product:{graph['id']}"
    old = next((n for n in brand["nodes"] if n.get("id") == node_id), {})
    kept = [e for e in brand["edges"] if e.get("to") == node_id and e["from"] != "company" and bg.node(brand, e["from"])]
    brand["nodes"] = [n for n in brand["nodes"] if n.get("id") != node_id]
    brand["edges"] = [e for e in brand["edges"] if e.get("to") != node_id]
    brand["nodes"].append({"id": node_id, "type": "Product", "name": product(graph).get("name", graph["id"]),
                           "url": graph["url"], "graph": f"products/{graph['id']}.json", "source": graph["url"],
                           **({"category": old["category"]} if old.get("category") else {})})
    # Under its category if it already had one (place_products sets it after a build), else straight under the company.
    brand["edges"] += kept or [{"from": "company", "rel": "SELLS", "to": node_id}]
    bg.save(brand)


# ── where a product sits in the brand's menu ──────────────────────────────────

CHROME = re.compile(r"(^|[-_ ])(nav|navbar|menu|megamenu|header|footer|breadcrumb|drawer|sidebar)([-_ ]|$)", re.I)
PLACE_SYSTEM = "You sort a store's products into its menu categories. Answer with JSON only."


def _slug_tail(url: str) -> str:
    segs = [s for s in urlparse(url).path.split("/") if s]
    return segs[-1].lower() if segs else ""


def _content_links(html: str, base: str) -> set[str]:
    """The product links in a page's main content (the site's menus, header and footer link everywhere)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for el in soup.find_all(["header", "nav", "footer", "aside"]) + soup.find_all(attrs={"role": re.compile("navigation|banner|contentinfo|menu")}):
        el.decompose()
    for el in soup.find_all(True):
        if el.attrs is not None and CHROME.search(" ".join(el.get("class") or []) + " " + (el.get("id") or "")):
            el.decompose()
    return {_slug_tail(urljoin(base, a["href"])) for a in soup.find_all("a", href=True)}


async def _collection_links(brand: dict, allow_host, log) -> dict[str, Optional[set]]:
    """For each collection, the product links on its page (None when the page couldn't be read). Paced, since
    stores rate-limit quick visitors."""
    import asyncio

    fetcher = bg.Fetcher(allow_host)
    out: dict[str, Optional[set]] = {}
    try:
        for c in bg.nodes(brand, "Collection"):
            out[c["id"]] = None
            if not c.get("url"):
                continue
            for attempt in range(2):
                try:
                    r = await fetcher.get(c["url"], max_bytes=20_000_000)
                    out[c["id"]] = _content_links(r.text, str(r.url))
                    break
                except Exception as exc:
                    if "429" in str(exc) and attempt == 0:
                        await asyncio.sleep(8)
                        continue
                    log(f"  couldn't read {c['name']}: {str(exc).splitlines()[0][:60]}")
                    break
            await asyncio.sleep(1.5)
    finally:
        await fetcher.close()
    return out


async def place_products(brand_id: str, product_ids: Optional[list[str]], allow_host: Callable[[str], None],
                         log: Callable[[str], None] = print) -> dict:
    """Put each product under its category in the brand graph (Phones → Pixel 11 Pro), and note the promotional
    collections that list it (Offers, New Arrivals...). The category is the model's pick from the brand's
    collections; promotional ones count only when their page really lists the product."""
    brand = bg.load(brand_id)
    collections = bg.nodes(brand, "Collection")
    ids = product_ids or [n["id"].split(":", 1)[1] for n in bg.nodes(brand, "Product") if n.get("graph")]
    if not collections or not ids:
        return {}
    log(f"Checking which of {len(collections)} collections list the product")
    links = await _collection_links(brand, allow_host, log)
    placed = {}
    for pid in ids:
        try:
            graph = load(brand_id, pid)
        except (ValueError, FileNotFoundError):
            continue
        p = product(graph)
        tails = {_slug_tail(graph["url"])} | ({_slug_tail(graph["overview_url"])} if graph.get("overview_url") else set())
        listed = {c["id"]: (None if links.get(c["id"]) is None else bool(tails & links[c["id"]])) for c in collections}
        out = await bg._ask_model(PLACE_SYSTEM, [{"type": "text", "text": f"""Store: {bg.company(brand).get('name') or brand['domain']}
Product: {p.get('name')} ({p.get('category') or 'no category given'}). {p.get('description', '')[:400]}
Product page: {graph['url']}

The store's collections (listed: whether that collection's page lists this product; null = couldn't check):
{json.dumps([{"id": c["id"], "name": c["name"], "url": c.get("url"), "listed": listed[c["id"]]} for c in collections])}

1. category: the ONE collection that is this product's own category, where a shopper browsing the menu would find it
   (a kind of product, e.g. Phones, iPhone, Women's Shoes). Promotional collections (offers, sale, new arrivals,
   bestsellers, gifts) are never the category. null if no collection fits.
2. featured: promotional collections that list this product (only ones with listed = true).
Return {{"category": "<collection id or null>", "featured": ["<collection ids>"], "reason": "one sentence"}}"""}], max_tokens=400)
        valid = {c["id"] for c in collections}
        category = out.get("category") if out.get("category") in valid else None
        featured = [f for f in out.get("featured") or [] if f in valid and f != category and listed.get(f)]
        _set_placement(brand, graph, category, featured)
        placed[pid] = {"category": category, "featured": featured, "reason": out.get("reason", "")}
        names = {c["id"]: c["name"] for c in collections}
        log(f"{p.get('name')}: under {names.get(category, 'no category')}" + (f"; featured in {', '.join(names[f] for f in featured)}" if featured else ""))
    bg.save(brand)
    return placed


def _set_placement(brand: dict, graph: dict, category: Optional[str], featured: list[str]) -> None:
    """Edges in both graphs: collection → product in the brand graph (instead of company → product when it has a
    category), and the product's category and featured collections as nodes in its own graph."""
    node_id = f"product:{graph['id']}"
    by_id = {c["id"]: c for c in bg.nodes(brand, "Collection")}
    brand["edges"] = [e for e in brand["edges"] if e["to"] != node_id]
    if category:
        brand["edges"].append({"from": category, "rel": "INCLUDES", "to": node_id})
    else:
        brand["edges"].append({"from": "company", "rel": "SELLS", "to": node_id})
    brand["edges"] += [{"from": f, "rel": "FEATURES", "to": node_id} for f in featured]
    for n in brand["nodes"]:
        if n["id"] == node_id:
            n["category"] = by_id[category]["name"] if category else None
    graph["nodes"] = [n for n in graph["nodes"] if n.get("type") != "Collection"]
    graph["edges"] = [e for e in graph["edges"] if not e["to"].startswith("collection:")]
    for cid, rel, role in [(category, "IN_CATEGORY", "category")] + [(f, "FEATURED_IN", "featured") for f in featured]:
        if cid:
            c = by_id[cid]
            graph["nodes"].append({"id": cid, "type": "Collection", "name": c["name"], "url": c.get("url"), "role": role, "source": c.get("url")})
            graph["edges"].append({"from": "product", "rel": rel, "to": cid})
    save(graph)


def placement(graph: dict) -> dict:
    cols = bg.nodes(graph, "Collection")
    cat = next((c for c in cols if c.get("role") == "category"), None)
    return {"category": {"name": cat["name"], "url": cat.get("url")} if cat else None,
            "featured": [{"name": c["name"], "url": c.get("url")} for c in cols if c.get("role") == "featured"]}


# ── keeping products current ──────────────────────────────────────────────────

AVAILABILITY = {"instock": "in stock", "outofstock": "sold out", "soldout": "sold out", "preorder": "preorder",
                "backorder": "backorder", "limitedavailability": "limited stock", "discontinued": "discontinued"}


def _availability(value: str) -> str:
    key = re.sub(r"[^a-z]", "", (value or "").rsplit("/", 1)[-1].lower())
    return AVAILABILITY.get(key, value or "")


def _num(price: str) -> str:
    try:
        return f"{float(str(price).replace(',', '')):g}"
    except ValueError:
        return str(price).strip()


async def check_for_changes(brand_id: str, product_id: str, allow_host: Callable[[str], None], log: Callable[[str], None] = print) -> dict:
    """Re-read the product page (no model call) and record what changed: price, stock, new variants, a missing page."""
    import httpx

    graph = load(brand_id, product_id)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    changes: list[str] = []
    fetcher = bg.Fetcher(allow_host)
    facts = None
    try:
        resp = await fetcher.get(graph["url"])
        facts = _page_facts(resp.text, str(resp.url))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (404, 410):
            changes.append(f"the product page is gone ({exc.response.status_code})")
        else:
            log(f"Couldn't read the page ({exc.response.status_code}); nothing recorded")
    finally:
        await fetcher.close()
    if facts:
        offer = bg.node(graph, "offer")
        o = facts["offers"][0] if facts["offers"] else {}
        price = o.get("price") or o.get("lowPrice") or facts["meta_price"]
        currency = o.get("priceCurrency") or facts["meta_currency"]
        stock = _availability(o.get("availability", ""))
        if price:
            if not offer:
                offer = {"id": "offer", "type": "Offer", "price": _num(price), "currency": currency, "availability": stock, "source": graph["url"]}
                graph["nodes"].append(offer)
                graph["edges"].append({"from": "product", "rel": "HAS_OFFER", "to": "offer"})
                changes.append(f"price now shown: {_price_text(offer)}")
            elif _num(price) != _num(offer.get("price", "")):
                old = _price_text(offer)
                if not offer.get("edited"):
                    offer["price"], offer["currency"] = _num(price), currency or offer.get("currency", "")
                changes.append(f"price {old} → {_price_text({'price': _num(price), 'currency': currency or offer.get('currency', '')})}")
        if offer and stock and stock != offer.get("availability"):
            changes.append(f"stock {offer.get('availability') or 'unknown'} → {stock}")
            if not offer.get("edited"):
                offer["availability"] = stock
        known = {bg._norm(n["name"]) for n in bg.nodes(graph, "Variant")}
        seen = {v.get("color") or v.get("name") for v in facts["variants"] if v.get("color") or v.get("name")}
        new = sorted(v for v in seen if not any(k and (k in bg._norm(v) or bg._norm(v) in k) for k in known))
        if known and new:
            changes.append("new on the page: " + ", ".join(new[:5]))
    graph["last_checked"] = now
    graph["check_history"] = ([{"at": now, "changes": changes}] + (graph.get("check_history") or []))[:20]
    save(graph)
    log("No changes" if not changes else "Changes: " + "; ".join(changes))
    return {"changes": changes, "last_checked": now}


def due_for_check(days: float) -> list[tuple[str, str]]:
    """(brand, product) pairs not checked for this many days, oldest first."""
    cutoff = time.time() - days * 86400
    due = []
    for path in bg.BRANDS_DIR.glob("*/products/*.json"):
        try:
            g = json.loads(path.read_text())
        except ValueError:
            continue
        stamp = g.get("last_checked") or g.get("updated") or ""
        try:
            t = time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            t = 0
        if t < cutoff:
            due.append((t, g["brand_id"], g["id"]))
    return [(b, p) for _, b, p in sorted(due)]
