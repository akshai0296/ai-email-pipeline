"""Local web UI for the email pipeline.  Run:  .venv/bin/python local/app.py

Each generation runs in its own worker process (worker.py) with the model
backend chosen in the form: Claude Code login, ChatGPT login, or API keys.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402
import brand_graph  # noqa: E402
import email_doc  # noqa: E402
import graph_edit  # noqa: E402
import model_choice  # noqa: E402
import product_graph  # noqa: E402
import versions  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import export  # noqa: E402

app = FastAPI(title="Email pipeline (local)")
app.mount("/files", StaticFiles(directory=bootstrap.STORAGE_DIR), name="files")

WORKER = Path(__file__).resolve().parent / "worker.py"
BRAND_WORKER = Path(__file__).resolve().parent / "brand_worker.py"
JOBS_DIR = bootstrap.STORAGE_DIR / "jobs"
JOBS: dict[str, dict] = {}
BRAND_JOBS: dict[str, dict] = {}
_brand_lock = asyncio.Lock()


NO_STORE = {"Cache-Control": "no-store"}  # the UI changes between versions; never serve a stale copy


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html", headers=NO_STORE)


@app.get("/api/status")
def status(refresh: bool = False) -> dict:
    backends = bootstrap.all_backend_status(max_age_s=0 if refresh else 60)
    ready = [b["id"] for b in backends if b["ready"]]
    default = bootstrap.LLM_BACKEND if bootstrap.LLM_BACKEND in ready else (ready[0] if ready else None)
    running = sum(1 for j in JOBS.values() if j["state"] == "running")
    queued = sum(1 for j in JOBS.values() if j["state"] == "queued")
    return {"backends": backends, "default": default, "busy": running >= RUN_SLOTS, "running": running,
            "queued": queued, "slots": RUN_SLOTS,
            "model_options": model_choice.options(), "model_settings": {b["id"]: model_choice.get(b["id"]) for b in backends}}


class ModelSettings(BaseModel):
    backend: str
    model: str = ""
    effort: str = ""


@app.put("/api/model-settings")
def put_model_settings(req: ModelSettings) -> dict:
    """The model and effort a model-access mode uses for everything it runs (empty: the pipeline's own choice)."""
    try:
        saved = model_choice.save(req.backend, req.model, req.effort)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {**saved, "label": model_choice.label(req.backend)}


def _llm_env(backend: str, **extra: str) -> dict[str, str]:
    """A worker's environment: the model access, plus the model and effort chosen for it."""
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_MODEL", "CHATGPT_MODEL", "FORCE_MODEL", "LLM_EFFORT")}
    return {**env, "LLM_BACKEND": backend, **model_choice.env(backend), **extra}


def _ready_backend(backend: str) -> str:
    statuses = {b["id"]: b for b in bootstrap.all_backend_status()}
    backend = backend or bootstrap.LLM_BACKEND
    if backend not in statuses:
        raise HTTPException(400, f"unknown backend {backend!r}")
    if not statuses[backend]["ready"]:
        raise HTTPException(400, f"{statuses[backend]['label']}: {statuses[backend]['detail']}")
    return backend


# ── company knowledge graphs ──────────────────────────────────────────────────


class BrandSearch(BaseModel):
    query: str
    backend: str = ""
    refresh: bool = False


@app.get("/api/brands")
def list_brands() -> list[dict]:
    return brand_graph.list_graphs()


@app.get("/static/modify.js")
def modify_script() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "modify.js", media_type="text/javascript", headers=NO_STORE)


@app.get("/graph/{brand_id}", response_class=HTMLResponse)
@app.get("/graph/{brand_id}/{product_id}", response_class=HTMLResponse)
@app.get("/graph/{brand_id}/collection/{collection}", response_class=HTMLResponse)
def graph_page(brand_id: str, product_id: str = "", collection: str = "") -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "graph.html", headers=NO_STORE)


@app.get("/api/brands/{brand_id}/graph")
def get_brand_graph(brand_id: str) -> dict:
    """The raw graph, with a viewable URL on each logo node."""
    try:
        graph = brand_graph.load(brand_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "unknown brand")
    url = bootstrap.LocalStorageAdapter().url
    for n in graph["nodes"]:
        if n.get("file"):
            n["file_url"] = url(n["file"])
    return graph


@app.get("/api/brands/{brand_id}")
def get_brand(brand_id: str) -> dict:
    try:
        return brand_graph.form_values(brand_graph.load(brand_id))
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "unknown brand")


@app.post("/api/brands")
async def search_brand(req: BrandSearch) -> dict:
    """Reuse the saved graph for this brand, or start building one."""
    query = req.query.strip()
    if not query:
        raise HTTPException(400, "type a brand name or website")
    if not req.refresh:
        found = brand_graph.find(query)
        if found:
            brand_graph.remember_alias(found, query)
            return {"status": "found", "brand": brand_graph.form_values(found)}
    return _start_graph_job([query], query, req.backend)


def _start_graph_job(args: list[str], query: str, backend: str) -> dict:
    """Run brand_worker.py with these args, or join an identical build already running."""
    for job in BRAND_JOBS.values():
        if job["state"] in ("queued", "running") and [a.lower() for a in job["args"]] == [a.lower() for a in args]:
            return {"status": "building", "job_id": job["id"]}
    backend = _ready_backend(backend)
    job_id = uuid.uuid4().hex[:12]
    BRAND_JOBS[job_id] = {
        "id": job_id, "query": query, "args": args, "state": "queued", "stage": "Waiting for another search to finish",
        "log": [], "brand": None, "product": None, "error": None, "started": time.time(),
    }
    asyncio.create_task(_build_brand(job_id, backend))
    return {"status": "building", "job_id": job_id}


