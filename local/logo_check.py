"""After rendering: make sure the logo is legible on whatever the designer put behind it.

The designer places the logo from instructions, so it can end up on a background
it disappears into (light grey lettering on the white page around a dark email).
This step renders the finished HTML, reads the real pixels behind each logo and
measures contrast along the logo's edges. When too little of the logo stands out:

  1. swap in another variant of the logo from the brand graph that reads on
     that background (the white "reversed" mark on a dark band), or
  2. paint the logo's own row in a color the logo needs (a dark band for a light
     logo), then check again.

No model calls: a headless browser and pixel arithmetic, well under a second.
"""

from __future__ import annotations

import io
import re
from typing import Callable

from bs4 import BeautifulSoup
from PIL import Image

import brand_graph as bg
from bootstrap import STORAGE_DIR

MIN_EDGE_CONTRAST = 2.0  # an edge pixel this far from what is behind it counts as visible
MIN_VISIBLE_SHARE = 0.75  # this share of the logo's edge must be visible


def _logo_pixels(node: dict, size: tuple[int, int]) -> Image.Image:
    img = Image.open(STORAGE_DIR / node["file"]).convert("RGBA")
    return img.resize(size, Image.LANCZOS)


def legibility(logo: Image.Image, behind: Image.Image) -> float:
    """Share of the logo's edge pixels that contrast with the pixels right behind them."""
    w, h = logo.size
    behind = behind.convert("RGB").resize((w, h))
    alpha = logo.getchannel("A").load()
    lp, bp = logo.load(), behind.load()
    edge = visible = 0
    for y in range(h):
        for x in range(w):
            if alpha[x, y] <= 128:
                continue
            if 0 < x < w - 1 and 0 < y < h - 1 and min(alpha[x - 1, y], alpha[x + 1, y], alpha[x, y - 1], alpha[x, y + 1]) > 128:
                continue  # interior pixel: what matters is where the mark meets the background
            edge += 1
            if bg._contrast("#%02X%02X%02X" % lp[x, y][:3], "#%02X%02X%02X" % bp[x, y]) >= MIN_EDGE_CONTRAST:
                visible += 1
    return visible / edge if edge else 1.0


async def _measure(page, html: str, url: str) -> list[dict]:
    """For each <img> showing this URL: its box and a screenshot of what is behind it."""
    await page.set_content(html, wait_until="load")
    await page.wait_for_timeout(300)
    boxes = await page.evaluate(
        """(u) => [...document.images].filter((i) => i.src === u && i.getBoundingClientRect().width > 0).map((i, k) => {
              i.setAttribute('data-logo-check', String(k));
              const r = i.getBoundingClientRect();
              return { k, x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height };
            })""",
        url,
    )
    out = []
    for b in boxes:
        sel = f'img[data-logo-check="{b["k"]}"]'
        await page.eval_on_selector(sel, "e => e.style.visibility = 'hidden'")
        shot = await page.screenshot(clip={"x": b["x"], "y": b["y"], "width": b["w"], "height": b["h"]}, full_page=True)
        await page.eval_on_selector(sel, "e => e.style.visibility = ''")
        out.append({**b, "behind": Image.open(io.BytesIO(shot))})
    return out


def _band_color(node: dict, brand_background: str) -> str:
    if node.get("tile_hex"):
        return node["tile_hex"]
    wants_dark = node.get("background") == "dark"
    if wants_dark == (bg._luminance(brand_background) < 0.35):
        return brand_background
    return "#111111" if wants_dark else "#FFFFFF"


_BG_DECL = re.compile(r"\s*background(?:-color)?\s*:[^;]*;?", re.I)


def _recolor_tag(tag_src: str, color: str) -> str:
    """One opening tag with its background set to color (style and bgcolor), nothing else touched."""
    m = re.search(r'\sstyle\s*=\s*(["\'])(.*?)\1', tag_src, re.S)
    if m:
        style = _BG_DECL.sub("", m.group(2)).strip().rstrip(";")
        style = f"{style};background:{color};background-color:{color}" if style else f"background:{color};background-color:{color}"
        tag_src = tag_src[: m.start()] + f' style="{style}"' + tag_src[m.end():]
    else:
        tag_src = tag_src[:-1].rstrip("/").rstrip() + f' style="background:{color};background-color:{color}"' + (" />" if tag_src.endswith("/>") else ">")
    if re.search(r"^<(td|table)\b", tag_src, re.I):
        if re.search(r"\sbgcolor\s*=", tag_src, re.I):
            tag_src = re.sub(r'\sbgcolor\s*=\s*(["\']?)[^"\'\s>]*\1', f' bgcolor="{color}"', tag_src, flags=re.I)
        else:
            tag_src = re.sub(r"^<(td|table)\b", lambda mm: f'<{mm.group(1)} bgcolor="{color}"', tag_src, flags=re.I)
    return tag_src


