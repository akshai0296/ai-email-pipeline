"""Hand edits to brand and product graphs.

Change a node's fields, delete it, add one, or upload an official logo. Every
node touched here is marked ``edited: true`` (and keeps ``source: "edited by
you"``), and rebuilding a graph from the website keeps those nodes, so a fix
made by hand survives "Rebuild". Products can carry their own Logo, Color, Font
and Voice nodes; when they do, they override the brand's in emails about that
product.
"""

from __future__ import annotations

import io
import re
import uuid
from typing import Optional

import brand_graph as bg
import product_graph as pg
from bootstrap import STORAGE_DIR, LocalStorageAdapter

# type -> (editable fields, relation from the center node, single-instance id or None)
BRAND_TYPES = {
    "Rule": (["text"], "FOLLOWS", None),
    "Collection": (["name", "url"], "HAS_COLLECTION", None),
    "Link": (["kind", "url"], "HAS_LINK", None),
    "SocialProfile": (["network", "url"], "ON_SOCIAL", None),
    "Color": (["role", "hex"], "HAS_COLOR", "color:{role}"),
    "Font": (["name"], "USES_FONT", "font:primary"),
    "Voice": (["text"], "HAS_VOICE", "voice"),
    "Address": (["text"], "LOCATED_AT", "address"),
    "Company": (["name", "legal_name", "tagline", "description"], None, "company"),
    "Logo": (["background"], "HAS_LOGO", None),
    # A branch of its own: a Topic ("Sustainability") with Facts under it.
    "Topic": (["name"], "HAS_TOPIC", None),
    "Fact": (["topic", "text"], "INCLUDES", None),
}
PRODUCT_TYPES = {
    "Feature": (["text"], "HAS_FEATURE", None),
    "Variant": (["name", "hex"], "COMES_IN", None),
    "Offer": (["price", "currency", "availability"], "HAS_OFFER", "offer"),
    "Link": (["kind", "url"], "HAS_PAGE", None),
    "Product": (["name", "description", "category"], None, "product"),
    "ProductImage": (["role", "description"], "HAS_IMAGE", None),
    # A product's own identity, overriding the brand's in emails about it:
    "Color": (["role", "hex"], "HAS_COLOR", "color:{role}"),
    "Font": (["name"], "USES_FONT", "font:primary"),
    "Voice": (["text"], "HAS_VOICE", "voice"),
    "Logo": (["background"], "HAS_LOGO", None),
    "Topic": (["name"], "HAS_TOPIC", None),
    "Fact": (["topic", "text"], "INCLUDES", None),
}
COLOR_ROLES = ("background", "text", "accent", "accent_text")


class EditError(ValueError):
    pass


def _graph(brand_id: str, product_id: Optional[str]) -> tuple[dict, dict, str]:
    if product_id:
        return pg.load(brand_id, product_id), PRODUCT_TYPES, "product"
    return bg.load(brand_id), BRAND_TYPES, "company"


def _save(graph: dict, product_id: Optional[str]) -> None:
    (pg.save if product_id else bg.save)(graph)


def _clean(type_: str, fields: dict, types: dict) -> dict:
    allowed = types[type_][0]
    out = {k: str(v).strip() for k, v in fields.items() if k in allowed and v is not None}
    if "hex" in out and out["hex"]:
        if not re.fullmatch(r"#?[0-9A-Fa-f]{6}", out["hex"]):
            raise EditError("colors are 6-digit hex, like #1A1A1A")
        out["hex"] = "#" + out["hex"].lstrip("#").upper()
    if "url" in out and out["url"] and not out["url"].startswith(("https://", "http://")):
        raise EditError("links must start with https://")
    if type_ == "Color" and out.get("role") not in COLOR_ROLES:
        raise EditError(f"color role is one of {', '.join(COLOR_ROLES)}")
    if type_ == "Logo" and out.get("background") not in (None, "light", "dark", "any"):
        raise EditError("logo background is light, dark or any")
    return out