class BriefRequest(BaseModel):
    mode: str = "write"  # write | improve | sequence
    template: str = ""
    sequence: str = ""
    text: str = ""
    audience: str = "Consumer"
    audience_description: str = ""
    brand_id: str = ""
    product_id: str = ""
    uploaded: list[dict] = []
    backend: str = ""


@app.get("/api/brief/templates")
def brief_templates() -> dict:
    import brief_writer  # imports the backend; only needed for its two tables

    return {
        "templates": [{"id": k, "name": n, "layout": l} for k, (n, l) in brief_writer.TEMPLATES.items()],
        # The checklist the meter shows, so it matches what the writer is checked against.
        "checks": [{"name": n, "pattern": p, "tip": t} for n, p, t in brief_writer.CHECKS],
        "length": list(brief_writer.LENGTH),
        "sequences": [{"id": k, "name": n, "emails": [{"name": a, "send": b, "job": c} for a, b, c in steps]}
                      for k, (n, steps) in brief_writer.SEQUENCES.items()],
    }


@app.post("/api/brief")
async def write_brief(req: BriefRequest) -> dict:
    """Draft a brief, or improve the user's, from the saved graphs. Takes 10-40 seconds."""
    if req.mode not in ("write", "improve", "sequence"):
        raise HTTPException(400, "mode is write, improve or sequence")
    if req.mode == "improve" and not req.text.strip():
        raise HTTPException(400, "write something to improve first, or use Write it for me")
    backend = _ready_backend(req.backend)
    payload = req.model_dump(exclude={"backend", "sequence", "template"} if req.mode == "sequence" else {"backend", "sequence"})
    payload["brand_id"] = payload["brand_id"] or None
    payload["product_id"] = (payload["product_id"] or None) if payload["brand_id"] else None
    flag = "--brief"
    if req.mode == "sequence":
        payload = {**{k: v for k, v in payload.items() if k != "mode"}, "kind": req.sequence}
        flag = "--sequence"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(BRAND_WORKER), flag, json.dumps(payload),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=str(bootstrap.ROOT_DIR), env=_llm_env(backend), limit=8 * 1024 * 1024,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    for line in out.decode(errors="replace").splitlines():
        if line.startswith("@@RESULT "):
            return json.loads(line[9:])
        if line.startswith("@@ERROR "):
            raise HTTPException(500, json.loads(line[8:])["error"])
    raise HTTPException(500, f"brief writer exited with code {proc.returncode}")


# ── editing graphs by hand ────────────────────────────────────────────────────


class NodeEdit(BaseModel):
    fields: dict = {}
    type: str = ""


def _graph_call(fn, *args):
    try:
        return fn(*args)
    except graph_edit.EditError as exc:
        raise HTTPException(400, str(exc))
    except (KeyError, FileNotFoundError):
        raise HTTPException(404, "not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/brands/{brand_id}/nodes/{node_id}")
@app.patch("/api/brands/{brand_id}/products/{product_id}/nodes/{node_id}")
def edit_node(brand_id: str, node_id: str, req: NodeEdit, product_id: str = "") -> dict:
    return _graph_call(graph_edit.update_node, brand_id, product_id or None, node_id, req.fields)


@app.delete("/api/brands/{brand_id}/nodes/{node_id}")
@app.delete("/api/brands/{brand_id}/products/{product_id}/nodes/{node_id}")
def remove_node(brand_id: str, node_id: str, product_id: str = "") -> dict:
    _graph_call(graph_edit.delete_node, brand_id, product_id or None, node_id)
    return {"deleted": node_id}


@app.post("/api/brands/{brand_id}/nodes")
@app.post("/api/brands/{brand_id}/products/{product_id}/nodes")
def create_node(brand_id: str, req: NodeEdit, product_id: str = "") -> dict:
    return _graph_call(graph_edit.add_node, brand_id, product_id or None, req.type, req.fields)


@app.post("/api/brands/{brand_id}/logo")
@app.post("/api/brands/{brand_id}/products/{product_id}/logo")
async def upload_logo(brand_id: str, file: UploadFile = File(...), product_id: str = "") -> dict:
    data = await file.read()
    if len(data) > 10_000_000:
        raise HTTPException(400, "logo files are limited to 10 MB")
    try:
        return await graph_edit.upload_logo(brand_id, product_id or None, data, file.filename or "logo.png")
    except graph_edit.EditError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError:
        raise HTTPException(404, "not found")


class ProductSettings(BaseModel):
    logo_mode: str


@app.patch("/api/brands/{brand_id}/products/{product_id}/settings")
def product_settings(brand_id: str, product_id: str, req: ProductSettings) -> dict:
    """How the product's own logo is used: brand (default), both, or product."""
    return _graph_call(graph_edit.set_logo_mode, brand_id, product_id, req.logo_mode)


