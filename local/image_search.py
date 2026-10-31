"""Find images for an email: anywhere on the web, on the brand's own website, or on Openverse.

    web        a web search for pages about the query (Claude's web search on a Claude login, the
               API's on API keys, Brave if set, Wikipedia), then the photos on those pages: each
               page's share image (og:image) first, then its large images
    brand      the brand's own photos: those already saved in its product graphs, plus the images on
               its pages that best match the search (menu and product pages first)
    openverse  Openverse's image index (Creative Commons and public domain photos)
    pexels     stock photos, when PEXELS_API_KEY is set

Every result keeps the page it came from and its credit line, for reference.

Runs in brand_worker.py (it opens other sites). A picked image is downloaded, checked and saved as
a staged upload (uploads/staged/...); the page then adds it to the run like a dropped file.
"""

from __future__ import annotations

import io
import os
import re
import uuid
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import brand_graph as bg
import product_graph as pg
from bootstrap import LocalStorageAdapter

UA = "ai-email-pipeline/1.0 (https://github.com/akshai0296/ai-email-pipeline)"
SKIP = re.compile(r"(icon|logo|sprite|badge|payment|flag|avatar|pixel|spacer|placeholder|\.svg)", re.I)
MAX_RESULTS = 30


# ── the brand's own images ────────────────────────────────────────────────────