def set_logo_mode(brand_id: str, product_id: str, mode: str) -> dict:
    graph = pg.load(brand_id, product_id)
    if mode not in pg.LOGO_MODES:
        raise EditError(f"logo mode is one of {', '.join(pg.LOGO_MODES)}")
    if mode != "brand" and not bg.nodes(graph, "Logo"):
        raise EditError("upload a logo for this product first (on its graph page)")
    graph["logo_mode"] = mode
    pg.save(graph)
    return pg.values(graph)


def topic_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    if not slug:
        raise EditError("give the branch a name")
    return f"topic:{slug}"


def _ensure_topic(graph: dict, name: str, center: str, source: str) -> str:
    """The Topic node a Fact hangs from, created (and linked to the center) if it's new."""
    same = [n["id"] for n in bg.nodes(graph, "Topic") if n.get("name", "").strip().lower() == name.strip().lower()]
    tid = same[0] if same else topic_id(name)  # a renamed branch keeps its first id
    if not bg.node(graph, tid):
        graph["nodes"].append({"id": tid, "type": "Topic", "name": name.strip(), "edited": True, "source": source})
        graph["edges"].append({"from": center, "rel": "HAS_TOPIC", "to": tid})
        graph["deleted_by_user"] = [d for d in graph.get("deleted_by_user", []) if d != tid]
    return tid


def _attach_fact(graph: dict, node: dict, center: str, source: str) -> None:
    tid = _ensure_topic(graph, node["topic"], center, source)
    graph["edges"] = [e for e in graph["edges"] if e["to"] != node["id"]] + [{"from": tid, "rel": "INCLUDES", "to": node["id"]}]


def update_node(brand_id: str, product_id: Optional[str], node_id: str, fields: dict, source: str = "edited by you") -> dict:
    graph, types, center = _graph(brand_id, product_id)
    node = bg.node(graph, node_id)
    if not node:
        raise KeyError(node_id)
    if node["type"] not in types:
        raise EditError(f"{node['type']} nodes can't be edited here")
    node.update(_clean(node["type"], fields, types), edited=True, source=source)
    if node["type"] == "Fact" and node.get("topic"):
        _attach_fact(graph, node, center, source)  # moved to another branch
    if node["type"] == "Topic":
        for f in bg.nodes(graph, "Fact"):
            if any(e["from"] == node_id and e["to"] == f["id"] for e in graph["edges"]):
                f["topic"] = node["name"]
    _save(graph, product_id)
    return node


def delete_node(brand_id: str, product_id: Optional[str], node_id: str) -> None:
    graph, _, center = _graph(brand_id, product_id)
    if node_id in (center, "brand"):
        raise EditError("the center node can't be deleted")
    if not bg.node(graph, node_id):
        raise KeyError(node_id)
    gone = {node_id}
    if bg.node(graph, node_id)["type"] == "Topic":  # a branch goes with its facts
        gone |= {e["to"] for e in graph["edges"] if e["from"] == node_id and e["rel"] == "INCLUDES"}
    keys = {bg.node_key(n) for n in graph["nodes"] if n["id"] in gone} - {None}
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] not in gone]
    graph["edges"] = [e for e in graph["edges"] if not gone & {e["from"], e["to"]}]
    graph.setdefault("deleted_by_user", []).extend(sorted(gone))  # a rebuild won't bring them back,
    graph["deleted_keys"] = sorted(set(graph.get("deleted_keys", [])) | keys)  # even under a new id
    _save(graph, product_id)