async def _worker_result(args: list[str], backend: str, timeout: float = 180) -> dict:
    """Run brand_worker.py once and return its @@RESULT (raising its @@ERROR as a 4xx/5xx)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(BRAND_WORKER), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=str(bootstrap.ROOT_DIR), env=_llm_env(backend), limit=8 * 1024 * 1024,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, "that took too long; try again")
    for line in out.decode(errors="replace").splitlines():
        if line.startswith("@@RESULT "):
            return json.loads(line[9:])
        if line.startswith("@@ERROR "):
            err = json.loads(line[8:])
            raise HTTPException(400 if not err.get("trace") else 502, err["error"])
    raise HTTPException(502, f"worker exited with code {proc.returncode}")


class ImageSearch(BaseModel):
    source: str = "web"
    query: str = ""
    brand_id: str = ""
    product_id: str = ""
    backend: str = ""


@app.get("/api/images/sources")
def image_sources() -> list[dict]:
    import image_search

    return image_search.sources()


@app.post("/api/images/search")
async def search_images(req: ImageSearch) -> dict:
    """Find images on the web, the brand's site or Openverse (10-60 seconds)."""
    if len(req.query) > 200:
        raise HTTPException(400, "keep the search under 200 characters")
    backend = _ready_backend(req.backend) if req.source == "web" else (req.backend or bootstrap.LLM_BACKEND)
    payload = req.model_dump(exclude={"backend"})
    payload["brand_id"] = payload["brand_id"] or None
    payload["product_id"] = payload["product_id"] or None
    return await _worker_result(["--images", json.dumps(payload)], backend)


@app.post("/api/images/describe")
async def describe_image(file: UploadFile = File(...), brand_id: str = Form(""), product_id: str = Form(""), backend: str = Form("")) -> dict:
    """One line on what an uploaded image shows, and the role it suits (5-15 seconds)."""
    data = await file.read()
    if len(data) > 20_000_000:
        raise HTTPException(400, "images are limited to 20 MB")
    data, name = _email_safe_image(data, file.filename or "image.png")
    path = bootstrap.STORAGE_DIR / "uploads" / "staged" / f"describe-{uuid.uuid4().hex[:8]}{Path(name).suffix or '.png'}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    try:
        req = {"path": str(path), "brand_id": brand_id or None, "product_id": (product_id or None) if brand_id else None}
        return await _worker_result(["--describe-image", json.dumps(req)], _ready_backend(backend), timeout=120)
    finally:
        path.unlink(missing_ok=True)


class ImagePick(BaseModel):
    url: str = ""
    local: str = ""
    title: str = ""
    credit: str = ""


@app.post("/api/images/fetch")
async def fetch_image(req: ImagePick) -> dict:
    """Download a picked image and stage it; the page then adds it like a dropped file."""
    if not req.local and not req.url.startswith(("https://", "http://")):
        raise HTTPException(400, "give the image's link")
    return await _worker_result(["--fetch-image", req.model_dump_json()], bootstrap.LLM_BACKEND, timeout=90)


class ModifyRequest(BaseModel):
    request: str
    backend: str = ""
    web: Optional[bool] = None  # also search the web; None: only if the request asks for it


class ModifyApply(BaseModel):
    picks: list[dict] = []


@app.post("/api/brands/{brand_id}/modify")
@app.post("/api/brands/{brand_id}/products/{product_id}/modify")
async def modify_graph(brand_id: str, req: ModifyRequest, product_id: str = "") -> dict:
    """Look up what the request asks for on the brand's site and propose changes. Nothing is saved until you confirm."""
    if not req.request.strip():
        raise HTTPException(400, "say what to add or change")
    if len(req.request) > 1000:
        raise HTTPException(400, "keep the request under 1000 characters")
    try:
        product_graph.load(brand_id, product_id) if product_id else brand_graph.load(brand_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "not found")
    payload = json.dumps({"brand_id": brand_id, "product_id": product_id or None, "request": req.request.strip(), "web": req.web})
    return _start_graph_job(["--modify", payload], f"modify {brand_id}", req.backend)


@app.post("/api/brands/{brand_id}/modify/{proposal_id}/apply")
@app.post("/api/brands/{brand_id}/products/{product_id}/modify/{proposal_id}/apply")
def apply_modification(brand_id: str, proposal_id: str, req: ModifyApply, product_id: str = "") -> dict:
    """Save the proposed changes you ticked (with any edits you made to their values)."""
    import graph_modify

    result = _graph_call(graph_modify.apply, brand_id, product_id or None, proposal_id, req.picks)
    if product_id:
        return {"applied": result, "product": product_graph.values(product_graph.load(brand_id, product_id))}
    return {"applied": result, "brand": brand_graph.form_values(brand_graph.load(brand_id))}


app.mount("/design-assets", StaticFiles(directory=Path(__file__).parent / "design"), name="design-assets")


@app.get("/design/{brand_id}", response_class=HTMLResponse)
def design_page(brand_id: str) -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "design.html", headers=NO_STORE)


@app.get("/api/brands/{brand_id}/design")
def get_design(brand_id: str) -> dict:
    """The brand's design layer, plus the generic layer it builds on."""
    import brand_design
    import design_generic

    try:
        brand = brand_graph.load(brand_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "unknown brand")
    generic = design_generic.load()
    design = brand_design.load(brand_id)
    url = bootstrap.LocalStorageAdapter().url
    for n in design["nodes"]:
        if n.get("thumbnail"):
            n["thumbnail_url"] = url(n["thumbnail"])
    return {
        "brand": brand_graph.summary(brand), "design": design, "tokens": {**design_generic.TOKENS, **brand_design.tokens(design)},
        "has_guide": (brand_graph.BRANDS_DIR / brand_id / "brand-guide.pdf").exists(),
        "generic": {
            "patterns": [{k: p.get(k) for k in ("id", "name", "category", "anatomy", "when_to_use", "avoid_when", "sources")}
                         | {"thumbnail_url": f"/design-assets/{p['thumbnail']}"} for p in generic.get("patterns", [])],
            "recipes": generic.get("recipes", []),
            "constraints": {lvl: sum(c["level"] == lvl for c in generic.get("constraints", [])) for lvl in ("safe", "partial", "avoid")},
            "rule": design_generic.designer_rule(generic) if generic.get("constraints") else "",
            "sources": generic.get("sources", {}), "built": generic.get("built"), "clients_updated": generic.get("constraints_updated"),
        },
    }