def _images_on(html: str, base: str) -> list[dict]:
    """Images on a page with their alt text: <img> (src, srcset, lazy data-*) and <picture><source>."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out = []
    for el in soup.find_all(["img", "source"]):
        srcset = el.get("srcset") or el.get("data-srcset") or ""
        src = (srcset.split(",")[-1].strip().split(" ")[0] if srcset else "") or el.get("src") or el.get("data-src") or ""
        if not src or src.startswith("data:") or SKIP.search(src):
            continue
        width = int(re.sub(r"\D", "", str(el.get("width") or "")) or 0)
        if width and width < 240:
            continue
        img = el if el.name == "img" else (el.find_parent("picture").find("img") if el.find_parent("picture") else None)
        alt = " ".join(((img.get("alt") if img else "") or "").split())
        out.append({"url": urljoin(base, src), "alt": alt, "page": base})
    return out


def _score(words: list[str], text: str) -> float:
    import graph_modify

    return graph_modify._relevance(words, "", text) if words else 0.0


async def brand_images(brand_id: str, query: str, product_id: Optional[str], allow_host, log) -> list[dict]:
    import graph_modify

    brand = bg.load(brand_id)
    words = graph_modify._keywords(query)
    url = LocalStorageAdapter().url
    results: list[dict] = []
    # 1. Photos already saved in the brand's product graphs (the chosen product's first).
    products = sorted(pg.list_products(brand_id), key=lambda p: p["id"] != product_id)
    for p in products:
        g = pg.load(brand_id, p["id"])
        for n in bg.nodes(g, "ProductImage"):
            text = f"{p['name']} {n.get('description', '')}"
            results.append({"thumb": url(n["file"]), "url": url(n["file"]), "local": n["file"], "title": n.get("description") or p["name"],
                            "credit": f"{bg.company(brand).get('name') or brand['domain']} (saved {p['name']} photo)",
                            "license": "the brand's own", "page": g.get("url"), "score": 2 + _score(words, text) + (p["id"] == product_id)})
    # 2. Images on the brand's pages that match the search.
    site = bg.link(brand, "website") or f"https://{brand['domain']}/"
    fetcher = bg.Fetcher(allow_host)
    try:
        home = await fetcher.get(site)
        pages = [(str(home.url), home.text)]
        from bs4 import BeautifulSoup

        links = []
        for a in BeautifulSoup(home.text, "html.parser").find_all("a", href=True):
            href = urljoin(str(home.url), a["href"]).split("#")[0]
            if graph_modify._same_site(href, brand["domain"]):
                links.append((href, " ".join(a.get_text(" ", strip=True).split())))
        ranked = sorted({h: _score(words, f"{urlparse(h).path} {t}") for h, t in links}.items(), key=lambda kv: -kv[1])
        for href, s in ranked[:4]:
            if s <= 0:
                break
            try:
                r = await fetcher.get(href)
                pages.append((str(r.url), r.text))
            except Exception as exc:
                log(f"  skipped {href[:80]}: {str(exc).splitlines()[0][:60]}")
        for page_url, html in pages:
            for im in _images_on(html, page_url):
                results.append({"thumb": im["url"], "url": im["url"], "title": im["alt"] or "photo from the brand's site",
                                "credit": f"{bg.company(brand).get('name') or brand['domain']} ({urlparse(page_url).path or '/'})",
                                "license": "the brand's own", "page": page_url,
                                "score": _score(words, f"{im['alt']} {urlparse(im['url']).path}") + (0.3 if page_url != pages[0][0] else 0)})
    except Exception as exc:
        log(f"Couldn't read the brand's site: {str(exc).splitlines()[0][:80]}")
    finally:
        await fetcher.close()
    seen, out = set(), []
    for r in sorted(results, key=lambda r: -r["score"]):
        key = re.sub(r"[?#].*$", "", r["url"])
        if key not in seen:
            seen.add(key)
            out.append({k: v for k, v in r.items() if k != "score"})
    return out[:MAX_RESULTS]


# ── anywhere on the web ───────────────────────────────────────────────────────


async def web_images(query: str, allow_host, log) -> list[dict]:
    """Search the web for pages about the query and collect their photos."""
    import web_search
    from bs4 import BeautifulSoup

    words = re.findall(r"[a-z0-9]+", query.lower())
    fetcher = bg.Fetcher(allow_host)
    results: list[dict] = []
    try:
        urls = await web_search.find_pages(f"pages with good, large photos of {query}", query, "", fetcher, log, n=6, name=query)
        for page in urls[:6]:
            try:
                r = await fetcher.get(page, headers=web_search.headers_for(page))
            except Exception as exc:
                log(f"  skipped {page[:80]}: {str(exc).splitlines()[0][:60]}")
                continue
            page_url = str(r.url)
            soup = BeautifulSoup(r.text, "html.parser")
            title = (soup.title.string or "").strip() if soup.title and soup.title.string else urlparse(page_url).hostname
            host = urlparse(page_url).hostname or ""
            og = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "twitter:image"})
            found = ([{"url": urljoin(page_url, og["content"]), "alt": title}] if og and og.get("content") else []) + _images_on(r.text, page_url)
            for i, im in enumerate(found[:12]):
                text = f"{im['alt']} {urlparse(im['url']).path}".lower()
                results.append({"thumb": im["url"], "url": im["url"], "title": im["alt"] or title, "credit": f"from {host}",
                                "license": "", "page": page_url,
                                "score": sum(w in text for w in words) / max(len(words), 1) + (0.5 if i == 0 else 0)})
    finally:
        await fetcher.close()
    seen, out = set(), []
    for r in sorted(results, key=lambda r: -r["score"]):
        key = re.sub(r"[?#].*$", "", r["url"])
        if key not in seen:
            seen.add(key)
            out.append({k: v for k, v in r.items() if k != "score"})
    return out[:MAX_RESULTS]


# ── Openverse and stock photos ────────────────────────────────────────────────


async def openverse(query: str, allow_host) -> list[dict]:
    import httpx

    allow_host("api.openverse.org")
    async with httpx.AsyncClient(timeout=25, headers={"User-Agent": UA}) as client:
        r = await client.get("https://api.openverse.org/v1/images/", params={"q": query, "page_size": 20, "mature": "false"})  # 20 is the most without an API key
        r.raise_for_status()
    out = []
    for x in r.json().get("results", []):
        lic = f"CC {x.get('license', '').upper()} {x.get('license_version', '')}".strip() if x.get("license") not in ("cc0", "pdm") else (
            "public domain" if x.get("license") == "pdm" else "CC0 (no rights reserved)")
        out.append({"thumb": x.get("thumbnail") or x["url"], "url": x["url"], "title": x.get("title") or query,
                    "credit": x.get("attribution") or f"{x.get('title')} by {x.get('creator')}", "creator": x.get("creator"),
                    "license": lic, "license_url": x.get("license_url"), "page": x.get("foreign_landing_url"),
                    "needs_credit": x.get("license") not in ("cc0", "pdm"), "width": x.get("width"), "height": x.get("height")})
    return out


async def pexels(query: str, allow_host) -> list[dict]:
    import httpx

    allow_host("api.pexels.com")
    async with httpx.AsyncClient(timeout=25, headers={"Authorization": os.environ["PEXELS_API_KEY"]}) as client:
        r = await client.get("https://api.pexels.com/v1/search", params={"query": query, "per_page": MAX_RESULTS})
        r.raise_for_status()
    return [{"thumb": x["src"]["medium"], "url": x["src"]["large2x"], "title": x.get("alt") or query,
             "credit": f"Photo by {x['photographer']} on Pexels", "license": "Pexels licence (free to use)", "page": x["url"],
             "needs_credit": False, "width": x.get("width"), "height": x.get("height")} for x in r.json().get("photos", [])]


def sources() -> list[dict]:
    out = [{"id": "web", "label": "Anywhere on the web"}, {"id": "brand", "label": "The brand's website"},
           {"id": "openverse", "label": "Openverse (Creative Commons photos)"}]
    if os.environ.get("PEXELS_API_KEY"):
        out.append({"id": "pexels", "label": "Free stock (Pexels)"})
    return out


async def search(source: str, query: str, brand_id: Optional[str], product_id: Optional[str],
                 allow_host: Callable[[str], None], log: Callable[[str], None] = print) -> list[dict]:
    query = query.strip()
    if source == "brand":
        if not brand_id:
            raise LookupError("pick a company first to search its website")
        return await brand_images(brand_id, query, product_id, allow_host, log)
    if not query:
        raise LookupError("type what the image should show")
    if source == "web":
        return await web_images(query, allow_host, log)
    if source == "openverse":
        return await openverse(query, allow_host)
    if source == "pexels" and os.environ.get("PEXELS_API_KEY"):
        return await pexels(query, allow_host)
    raise LookupError(f"unknown image source {source!r}")


# ── describing an uploaded image ──────────────────────────────────────────────

DESCRIBE_SYSTEM = "You describe photos for an email designer. Plain, specific and factual. Answer with JSON only."
ROLES = ("hero_image", "product_image", "lifestyle_image", "header_image", "design_asset")


async def describe(path: str, brand_id: Optional[str], product_id: Optional[str]) -> dict:
    """One line on what an uploaded image shows (naming the product when it clearly is it) and the role it suits."""
    from pathlib import Path

    img = bg._to_png(Path(path).read_bytes())
    context = ""
    if brand_id:
        brand = bg.load(brand_id)
        context = f"The email is for {bg.company(brand).get('name') or brand['domain']}."
        if product_id:
            v = pg.values(pg.load(brand_id, product_id))
            context += f" Its product: {v['name']}" + (f", available in {', '.join(v['variants'][:8])}" if v["variants"] else "") + "."
    prompt = f"""{context}