def _paint_band(html: str, url: str, color: str) -> str:
    """Color the block around the logo that holds nothing else (its header row).

    The block and everything inside it that sets its own background (MJML repeats
    a section's color on an inner table) get the new color. Only those opening tags
    change; every other byte of the email stays as it was.
    """
    soup = BeautifulSoup(html, "html.parser")
    line_start = [0]
    for line in html.splitlines(keepends=True):
        line_start.append(line_start[-1] + len(line))
    starts: set[int] = set()
    for img in soup.find_all("img", src=url):
        target = None
        for anc in img.parents:
            if anc.name in ("body", "html", "[document]"):
                break
            if anc.get_text(strip=True) or len(anc.find_all("img")) > 1:
                break
            if anc.name in ("td", "div", "table"):
                target = anc
        target = target or img.parent
        painted = [target] + [
            t for t in target.find_all(["td", "table", "div"])
            if t.get("bgcolor") or re.search(r"background", t.get("style", ""), re.I)
        ]
        starts |= {line_start[t.sourceline - 1] + t.sourcepos for t in painted if t.sourceline}
    for start in sorted(starts, reverse=True):
        end = _tag_end(html, start)
        html = html[:start] + _recolor_tag(html[start:end], color) + html[end:]
    return html


def _tag_end(html: str, start: int) -> int:
    """Index just past the '>' closing the opening tag at start (quote-aware)."""
    quote = None
    for i in range(start, len(html)):
        c = html[i]
        if quote:
            quote = None if c == quote else quote
        elif c in "\"'":
            quote = c
        elif c == ">":
            return i + 1
    raise ValueError("unterminated tag")


async def check_and_fix(html: str, graph: dict, brand_background: str, log: Callable[[str], None] = print) -> tuple[str, list[str]]:
    """Return (html, notes). The html has the logo swapped or given a band if it wasn't legible."""
    from playwright.async_api import async_playwright

    # Logos are matched by their storage path, whatever host and port served them.
    logos: dict[str, dict] = {}
    for n in bg.nodes(graph, "Logo"):
        for found in re.findall(r'https?://[^"\'\s)]*?/files/' + re.escape(n["file"]), html):
            logos[found] = n
    used = list(logos)
    origin = used[0].split("/files/")[0] if used else ""
    for n in bg.nodes(graph, "Logo"):
        logos.setdefault(f"{origin}/files/{n['file']}", n)
    notes: list[str] = []
    if not used:
        return html, ["Logo check: no brand logo found in the email"]
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 700, "height": 1000})
            for current in used:
                node = logos[current]
                if node.get("transparent") is False:
                    continue  # it sits on its own tile, so it carries its own background
                for spot in await _measure(page, html, current):
                    size = (max(1, round(spot["w"])), max(1, round(spot["h"])))
                    score = legibility(_logo_pixels(node, size), spot["behind"])
                    if score >= MIN_VISIBLE_SHARE:
                        notes.append(f"Logo check: {node['id']} is legible ({score:.0%} of its edge stands out)")
                        continue
                    log(f"Logo hard to see ({score:.0%} of its edge stands out); fixing")
                    # 1. another variant that reads on this exact background
                    best = max(
                        ((legibility(_logo_pixels(v, size), spot["behind"]), u, v) for u, v in logos.items() if u != current and v.get("transparent", True)),
                        default=None, key=lambda t: t[0],
                    )
                    if best and best[0] >= MIN_VISIBLE_SHARE:
                        html = html.replace(current, best[1])
                        notes.append(f"Logo check: swapped {node['id']} for {best[2]['id']} ({score:.0%} → {best[0]:.0%} legible)")
                        break
                    # 2. give the logo a band in a color it reads on, then check again
                    color = _band_color(node, brand_background)
                    fixed = _paint_band(html, current, color)
                    after = await _measure(page, fixed, current)
                    new = min((legibility(_logo_pixels(node, size), s["behind"]) for s in after), default=0)
                    if new >= MIN_VISIBLE_SHARE:
                        html = fixed
                        notes.append(f"Logo check: put {node['id']} on a {color} band ({score:.0%} → {new:.0%} legible)")
                    else:
                        notes.append(f"Logo check: {node['id']} still hard to see after the fix ({new:.0%}); check the header")
                    break
        finally:
            await browser.close()
    return html, notes