@app.post("/api/brands/{brand_id}/design/import")
async def import_design(brand_id: str, request: Request) -> dict:
    """Read the brand's design from its website, its brand guide (JSON {source}) or its past emails
    (multipart: source=emails, files=.eml/.html, urls=JSON list). Returns a job whose result is a proposal to review."""
    try:
        brand_graph.load(brand_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "unknown brand")
    if request.headers.get("content-type", "").startswith("multipart/"):
        form = await request.form()
        source, backend = str(form.get("source", "")), str(form.get("backend", ""))
    else:
        body = await request.json()
        source, backend, form = str(body.get("source", "")), str(body.get("backend", "")), None
    if source == "emails":
        import email_import

        if form is None:
            raise HTTPException(400, "send the emails as a form upload")
        try:
            urls = [u for u in json.loads(str(form.get("urls") or "[]")) if isinstance(u, str)][:20]
        except ValueError:
            raise HTTPException(400, "urls must be a JSON list")
        files = [f for f in form.getlist("files") if hasattr(f, "read")][:20]
        if not files and not urls:
            raise HTTPException(400, "drop some emails or paste a link")
        saved = []
        for f in files:
            data = await f.read()
            if len(data) > 15_000_000:
                raise HTTPException(400, f"{f.filename} is over 15 MB")
            saved.append(email_import.save_upload(brand_id, f.filename or "email.html", data))
        payload = json.dumps({"files": saved, "urls": urls})
        return _start_graph_job(["--design", "emails", brand_id, payload], f"design emails {brand_id} {uuid.uuid4().hex[:6]}", backend)
    if source not in ("website", "guide"):
        raise HTTPException(400, "source is website, guide or emails")
    if source == "guide" and not (brand_graph.BRANDS_DIR / brand_id / "brand-guide.pdf").exists():
        raise HTTPException(400, "upload the brand guide first")
    return _start_graph_job(["--design", source, brand_id], f"design {source} {brand_id}", backend)


class TokenSet(BaseModel):
    name: str
    value: str


@app.post("/api/brands/{brand_id}/design/tokens")
def set_design_token(brand_id: str, req: TokenSet) -> dict:
    import brand_design

    return _graph_call(brand_design.set_token, brand_id, req.name, req.value)


@app.post("/api/brands/{brand_id}/design/modify/{proposal_id}/apply")
def apply_design(brand_id: str, proposal_id: str, req: ModifyApply) -> dict:
    import brand_design

    result = _graph_call(brand_design.apply, brand_id, proposal_id, req.picks)
    return {"applied": result, "summary": brand_design.summary(brand_id)}


@app.patch("/api/brands/{brand_id}/design/nodes/{node_id}")
def edit_design_node(brand_id: str, node_id: str, req: NodeEdit) -> dict:
    import brand_design

    return _graph_call(brand_design.edit_node, brand_id, node_id, req.fields)


@app.delete("/api/brands/{brand_id}/design/nodes/{node_id}")
def delete_design_node(brand_id: str, node_id: str) -> dict:
    import brand_design

    _graph_call(brand_design.delete_node, brand_id, node_id)
    return {"deleted": node_id}


@app.post("/api/brands/{brand_id}/guide")
async def upload_brand_guide(brand_id: str, file: UploadFile = File(...), backend: str = Form("")) -> dict:
    """Read a brand guide PDF into the graph (colors, font, voice, tagline, rules) as your own edits."""
    try:
        brand_graph.load(brand_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "unknown brand")
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "upload the brand guide as a PDF")
    if len(data) > 60_000_000:
        raise HTTPException(400, "brand guides are limited to 60 MB")
    path = brand_graph.BRANDS_DIR / brand_id / "brand-guide.pdf"
    path.write_bytes(data)
    return _start_graph_job(["--guide", brand_id, str(path)], f"guide {brand_id}", backend)


@app.post("/api/brands/{brand_id}/products/{product_id}/check")
async def check_product(brand_id: str, product_id: str) -> dict:
    """Re-read the product page now for price, stock and new variants (no model call)."""
    try:
        product_graph.load(brand_id, product_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "unknown product")
    err = await _run_product_check(brand_id, product_id)
    if err:
        raise HTTPException(502, err)
    return product_graph.values(product_graph.load(brand_id, product_id))


async def _run_product_check(brand_id: str, product_id: str) -> Optional[str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(BRAND_WORKER), "--check-product", brand_id, product_id,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(bootstrap.ROOT_DIR),
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
    for line in out.decode(errors="replace").splitlines():
        if line.startswith("@@ERROR "):
            return json.loads(line[8:])["error"]
    return None if proc.returncode == 0 else f"check exited with code {proc.returncode}"


REFRESH_DAYS = float(os.environ.get("PRODUCT_REFRESH_DAYS", "7"))


@app.on_event("startup")
async def _start_refresh_loop() -> None:
    if REFRESH_DAYS > 0:
        asyncio.create_task(_refresh_loop())


async def _refresh_loop() -> None:
    """Every hour, re-check product pages that haven't been checked for PRODUCT_REFRESH_DAYS."""
    await asyncio.sleep(60)
    while True:
        try:
            for brand_id, product_id in product_graph.due_for_check(REFRESH_DAYS)[:10]:
                await _run_product_check(brand_id, product_id)
                await asyncio.sleep(5)
        except Exception:
            pass  # a failed refresh is retried next hour
        await asyncio.sleep(3600)


@app.get("/api/brands/{brand_id}/products")
def list_products(brand_id: str) -> list[dict]:
    try:
        brand_graph.load(brand_id)
        return product_graph.list_products(brand_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "unknown brand")


@app.get("/api/brands/{brand_id}/products/{product_id}")
def get_product(brand_id: str, product_id: str) -> dict:
    try:
        return product_graph.values(product_graph.load(brand_id, product_id))
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "unknown product")