def add_node(brand_id: str, product_id: Optional[str], type_: str, fields: dict, source: str = "edited by you") -> dict:
    graph, types, center = _graph(brand_id, product_id)
    if type_ not in types or type_ in ("Company", "Product", "ProductImage", "Logo"):
        raise EditError(f"can't add a {type_} node here")
    clean = _clean(type_, fields, types)
    missing = [f for f in types[type_][0] if not clean.get(f)] if type_ not in ("Offer", "Variant") else ([] if clean.get("price") or clean.get("name") else ["value"])
    if missing:
        raise EditError(f"fill in {', '.join(missing)}")
    single = types[type_][2]
    if type_ == "Topic":
        tid = _ensure_topic(graph, clean["name"], center, source)
        _save(graph, product_id)
        return bg.node(graph, tid)
    node_id = single.format(**clean) if single else f"{type_.lower()}:yours-{uuid.uuid4().hex[:6]}"
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != node_id]
    graph["edges"] = [e for e in graph["edges"] if e["to"] != node_id]
    node = {"id": node_id, "type": type_, **clean, "edited": True, "source": source}
    graph["nodes"].append(node)
    if type_ == "Fact":
        _attach_fact(graph, node, center, source)
    else:
        graph["edges"].append({"from": center, "rel": types[type_][1], "to": node_id})
    graph["deleted_by_user"] = [d for d in graph.get("deleted_by_user", []) if d != node_id]
    graph["deleted_keys"] = [k for k in graph.get("deleted_keys", []) if k != bg.node_key(node)]
    _save(graph, product_id)
    return node