Describe the attached image in one line for an email designer: what it shows (name the product only if it clearly is the
product above, and its colour if one of those listed matches), the angle or crop, the setting or background, and anything
notable (hands, people, text in the image). 8-25 words, no marketing language.
Then pick the role it suits best: hero_image (a strong main photo), product_image (the product on its own),
lifestyle_image (people or a scene), header_image (a wide banner), design_asset (a logo, icon or graphic).
Return {{"description": "...", "role": "..."}}"""
    out = await bg._ask_model(DESCRIBE_SYSTEM, [{"type": "text", "text": prompt},
                                                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": bg._png_b64(img, 640)}}],
                              max_tokens=300)
    role = out.get("role") if out.get("role") in ROLES else ""
    return {"description": str(out.get("description") or "").strip()[:300], "role": role}


# ── adding one ────────────────────────────────────────────────────────────────


async def fetch(url: str, title: str, credit: str, local: str, allow_host) -> dict:
    """Download (or copy) a picked image, check it, make it email-safe (no WebP, at most 2000px) and stage it."""
    from PIL import Image

    from bootstrap import STORAGE_DIR

    if local:
        path = (STORAGE_DIR / local).resolve()
        if STORAGE_DIR.resolve() not in path.parents or not path.exists():
            raise LookupError("that saved photo is gone")
        data = path.read_bytes()
    else:
        fetcher = bg.Fetcher(allow_host)
        try:
            data = (await fetcher.get(url, max_bytes=15_000_000)).content
        finally:
            await fetcher.close()
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise LookupError("that link isn't an image")
    if img.width < 200 or img.height < 120:
        raise LookupError("that image is too small for an email")
    img.thumbnail((2000, 2000))
    alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    buf = io.BytesIO()
    if alpha:
        img.convert("RGBA").save(buf, "PNG", optimize=True)
        ext = "png"
    else:
        img.convert("RGB").save(buf, "JPEG", quality=88, optimize=True)
        ext = "jpg"
    stem = re.sub(r"[^a-z0-9]+", "-", (title or "image").lower()).strip("-")[:40] or "image"
    key = f"uploads/staged/{uuid.uuid4().hex[:8]}-{stem}.{ext}"
    LocalStorageAdapter().upload_file_object(key, buf.getvalue())
    return {"key": key, "url": LocalStorageAdapter().url(key), "name": f"{stem}.{ext}", "width": img.width, "height": img.height,
            "description": f"{title} (credit: {credit})" if credit else title}