@app.get("/api/brands/{brand_id}/products/{product_id}/graph")
def get_product_graph(brand_id: str, product_id: str) -> dict:
    try:
        graph = product_graph.load(brand_id, product_id)
        brand = brand_graph.summary(brand_graph.load(brand_id))
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "unknown product")
    url = bootstrap.LocalStorageAdapter().url
    for n in graph["nodes"]:
        if n.get("file"):
            n["file_url"] = url(n["file"])
        if n.get("type") == "Brand":
            n["logo_url"] = brand["logo_url"]
    return graph


@app.post("/api/brands/{brand_id}/products")
async def search_product(brand_id: str, req: BrandSearch) -> dict:
    """Reuse the saved graph for this product, or start building one."""
    query = req.query.strip()
    if not query:
        raise HTTPException(400, "type a product name or paste its page link")
    try:
        brand_graph.load(brand_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "unknown brand")
    if not req.refresh:
        found = product_graph.find(brand_id, query)
        if found:
            product_graph.remember_alias(found, query)
            return {"status": "found", "product": product_graph.values(found)}
    return _start_graph_job(["--product", brand_id, query], query, req.backend)


async def _build_brand(job_id: str, backend: str) -> None:
    job = BRAND_JOBS[job_id]
    async with _brand_lock:
        job["state"] = "running"
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(BRAND_WORKER), *job["args"],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=str(bootstrap.ROOT_DIR), env=_llm_env(backend), limit=8 * 1024 * 1024,
            )
            async for raw in proc.stdout:
                line = raw.decode(errors="replace")
                if line.startswith("@@STAGE "):
                    job["stage"] = json.loads(line[8:])["stage"]
                    job["log"].append(job["stage"])
                elif line.startswith("@@RESULT "):
                    res = json.loads(line[9:])
                    if "applied" in res:
                        job["applied"] = res["applied"]
                    if "proposal" in res:
                        job["proposal"] = res["proposal"]
                    elif "version" in res:
                        job["edit"] = {**res, **_run_files(res["run_id"])}
                    elif "product_id" in res:
                        job["product"] = product_graph.values(product_graph.load(res["brand_id"], res["product_id"]))
                    else:
                        job["brand"] = brand_graph.form_values(brand_graph.load(res["id"]))
                elif line.startswith("@@ERROR "):
                    job["error"] = json.loads(line[8:])["error"]
            await proc.wait()
            done = job["brand"] or job["product"] or job.get("edit") or job.get("proposal")
            job["state"] = "done" if done else "error"
            if not done:
                job["error"] = job["error"] or f"brand worker exited with code {proc.returncode}"
        except Exception as exc:
            job["state"], job["error"] = "error", f"{type(exc).__name__}: {exc}"


@app.get("/api/brand-jobs/{job_id}")
def get_brand_job(job_id: str) -> dict:
    job = BRAND_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown brand search")
    return {k: job.get(k) for k in ("state", "stage", "log", "brand", "product", "edit", "applied", "proposal", "error")} | {
        "elapsed_s": round(time.time() - job["started"], 1)
    }


def _email_safe_image(data: bytes, filename: str) -> tuple[bytes, str]:
    """WebP becomes PNG (if it has transparency) or JPG: Outlook doesn't show WebP (caniemail)."""
    if not (filename.lower().endswith(".webp") or data[8:12] == b"WEBP"):
        return data, filename
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img.load()
    buf = io.BytesIO()
    alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if alpha:
        img.convert("RGBA").save(buf, "PNG", optimize=True)
    else:
        img.convert("RGB").save(buf, "JPEG", quality=90, optimize=True)
    return buf.getvalue(), re.sub(r"(\.webp)?$", ".png" if alpha else ".jpg", filename, count=1, flags=re.I)


def _save_upload(data: bytes, filename: str, run_id: str) -> str:
    data, filename = _email_safe_image(data, filename)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-") or "image"
    key = f"uploads/{run_id}/{safe}"
    bootstrap.LocalStorageAdapter().upload_file_object(key, data)
    return key


RUN_SLOTS = max(1, int(os.environ.get("MAX_PARALLEL_RUNS", "2")))  # runs at once; CLI logins have rate limits
_run_slots = asyncio.Semaphore(RUN_SLOTS)
SPEEDS = ("best", "balanced", "fast")
STEPS = [("prep", "Getting ready"), ("plan", "Planning the email"), ("art", "Art direction"),
         ("render", "Designing and rendering"), ("checks", "Checking the email")]
STEP_DEFAULTS = {"prep": 6, "plan": 70, "art": 45, "render": 230, "checks": 45}
DRAFT_ANGLES = {
    1: "Draft B: take a noticeably different creative angle from the obvious version: a different headline idea, "
       "a different section order or layout, while keeping the same facts, audience and call to action.",
    2: "Draft C: go bolder and more unexpected than a typical version: a surprising headline, an unusual but "
       "email-safe layout, while keeping the same facts, audience and call to action.",
}