def cut_off_sides(img) -> list[str]:
    """Edges where the logo looks cut off: ink touches that edge while the opposite edge has a clear margin.

    Logo files are usually trimmed tight, so ink touching every edge is normal. Ink on one
    edge with empty space on the other side is what a cropped or too-narrow export looks like.
    """
    from collections import Counter

    rgba = img.convert("RGBA")
    rgba.thumbnail((600, 600))
    w, h = rgba.size
    px = rgba.load()
    edge_px = [px[x, y] for x in range(w) for y in (0, h - 1)] + [px[x, y] for y in range(h) for x in (0, w - 1)]
    bg_px = Counter((p[0] // 16, p[1] // 16, p[2] // 16, p[3] // 64) for p in edge_px).most_common(1)[0][0]

    def ink(p) -> bool:
        if (p[3] < 128) != (bg_px[3] < 2):
            return True
        return p[3] >= 128 and sum(abs(p[i] // 16 - bg_px[i]) for i in range(3)) > 4

    def touches(points) -> bool:
        points = list(points)
        return sum(ink(px[x, y]) for x, y in points) > max(2, len(points) * 0.02)

    def margin(lines) -> int:
        n = 0
        for line in lines:
            if any(ink(px[x, y]) for x, y in line):
                break
            n += 1
        return n

    cols = lambda xs: ([(x, y) for y in range(h)] for x in xs)  # noqa: E731
    rows = lambda ys: ([(x, y) for x in range(w)] for y in ys)  # noqa: E731
    sides = {
        "left": (touches((0, y) for y in range(h)), margin(cols(range(w)))),
        "right": (touches((w - 1, y) for y in range(h)), margin(cols(range(w - 1, -1, -1)))),
        "top": (touches((x, 0) for x in range(w)), margin(rows(range(h)))),
        "bottom": (touches((x, h - 1) for x in range(w)), margin(rows(range(h - 1, -1, -1)))),
    }
    opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
    out = []
    for side, (touch, _) in sides.items():
        other_touch, other_margin = sides[opposite[side]]
        span = w if side in ("left", "right") else h
        if touch and not other_touch and other_margin >= max(3, span * 0.02):
            out.append(side)
    return out


def add_product_image(brand_id: str, product_id: str, fields: dict, source: str) -> dict:
    """Add a photo found by Modify (already downloaded and staged) to the product graph.

    It goes right after the hero, so emails (which use the first few photos) pick it up; a new
    "hero" goes first and the old hero becomes a product photo.
    """
    from PIL import Image

    graph = pg.load(brand_id, product_id)
    staged = (STORAGE_DIR / fields["file"]).resolve()
    folder = f"brands/{brand_id}/products/{product_id}"
    if not str(fields["file"]).startswith(f"{folder}/proposed/") or not staged.exists():
        raise EditError("that photo is no longer available; ask again")
    used = [int(m.group(1)) for n in bg.nodes(graph, "ProductImage") for m in [re.search(r"image-(\d+)\.", n.get("file", ""))] if m]
    key = f"{folder}/image-{max(used, default=-1) + 1}{staged.suffix}"
    LocalStorageAdapter().upload_file_object(key, staged.read_bytes())
    with Image.open(staged) as im:
        width, height = im.size
    role = fields.get("role") if fields.get("role") in ("hero", "product", "detail", "lifestyle") else "product"
    node = {"id": f"image:yours-{uuid.uuid4().hex[:6]}", "type": "ProductImage", "file": key, "role": role,
            "description": str(fields.get("description", "")).strip(), "width": width, "height": height,
            "source": source, "edited": True}
    photos = [i for i, n in enumerate(graph["nodes"]) if n.get("type") == "ProductImage"]
    if role == "hero":
        for i in photos:
            if graph["nodes"][i].get("role") == "hero":
                graph["nodes"][i].update(role="product", edited=True)
        at = photos[0] if photos else len(graph["nodes"])
    else:
        at = photos[0] + 1 if photos else len(graph["nodes"])
    graph["nodes"].insert(at, node)
    graph["edges"].append({"from": "product", "rel": "HAS_IMAGE", "to": node["id"]})
    pg.save(graph)
    return node


async def upload_logo(brand_id: str, product_id: Optional[str], data: bytes, filename: str,
                      source: str = "uploaded by you") -> dict:
    """Make an uploaded file the primary logo (plus a reversed copy for one-color marks).

    Returns {"logos": [...], "warning": "..."}; the warning flags a logo that looks cut off.
    """
    graph, _, center = _graph(brand_id, product_id)
    is_svg = filename.lower().endswith(".svg") or data.lstrip()[:5] in (b"<svg ", b"<?xml")
    if is_svg:
        png = (await bg._rasterize_svgs([data.decode(errors="replace")]))[0]
        if not png:
            raise EditError("couldn't draw that SVG")
        data = png
    try:
        from PIL import Image

        original = Image.open(io.BytesIO(data))
        original.load()
        cut = cut_off_sides(original)
        img = bg._to_png(data)
    except Exception:
        raise EditError("that file isn't an image (use PNG, JPG, WebP or SVG)")
    if img.width < 32 or img.height < 16:
        raise EditError("that image is too small to be a logo")
    folder = f"brands/{brand_id}" + (f"/products/{product_id}" if product_id else "")
    key = f"{folder}/logo-primary.png"
    buf = io.BytesIO()
    img.save(buf, "PNG")
    LocalStorageAdapter().upload_file_object(key, buf.getvalue())
    graph["nodes"] = [n for n in graph["nodes"] if n.get("type") != "Logo"]
    graph["edges"] = [e for e in graph["edges"] if not e["to"].startswith("logo:")]
    node = {"id": "logo:primary", "type": "Logo", "variant": "primary", "file": key, "width": img.width, "height": img.height,
            **bg._analyze(img), "found_as": "upload", "edited": True, "source": source}
    graph["nodes"].append(node)
    graph["edges"].append({"from": center, "rel": "HAS_LOGO", "to": "logo:primary"})
    if node.get("mono") and node.get("transparent"):
        bg.add_mono_variants(graph["nodes"], graph["edges"], brand_id if not product_id else f"{brand_id}/products/{product_id}")
        for n in graph["nodes"]:
            if n["type"] == "Logo":
                n["edited"] = True
        for e in graph["edges"]:
            if e["to"] == "logo:reversed":
                e["from"] = center
    _save(graph, product_id)
    warning = (f"This logo looks cut off at the {' and '.join(cut)} edge: the artwork touches that edge while the opposite "
               "side has empty space. Check the file and upload the full logo if part of it is missing.") if cut else ""
    return {"logos": bg.nodes(graph, "Logo"), "warning": warning}
