"""Change a graph from a plain-language request, confirmed before anything is saved.

    "add their sustainability facts"  →  find pages on the brand's site about it
                                       →  the model proposes changes, each with the page it came from
                                          and a quote; quotes are checked against the page text
                                       →  the proposal is saved, nothing in the graph changes yet
    you tick the changes to keep       →  apply() writes them as your edits (kept on rebuild)

A change either fits an existing node type (a Feature, a Collection, a Rule, a Color...)
or starts a new branch: a Topic node ("Sustainability") with Fact nodes under it.
Proposals live in storage/brands/<brand>/proposals/<id>.json.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import brand_graph as bg
import graph_edit
import page_reader
import product_graph as pg

MAX_PAGES = 5          # from the brand's own site
MAX_WEB_PAGES = 4      # from a web search, when asked for
PAGE_CHARS = 14000     # page text sent to the model, per page
DETAIL_CHARS = 6000    # of that, for the details visible text leaves out (image alt text, option swatches)
WEB_WORDS = re.compile(r"\b(web ?search|search (the )?(web|internet|online)|google it|look (it )?up online|online|internet|other sites?)\b", re.I)

MODIFY_SYSTEM = """You update a company's knowledge graph for an email designer from a request and web pages.
Use only what the pages say, or values the request states outright. Never invent facts. Answer with JSON only."""


def _proposals_dir(brand_id: str):
    d = bg.graph_path(brand_id).parent / "proposals"  # graph_path checks the id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9%$£€.#]+", " ", text.lower().replace("’", "'")).strip()


# ── reading a page ────────────────────────────────────────────────────────────

def _read_page(html: str, words: list[str]) -> dict:
    """Text for the model (the page's start, then the parts that match the request), the full text to check quotes
    against, and the links."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    links = [(a.get("href", ""), " ".join(a.get_text(" ", strip=True).split())) for a in soup.find_all("a")]
    details = page_reader.details(soup)
    for t in soup(["script", "style", "noscript", "svg", "template"]):
        t.decompose()
    visible = " ".join(soup.get_text(" ", strip=True).split())
    detail_text = "\n".join(details)[:DETAIL_CHARS]
    room = PAGE_CHARS - len(detail_text)
    if len(visible) > room:
        sentences = re.split(r"(?<=[.!?])\s+", visible[3000:])
        picked = [x for x in sentences if words and _relevance(words, "", x) > 0]
        visible = visible[:3000] + " … " + " … ".join(picked)
    text = visible[:room] + (f"\n\nDetails on the page that aren't plain text:\n{detail_text}" if detail_text else "")
    full = " ".join(soup.get_text(" ", strip=True).split()) + "\n" + "\n".join(details)
    return {"text": text, "full": full, "links": links}


def _page_images(html: str, url: str) -> list[dict]:
    """A page's photos: its share image (og:image) first, then its large images, with their alt text."""
    import image_search
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", attrs={"property": "og:image"})
    first = [{"url": urljoin(url, og["content"]), "alt": (soup.title.string or "").strip() if soup.title and soup.title.string else "", "page": url}] \
        if og and og.get("content") else []
    return first + image_search._images_on(html, url)


def _fingerprint(img) -> list[bool]:
    """Average hash: an 8x8 greyscale version, each cell lighter or darker than the mean."""
    small = img.convert("L").resize((8, 8))
    px = list(small.getdata())
    mean = sum(px) / len(px)
    return [p > mean for p in px]


PHOTO_WORDS = re.compile(r"\b(photos?|images?|pictures?|pics?|shots?|imagery|gallery|visuals?)\b", re.I)
MAX_PHOTOS = 12


def wants_photos(request: str) -> bool:
    return bool(PHOTO_WORDS.search(request))


async def _photo_candidates(pages: list[dict], graph: dict, brand_id: str, product_id: str, request: str, allow_host, log) -> list[dict]:
    """Download the photos on the pages read, keep usable ones (not tiny, not near-empty, not already in the graph),
    crop them to the product and stage them, so the model can look at them and the review can show them."""
    import io

    import product_graph as pg
    import web_search
    from bootstrap import LocalStorageAdapter

    have = {re.sub(r"[?#].*$", "", n.get("source", "")) for n in bg.nodes(graph, "ProductImage")}
    # A product page can hold hundreds of images (menus, other products): rank them before downloading any.
    words = _keywords(request)
    name = [t for t in pg._tokens((pg.product(graph).get("name") or "")) if len(t) > 1]
    seen, queue = set(), []
    for order, p in enumerate(sorted(pages, key=lambda p: p["web"])):  # the brand's own pages first
        for im in p.get("images", []):
            key = re.sub(r"[?#].*$", "", im["url"])
            if key in seen or key in have:
                continue
            seen.add(key)
            text = f"{im['alt']} {urlparse(im['url']).path}"
            named = sum(t in pg._tokens(text) for t in name) / len(name) if name else 0
            im["rank"] = 2 * _relevance(words, "", text) + named - 0.1 * order
            queue.append(im)
    queue.sort(key=lambda im: -im["rank"])
    fetcher = bg.Fetcher(allow_host)
    storage = LocalStorageAdapter()
    batch = uuid.uuid4().hex[:6]
    out: list[dict] = []
    prints: list[list[bool]] = []  # starts with the product's own photos, so nothing it already has is offered again
    from PIL import Image

    from bootstrap import STORAGE_DIR

    for n in bg.nodes(graph, "ProductImage"):
        try:
            with Image.open(STORAGE_DIR / n["file"]) as own:
                prints.append(_fingerprint(own))
        except OSError:
            pass
    try:
        for im in queue[:40]:
            if len(out) >= MAX_PHOTOS:
                break
            try:
                img = bg._to_png((await fetcher.get(im["url"], max_bytes=15_000_000, headers=web_search.headers_for(im["url"]))).content)
            except Exception:
                continue
            if img.width < 300 or img.height < 200:
                continue
            fill = pg.subject_fill(img)
            if fill is not None and fill < 0.03:
                continue
            img, _ = pg.crop_to_subject(img)
            # Sites list one picture at several sizes: keep it once (a small brightness fingerprint per image).
            fp = _fingerprint(img)
            if any(sum(a != b for a, b in zip(fp, k)) <= 6 for k in prints):
                continue
            prints.append(fp)
            img.thumbnail((1200, 1200))
            alpha = img.getchannel("A").getextrema()[0] < 250
            buf = io.BytesIO()
            img.save(buf, "PNG", optimize=True) if alpha else img.convert("RGB").save(buf, "JPEG", quality=86, optimize=True)
            key = f"brands/{brand_id}/products/{product_id}/proposed/{batch}-{len(out)}.{'png' if alpha else 'jpg'}"
            storage.upload_file_object(key, buf.getvalue())
            out.append({"n": len(out), "file": key, "url": im["url"], "page": im["page"], "alt": im["alt"],
                        "width": img.width, "height": img.height, "b64": bg._png_b64(img, 320)})
    finally:
        await fetcher.close()
    log(f"Found {len(out)} new photo{'s' if len(out) != 1 else ''} to look at")
    return out


# ── finding pages ─────────────────────────────────────────────────────────────

STOPWORDS = set("""a an the and or of to for in on at by with from about their its it this that these those them they there
add put include change update fix set make use remove delete drop replace new more some any all every each other others also
please pls info information details detail facts fact like such as branch node graph page pages site sites website websites
web online internet search look find get give show list see check want need mentioned mention missing should shld would could
can will do does did done is are be been was were has have had brand company product products here not only just""".split())


def _keywords(request: str) -> list[str]:
    words = [t for t in pg._tokens(re.sub(r"https?://\S+", " ", request)) if t not in STOPWORDS and not (t.isdigit() and len(t) < 2)]
    return list(dict.fromkeys(words))


def _relevance(words: list[str], url: str, text: str = "") -> float:
    """Share of the request's keywords in a page's path or link text (prefix match, so "sustainable" finds "sustainability")."""
    if not words:
        return 0
    have = set(pg._tokens(urlparse(url).path)) | set(pg._tokens(text))

    def same(w: str, h: str) -> bool:
        return w == h or (len(w) > 4 and len(h) > 4 and w[:5] == h[:5])

    return sum(any(same(w, h) for h in have) for w in words) / len(words)


def _same_site(url: str, domain: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def _mentions(text: str, name: str) -> bool:
    """Does a page name the product or company? (most of the name's words, e.g. "pixel 11 pro")"""
    want = [t for t in pg._tokens(name) if t not in ("the", "and", "by", "of")]
    have = set(pg._tokens(text[:200_000]))
    return not want or sum(t in have for t in want) / len(want) >= 0.75


def _is_product_page(url: str) -> bool:
    return bool(re.search(r"/(products?|p|item|dp)/", urlparse(url).path.lower()))


def wants_web(request: str) -> bool:
    return bool(WEB_WORDS.search(request))


async def _gather_pages(request: str, brand: dict, product: Optional[dict], web: bool, allow_host, log) -> list[dict]:
    domain = brand.get("domain") or ""
    site = bg.link(brand, "website") or f"https://{domain}/"
    words = _keywords(request)
    fetcher = bg.Fetcher(allow_host)
    pages: list[dict] = []
    seen: set[str] = set()

    import web_search

    async def read(url: str, limit: int = MAX_PAGES, from_web: bool = False, about: str = "") -> Optional[list[tuple[str, str]]]:
        url = url.split("#")[0]
        if url in seen or sum(p["web"] == from_web for p in pages) >= limit:
            return None
        seen.add(url)
        try:
            resp = await fetcher.get(url, headers=web_search.headers_for(url))
        except Exception as exc:
            log(f"  skipped {url[:80]}: {str(exc).splitlines()[0][:80]}")
            return None
        if "html" not in resp.headers.get("content-type", "html"):
            return None
        page = _read_page(resp.text, words)
        page["images"] = _page_images(resp.text, str(resp.url))
        if about and not _mentions(page["full"], about):
            log(f"  skipped {str(resp.url)[:80]}: not about {about}")
            return None
        pages.append({"url": str(resp.url), "text": page["text"], "full": page["full"],
                      "web": from_web and not _same_site(str(resp.url), domain), "images": page["images"]})
        log(f"Read {str(resp.url)[:90]}")
        return [(urljoin(str(resp.url), h), t) for h, t in page["links"] if h and not h.startswith(("mailto:", "tel:", "javascript:"))]

    try:
        # 1. Links pasted into the request are read first, wherever they point.
        for url in re.findall(r"https?://[^\s<>\"')]+", request)[:3]:
            await read(url.rstrip(".,;"))
        # 2. The page this graph was built from.
        start_links = await read(product["url"]) if product and product.get("url") else None
        home_links = await read(site) or []
        # 3. The site's own pages that best match the request: menu links first, then the sitemap.
        #    A product lookup skips other products' pages.
        def fits(url: str) -> bool:
            return _same_site(url, domain) and not (product and _is_product_page(url))

        scored: dict[str, float] = {}
        for href, text in (start_links or []) + home_links:
            href = href.split("#")[0].split("?")[0]
            if fits(href) and _relevance(words, href, text) > 0:
                scored[href] = max(scored.get(href, 0), _relevance(words, href, text) + 0.1)  # linked from the menu
        if words:
            log("Searching the site for pages about it")
            for url in await pg._sitemap_urls(fetcher, site, log):
                if fits(url) and _relevance(words, url) > scored.get(url, 0):
                    scored[url] = _relevance(words, url)
        for url, score in sorted(scored.items(), key=lambda kv: (-kv[1], len(kv[0])))[:MAX_PAGES * 2]:
            if score < 0.3:
                break
            await read(url)
        # 4. The wider web, when asked for.
        if web:
            company = bg.company(brand).get("name") or domain
            name = re.split(r"\s+(?:&|and|/)\s+", pg.product(product).get("name", ""))[0] if product else company
            subject = f"{pg.product(product).get('name')} by {company}" if product else company
            log("Searching the web")
            for url in await web_search.find_pages(request, subject, domain, fetcher, log, n=MAX_WEB_PAGES, name=name):
                await read(url, MAX_WEB_PAGES, from_web=True, about=name)
    finally:
        await fetcher.close()
    return pages


# ── asking for changes ────────────────────────────────────────────────────────


def _node_summary(n: dict) -> dict:
    keep = {k: v for k, v in n.items() if k not in ("file", "file_url", "edited", "found_as", "width", "height", "source",
                                                       "luminance", "transparent", "mono", "ink_hex", "tile_hex", "graph")}
    return {k: (v[:200] if isinstance(v, str) else v) for k, v in keep.items()}


def _photo_instructions(photos: list[dict]) -> str:
    return f"""
Photos: {len(photos)} photos from these pages are attached after this text, numbered 0-{len(photos) - 1}. When the request asks
for photos, add the ones that show THIS product and match what was asked (e.g. the colour or angle named), best first, at most 6:
{{"op": "add", "type": "ProductImage", "fields": {{"image": <photo number>, "role": "hero|product|detail|lifestyle",
"description": "what the photo shows, one line (product, colour, angle, setting)"}}, "source_url": "<page it came from>",
"evidence": "<what in the photo matches the request>", "basis": "page"}}
Use role "hero" only if the request asks for a new main photo. Skip photos of other products, logos, banners and duplicates.
"""


def _prompt(request: str, graph: dict, types: dict, pages: list[dict], is_product: bool, photos: Optional[list] = None) -> str:
    kinds = {t: f for t, (f, _, _) in types.items() if t not in ("Logo", "ProductImage")}
    existing = [_node_summary(n) for n in graph["nodes"] if n["type"] not in ("Logo", "ProductImage")]
    topics = [n["name"] for n in bg.nodes(graph, "Topic")]
    page_block = "\n\n".join(f"=== {'WEB PAGE (another site, not the company)' if p['web'] else 'COMPANY PAGE'} {p['url']} ===\n{p['text']}"
                             for p in pages) or "(no pages could be read)"
    return f"""Request from the user: "{request}"

This is the {'product' if is_product else 'company'} graph. Its nodes now:
{json.dumps(existing, ensure_ascii=False)}

Node types and their fields: {json.dumps(kinds)}
- Color role is one of background, text, accent, accent_text; hex is #RRGGBB.
- Use an existing type when the information fits it (a product fact is a Feature, a menu category a Collection,
  a style instruction a Rule, a social account a SocialProfile with network instagram|facebook|x|tiktok|youtube|linkedin|pinterest).
- When it fits none of them, start a branch: a Fact node with fields {{"topic": "<short branch name, e.g. Sustainability>", "text": "<one fact>"}}.
  Existing branches: {json.dumps(topics)}. Reuse one of these names when it fits.
- Keep each Feature or Fact to one short, specific statement, as the page states it.
- Variants (colors, sizes, finishes): look at the option buttons and image alt text under "Details on the page", not just
  the text. A page can show option groups for several products (e.g. a comparison table): use only the group that belongs to
  this {'product' if is_product else 'company'}, judging by its position and the alt text. Give a Variant its hex when the option button shows one.
- Prefer COMPANY PAGES. Use a WEB PAGE only for what the company's pages don't say, and only when it clearly states it.
  Never use a web page for prices, stock or offers.

Pages:
{page_block}

Return {{
  "summary": "one or two sentences: what you found and what you propose",
  "changes": [
    {{"op": "add", "type": "<type>", "fields": {{...}}, "source_url": "<page URL it came from, or empty>",
      "evidence": "<short exact quote copied from that page>", "basis": "page"}},
    {{"op": "update", "node_id": "<existing id>", "fields": {{<only the fields that change>}}, "source_url": "...", "evidence": "...", "basis": "page"}},
    {{"op": "delete", "node_id": "<existing id>", "reason": "why", "basis": "request"}}
  ],
  "not_found": "what the request asked for that the pages don't say (empty if none)"
}}
{_photo_instructions(photos) if photos else ''}
basis is "page" when the value comes from a page (evidence must then be copied exactly from it), or "request" when the
user stated the value themselves (e.g. "the accent color is #0A7B55", "remove the TikTok link").
Do not propose changes the request doesn't ask for, and don't repeat what the graph already has: if a node already says
the same thing in other words, leave it alone (only update a node when the request asks to, or when the page shows it is wrong)."""


def _verify(change: dict, pages: list[dict]) -> bool:
    if change.get("basis") == "request":
        return True
    quote = _norm(change.get("evidence") or "")
    if len(quote) < 6:
        return False
    texts = [p for p in pages if p["url"] == change.get("source_url")] or pages
    return any(quote in _norm(p["full"]) for p in texts)


def _check_change(change: dict, graph: dict, types: dict) -> Optional[str]:
    """Why a proposed change can't be applied, or None."""
    op = change.get("op")
    if op not in ("add", "update", "delete"):
        return "unknown change"
    if op == "add":
        t = change.get("type")
        if t not in types or t in ("Company", "Product", "ProductImage", "Logo", "Topic"):
            return f"can't add a {t} node"
        try:
            graph_edit._clean(t, change.get("fields") or {}, types)
        except graph_edit.EditError as exc:
            return str(exc)
        return None
    n = bg.node(graph, change.get("node_id", ""))
    if not n:
        return "that node isn't in the graph"
    if n["type"] not in types or n["type"] in ("Logo", "ProductImage") and op == "update":
        return f"{n['type']} nodes can't be changed this way"
    if op == "update":
        try:
            graph_edit._clean(n["type"], {**{k: n.get(k) for k in types[n['type']][0]}, **(change.get("fields") or {})}, types)
        except graph_edit.EditError as exc:
            return str(exc)
    return None


async def propose(brand_id: str, product_id: Optional[str], request: str, allow_host: Callable[[str], None],
                  log: Callable[[str], None] = print, web: Optional[bool] = None) -> dict:
    """web: also search the wider web (None: only when the request asks for it, e.g. "do a web search")."""
    request = request.strip()
    if not request:
        raise ValueError("say what to add or change")
    brand = bg.load(brand_id)
    graph, types, _ = graph_edit._graph(brand_id, product_id)
    log("Looking for it on the website")
    web = wants_web(request) if web is None else web
    pages = await _gather_pages(request, brand, graph if product_id else None, web, allow_host, log)
    photos = await _photo_candidates(pages, graph, brand_id, product_id, request, allow_host, log) if product_id and wants_photos(request) else []
    log(f"Reading {len(pages)} page{'s' if len(pages) != 1 else ''} for what you asked")
    content: list[dict] = [{"type": "text", "text": _prompt(request, graph, types, pages, bool(product_id), photos)}]
    for ph in photos:
        content += [{"type": "text", "text": f"Photo {ph['n']} (from {ph['page']}; alt text: {ph['alt'] or 'none'})"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ph["b64"]}}]
    out = await bg._ask_model(MODIFY_SYSTEM, content, max_tokens=4000)
    changes = []
    for c in out.get("changes") or []:
        if not isinstance(c, dict):
            continue
        c["fields"] = {k: str(v) for k, v in (c.get("fields") or {}).items() if v is not None}
        if c.get("type") == "ProductImage" and c.get("op") == "add":
            ph = next((p for p in photos if str(p["n"]) == c["fields"].get("image", "").strip()), None)
            role = c["fields"].get("role") if c["fields"].get("role") in ("hero", "product", "detail", "lifestyle") else "product"
            if not ph or any(x.get("fields", {}).get("file") == ph["file"] for x in changes):
                log("  dropped a proposed photo: not one of the photos found")
                continue
            changes.append({"op": "add", "type": "ProductImage", "fields": {"file": ph["file"], "role": role,
                            "description": c["fields"].get("description") or ph["alt"] or "product photo"},
                            "source_url": ph["page"], "evidence": c.get("evidence") or ph["alt"], "basis": "page", "verified": True,
                            "from_web": not _same_site(ph["page"], brand.get("domain") or ""), "image_url": ph["url"]})
            continue
        problem = _check_change(c, graph, types)
        if problem:
            log(f"  dropped a proposed change: {problem}")
            continue
        c["verified"] = _verify(c, pages)
        c["from_web"] = bool(c.get("source_url")) and not _same_site(c["source_url"], brand.get("domain") or "")
        if c["op"] != "add":
            c["current"] = _node_summary(bg.node(graph, c["node_id"]))
            c["type"] = c["current"]["type"]
        changes.append({k: c.get(k) for k in ("op", "type", "node_id", "fields", "source_url", "evidence", "basis", "reason",
                                               "verified", "from_web", "current") if c.get(k) not in (None, "")})
    proposal = {
        "id": uuid.uuid4().hex[:12], "brand_id": brand_id, "product_id": product_id, "request": request,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "summary": out.get("summary", ""), "not_found": out.get("not_found", ""),
        "pages": [p["url"] for p in pages], "web_pages": [p["url"] for p in pages if p["web"]], "searched_web": web, "topics": [n["name"] for n in bg.nodes(graph, "Topic")],
        "changes": changes, "applied": None,
    }
    (_proposals_dir(brand_id) / f"{proposal['id']}.json").write_text(json.dumps(proposal, indent=2))
    return proposal


# ── applying the ones you confirm ─────────────────────────────────────────────


def load_proposal(brand_id: str, proposal_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", proposal_id):
        raise KeyError(proposal_id)
    return json.loads((_proposals_dir(brand_id) / f"{proposal_id}.json").read_text())


def apply(brand_id: str, product_id: Optional[str], proposal_id: str, picks: list[dict]) -> dict:
    """picks: [{"index": i, "fields": {...optional edits to the proposed values}}]."""
    proposal = load_proposal(brand_id, proposal_id)
    if (proposal.get("product_id") or None) != (product_id or None):
        raise graph_edit.EditError("that proposal is for a different graph")
    if proposal.get("applied") is not None:
        raise graph_edit.EditError("this proposal was already applied; ask again for a fresh one")
    done, failed = [], []
    for pick in picks:
        i = pick.get("index")
        if not isinstance(i, int) or not 0 <= i < len(proposal["changes"]):
            continue
        c = proposal["changes"][i]
        fields = {**c.get("fields", {}), **{k: v for k, v in (pick.get("fields") or {}).items() if k in c.get("fields", {})}}
        source = c.get("source_url") or "your request"
        try:
            if c["op"] == "add" and c["type"] == "ProductImage":
                graph_edit.add_product_image(brand_id, product_id, fields, source=source)
            elif c["op"] == "add":
                graph_edit.add_node(brand_id, product_id, c["type"], fields, source=source)
            elif c["op"] == "update":
                graph_edit.update_node(brand_id, product_id, c["node_id"], fields, source=source)
            else:
                graph_edit.delete_node(brand_id, product_id, c["node_id"])
            done.append(i)
        except (graph_edit.EditError, KeyError, ValueError) as exc:
            failed.append({"index": i, "error": str(exc) or "not found"})
    proposal["applied"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "done": done, "failed": failed}
    (_proposals_dir(brand_id) / f"{proposal_id}.json").write_text(json.dumps(proposal, indent=2))
    return proposal["applied"]