def _step_of(stage: str) -> Optional[str]:
    s = stage.lower()
    if s.startswith("planning"):
        return "plan"
    if s.startswith("art direction"):
        return "art"
    if s.startswith("designing"):
        return "render"
    if s.startswith(("checking", "logo hard")):
        return "checks"
    return None


def _step_estimates() -> dict:
    """Median seconds per step over recent runs (timings.json), falling back to defaults."""
    keys = {"plan": "blueprint_s", "art": "art_direction_s", "render": "render_s", "checks": "checks_s"}
    samples: dict[str, list[float]] = {k: [] for k in keys}
    runs = sorted((bootstrap.STORAGE_DIR / "runs").glob("*/timings.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]
    for path in runs:
        try:
            t = json.loads(path.read_text())
        except ValueError:
            continue
        for step, key in keys.items():
            if isinstance(t.get(key), (int, float)) and t[key] > 0:
                samples[step].append(float(t[key]))
    out = dict(STEP_DEFAULTS)
    for step, vals in samples.items():
        if vals:
            out[step] = sorted(vals)[len(vals) // 2]
    return out


def _progress(job: dict) -> dict:
    est = _step_estimates()
    now = time.time()
    order = [k for k, _ in STEPS]
    current = job.get("step") if job.get("step") in order else None
    steps, done_s, total_s = [], 0.0, sum(est.values())
    for key, label in STEPS:
        started = job["step_started"].get(key)
        if job["state"] == "done" or (current and order.index(key) < order.index(current)):
            state, spent = "done", job["step_spent"].get(key, est[key])
        elif key == current and job["state"] == "running":
            state, spent = "running", now - started if started else 0
        else:
            state, spent = "pending", 0
        if state == "done":
            done_s += est[key]
        elif state == "running":
            done_s += min(spent, est[key] * 0.95)
        steps.append({"key": key, "label": label, "state": state, "spent_s": round(spent), "estimate_s": round(est[key])})
    pct = 100 if job["state"] == "done" else round(100 * done_s / total_s) if job["state"] == "running" else 0
    eta = None if job["state"] != "running" else max(5, round(total_s - done_s))
    return {"steps": steps, "percent": pct, "eta_s": eta}


def _enqueue(spec: dict, backend: str, speed: str) -> str:
    run_id = spec["run_id"]
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    spec_path = JOBS_DIR / f"{run_id}.json"
    spec_path.write_text(json.dumps(spec, indent=2))
    JOBS[run_id] = {
        "id": run_id, "backend": backend, "speed": speed, "model": model_choice.label(backend), "state": "queued",
        "stage": f"Queued: up to {RUN_SLOTS} emails generate at once",
        "log": deque(maxlen=400), "result": None, "error": None, "started": time.time(), "queued_at": time.time(),
        "step": None, "step_started": {}, "step_spent": {}, "group": spec.get("group"), "brief": spec["brief"][:160],
    }
    asyncio.create_task(_run(run_id, backend, spec_path, speed))
    return run_id


@app.post("/api/runs")
async def create_run(
    brief: str = Form(...),
    backend: str = Form(""),
    audience: str = Form("Consumer"),
    audience_description: str = Form(""),
    brand_name: str = Form(...),
    tagline: str = Form(""),
    background: str = Form("#0B1118"),
    text_color: str = Form("#FFFFFF"),
    accent: str = Form("#C9DCF2"),
    accent_text: str = Form("#0B1118"),
    font: str = Form("Helvetica Neue"),
    voice: str = Form(""),
    rules: str = Form(""),
    brand_id: str = Form(""),
    product_id: str = Form(""),
    speed: str = Form("best"),
    fact_check: str = Form("1"),
    drafts: int = Form(1),
    sequence: str = Form(""),
    template: str = Form(""),
    image_roles: str = Form("[]"),
    image_descriptions: str = Form("[]"),
    images: list[UploadFile] = File(default=[]),
) -> dict:
    """Queue one email, several drafts of it (drafts=2..3), or a planned sequence (sequence=JSON list of briefs)."""
    backend = _ready_backend(backend)
    speed = speed if speed in SPEEDS else "best"
    if brand_id:
        try:
            brand_graph.load(brand_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(400, f"unknown brand {brand_id!r}")
    if product_id:
        try:
            product_graph.load(brand_id, product_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(400, f"unknown product {product_id!r} for brand {brand_id!r}")
    steps = json.loads(sequence) if sequence else []
    if steps and not all(isinstance(x, dict) and str(x.get("brief", "")).strip() for x in steps):
        raise HTTPException(400, "every email in the sequence needs a brief")

    group_id = uuid.uuid4().hex[:12]
    roles = json.loads(image_roles or "[]")
    descs = json.loads(image_descriptions or "[]")
    image_specs = []
    for i, up in enumerate(images or []):
        data = await up.read()
        if not data:
            continue
        image_specs.append(
            {
                "key": _save_upload(data, up.filename or f"image-{i}.png", group_id),
                "role": roles[i] if i < len(roles) else "product_image",
                "description": descs[i] if i < len(descs) else "",
            }
        )
    font = font.strip() or "Helvetica Neue"
    base = {
        "audience": audience.strip() or "Consumer",
        "audience_description": audience_description.strip(),
        "images": image_specs,
        "brand_id": brand_id or None,
        "template": template if re.fullmatch(r"[a-z0-9_-]{0,40}", template) else "",
        "product_id": product_id or None,
        "fact_check": fact_check not in ("0", "false", ""),
        "brand": {
            "name": brand_name.strip(),
            "tagline": tagline.strip(),
            "background": background,
            "text": text_color,
            "accent": accent,
            "accent_text": accent_text,
            "font": font,
            "font_stack": f"'{font}', Helvetica, Arial, sans-serif",
            "voice": voice.strip(),
            "rules": [r.strip() for r in rules.splitlines() if r.strip()],
        },
    }
    if steps:
        variants = [(x["brief"].strip(), {"id": group_id, "kind": "sequence", "index": i, "total": len(steps),
                                          "label": x.get("name") or f"Email {i + 1}", "send": x.get("send", "")})
                    for i, x in enumerate(steps)]
    else:
        n = max(1, min(3, int(drafts or 1)))
        variants = [((brief.strip() if i == 0 else f"{brief.strip()}\n\n{DRAFT_ANGLES[i]}"),
                     {"id": group_id, "kind": "drafts", "index": i, "total": n, "label": f"Draft {'ABC'[i]}"} if n > 1 else None)
                    for i in range(n)]
    run_ids = []
    for text, group in variants:
        run_id = uuid.uuid4().hex[:12]
        spec = {**base, "run_id": run_id, "brief": text, **({"group": group} if group else {})}
        run_ids.append(_enqueue(spec, backend, speed))
    _save_group(group_id, run_ids, variants)
    return {"run_id": run_ids[0], "run_ids": run_ids, "group_id": group_id if len(run_ids) > 1 else None}


def _save_group(group_id: str, run_ids: list[str], variants: list) -> None:
    groups = bootstrap.STORAGE_DIR / "groups"
    groups.mkdir(exist_ok=True)
    kind = (variants[0][1] or {}).get("kind") if variants and variants[0][1] else None
    if len(run_ids) > 1:
        (groups / f"{group_id}.json").write_text(json.dumps({
            "id": group_id, "kind": kind, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "runs": [{"run_id": r, **(g or {})} for r, (_, g) in zip(run_ids, variants)],
        }, indent=2))


def _log_line(raw: str) -> Optional[str]:
    """Pipeline logs arrive as JSON lines; keep INFO and above, shortened."""
    raw = raw.rstrip()
    if not raw:
        return None
    level, msg = "", raw
    if raw.startswith("{"):
        try:
            rec = json.loads(raw)
            msg, level = str(rec.get("message", raw)), str(rec.get("level", ""))
        except ValueError:
            pass
    if level == "DEBUG" or msg.startswith(("FULLMJML", "FULLHTML", "<")):
        return None
    return f"{time.strftime('%H:%M:%S')} {level[:4]:4} {msg[:400]}"


def _enter_step(job: dict, step: str) -> None:
    now = time.time()
    if job["step"] == step:
        return
    if job["step"]:
        job["step_spent"][job["step"]] = now - job["step_started"][job["step"]]
    job["step"] = step
    job["step_started"][step] = now


async def _run(run_id: str, backend: str, spec_path: Path, speed: str = "best") -> None:
    job = JOBS[run_id]
    async with _run_slots:
        job["state"] = "running"
        job["started"] = time.time()
        _enter_step(job, "prep")
        env = _llm_env(backend, SPEED_MODE=speed)
        if speed == "fast" and backend == "claude_code" and "CLAUDE_CODE_MODEL" not in env:
            env["CLAUDE_CODE_MODEL"] = "sonnet"  # a model chosen under Model access wins over Speed
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(WORKER), str(spec_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=str(bootstrap.ROOT_DIR), env=env, limit=32 * 1024 * 1024,
            )
            async for raw in proc.stdout:
                line = raw.decode(errors="replace")
                if line.startswith("@@STAGE "):
                    job["stage"] = json.loads(line[8:])["stage"]
                    step = _step_of(job["stage"])
                    if step:
                        _enter_step(job, step)
                    job["log"].append(f"{time.strftime('%H:%M:%S')} ──── {job['stage']}")
                elif line.startswith("@@RESULT "):
                    job["result"] = {**json.loads(line[9:]), **_run_files(run_id)}
                elif line.startswith("@@ERROR "):
                    err = json.loads(line[8:])
                    job["error"] = err["error"]
                    job["log"].append(err.get("trace", ""))
                else:
                    shown = _log_line(line)
                    if shown:
                        job["log"].append(shown)
            await proc.wait()
            if job["result"]:
                job["state"] = "done"
            else:
                job["state"] = "error"
                job["error"] = job["error"] or f"worker exited with code {proc.returncode}"
        except Exception as exc:
            job["state"] = "error"
            job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if job["step"]:
                _enter_step(job, "__end")
            job["elapsed_s"] = round(time.time() - job["started"], 1)


def _job_view(job: dict) -> dict:
    queued_ahead = sum(1 for j in JOBS.values() if j["state"] == "queued" and j["queued_at"] < job["queued_at"])
    return {
        "id": job["id"], "state": job["state"], "stage": job["stage"], "speed": job.get("speed"), "model": job.get("model"),
        "group": job.get("group"), "brief": job.get("brief"), "error": job["error"],
        "elapsed_s": job.get("elapsed_s", round(time.time() - job["started"], 1)) if job["state"] != "queued" else 0,
        "queued_ahead": queued_ahead if job["state"] == "queued" else 0,
        **_progress(job),
    }


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    """Runs started since the server started: queued and running first, then the latest finished."""
    jobs = sorted(JOBS.values(), key=lambda j: (j["state"] not in ("running", "queued"), -j["queued_at"]))
    return [_job_view(j) for j in jobs[:30]]


@app.get("/api/groups/{group_id}")
def get_group(group_id: str) -> dict:
    path = bootstrap.STORAGE_DIR / "groups" / f"{group_id}.json"
    if not re.fullmatch(r"[0-9a-f]{12}", group_id) or not path.exists():
        raise HTTPException(404, "unknown group")
    group = json.loads(path.read_text())
    for r in group["runs"]:
        job = JOBS.get(r["run_id"])
        r["state"] = job["state"] if job else ("done" if (export.run_dir(r["run_id"]) / "email.html").exists() else "unknown")
        if r["state"] == "done":
            try:
                r.update(_run_files(r["run_id"]))
            except (ValueError, FileNotFoundError):
                pass
    return group


class EditRequest(BaseModel):
    notes: list[dict]
    backend: str = ""


def _finished_run(run_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", run_id or "") or not (export.run_dir(run_id) / "email.html").exists():
        raise HTTPException(404, "unknown run")
    return export.run_dir(run_id)


@app.get("/api/runs/{run_id}/sections")
def run_sections(run_id: str) -> list[dict]:
    html = (_finished_run(run_id) / "email.html").read_text()
    return [{"id": s["id"], "text": s["text"][:140]} for s in email_doc.sections(html)]


@app.post("/api/runs/{run_id}/edit")
async def edit_run(run_id: str, req: EditRequest) -> dict:
    """Apply reviewer notes to sections of a finished email; the result is a new version."""
    _finished_run(run_id)
    notes = [{"section": str(n.get("section", "")), "text": str(n.get("text", "")).strip()} for n in req.notes if str(n.get("text", "")).strip()]
    if not notes:
        raise HTTPException(400, "add at least one note")
    if not (JOBS_DIR / f"{run_id}.json").exists():
        raise HTTPException(400, "this run's settings weren't saved, so it can't be edited")
    return _start_graph_job(["--edit", json.dumps({"run_id": run_id, "notes": notes})], f"edit {run_id}", req.backend)


@app.post("/api/runs/{run_id}/versions/{n}/restore")
def restore_version(run_id: str, n: int) -> dict:
    _finished_run(run_id)
    try:
        versions.restore(run_id, n)
    except (FileNotFoundError, StopIteration):
        raise HTTPException(404, "unknown version")
    return _run_files(run_id)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, since: int = 0) -> dict:
    job = JOBS.get(run_id)
    if not job:
        raise HTTPException(404, "unknown run")
    log = list(job["log"])
    return {
        **_job_view(job),
        "backend": job.get("backend"),
        "result": job["result"],
        "log": log[since:] if since <= len(log) else log,
        "log_len": len(log),
    }


def _run_files(run_id: str) -> dict:
    """Links for one run, splitting out its subject lines first if that hasn't happened yet."""
    info = export.ensure_exports(run_id)
    url = bootstrap.LocalStorageAdapter().url
    checks_path = export.run_dir(run_id) / "checks.json"
    checks = json.loads(checks_path.read_text()) if checks_path.exists() else []
    return {
        "checks": checks,
        "versions": [{k: v[k] for k in ("n", "created", "note", "current")} | {"url": url(f"runs/{run_id}/versions/{v['n']}.html")}
                     for v in versions.versions(run_id)],
        "run_id": run_id,
        "html_url": url(f"runs/{run_id}/email.html"),
        "blueprint_url": url(f"runs/{run_id}/blueprint.json"),
        "mjml_url": url(f"runs/{run_id}/email.mjml"),
        "subject_lines": info["subject_lines"],
        "placeholder_links": info["placeholder_links"],
        "downloads": {
            "html": f"/api/runs/{run_id}/download/html",
            "subjects": f"/api/runs/{run_id}/download/subjects",
            "zip": f"/api/runs/{run_id}/download/zip",
        },
    }


@app.get("/api/runs")
def list_runs() -> list[dict]:
    runs_dir = bootstrap.STORAGE_DIR / "runs"
    out = []
    def made(d: Path) -> float:
        marker = d / "blueprint.json"
        return (marker if marker.exists() else d).stat().st_mtime

    for d in sorted(runs_dir.glob("*"), key=made, reverse=True) if runs_dir.exists() else []:
        if (d / "email.html").exists() and not (JOBS.get(d.name, {}).get("state") == "running"):
            try:
                out.append({**_run_files(d.name), "created": time.strftime("%b %d %H:%M", time.localtime(made(d)))})
            except (ValueError, FileNotFoundError):
                continue
        if len(out) == 20:
            break
    return out


@app.get("/api/runs/{run_id}/download/{kind}")
def download(run_id: str, kind: str) -> Response:
    try:
        export.ensure_exports(run_id)
        if kind == "html":
            body, mime, name = export.standalone_html(run_id), "text/html", f"email-{run_id}.html"
        elif kind == "subjects":
            body = (export.run_dir(run_id) / "subject-lines.txt").read_bytes()
            mime, name = "text/plain; charset=utf-8", f"subject-lines-{run_id}.txt"
        elif kind == "zip":
            body, mime, name = export.bundle_zip(run_id), "application/zip", f"email-{run_id}.zip"
        else:
            raise HTTPException(404, "unknown download")
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "unknown run")
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})


if __name__ == "__main__":
    print(f"Email pipeline UI → {bootstrap.BASE_URL}")
    for b in bootstrap.all_backend_status():
        print(f"  {'✓' if b['ready'] else '·'} {b['label']}: {b['detail']}")
    uvicorn.run(app, host=bootstrap.HOST, port=bootstrap.PORT, log_level="warning")
