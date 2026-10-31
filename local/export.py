"""Turn a pipeline run into deliverable files.

The pipeline puts its subject-line/preheader options at the top of the email
itself and leaves the inbox preview text as "Preview text". This splits them:

    runs/<id>/email.html          the email only (preview text = option 1 preheader)
    runs/<id>/email.mjml          same, as MJML
    runs/<id>/subject-lines.txt   every subject + preheader option
    runs/<id>/original/           the pipeline's untouched output

Downloads are built from those files:
    html → one self-contained file, images embedded (opens anywhere)
    zip  → email.html with images/ beside it + subject-lines.txt (for email platforms)
"""

from __future__ import annotations

import base64
import html as html_lib
import io
import mimetypes
import re
import shutil
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup

from bootstrap import FILES_URL, STORAGE_DIR

TOPPER_MARKER = "<!-- Subject/Preheader Section -->"
_SECTION_START = re.compile(r"<!--\s*Section:Start")
_HTML_PREVIEW = re.compile(r'(<body[^>]*>.*?<div style="display:none;[^"]*">)(.*?)(</div>)', re.S)
_MJML_PREVIEW = re.compile(r"(<mj-preview>)(.*?)(</mj-preview>)", re.S)
_LOCAL_ASSET = re.compile(re.escape(FILES_URL) + r"/([^\"')\s]+)")


def run_dir(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id or ""):
        raise ValueError("bad run id")
    path = STORAGE_DIR / "runs" / run_id
    if not (path / "email.html").exists():
        raise FileNotFoundError(run_id)
    return path


def parse_subject_lines(toppers_html: str) -> list[dict]:
    options = []
    for box in BeautifulSoup(toppers_html or "", "html.parser").select(".email-topper"):
        item = {"subject": "", "preheader": ""}
        for row in box.find_all("div", recursive=False):
            label = row.find("strong")
            if not label:
                continue
            key = label.get_text(strip=True).rstrip(":").lower()
            label.extract()
            if key in item:
                item[key] = row.get_text(" ", strip=True)
        if item["subject"] or item["preheader"]:
            options.append(item)
    return options


def subject_lines_text(options: list[dict]) -> str:
    if not options:
        return "No subject lines were generated for this run.\n"
    lines = ["Subject lines and preheaders", "(The email's inbox preview text uses option 1's preheader.)", ""]
    for i, opt in enumerate(options, 1):
        lines += [f"Option {i}", f"Subject:   {opt['subject']}", f"Preheader: {opt['preheader']}", ""]
    return "\n".join(lines)


def _strip_topper_html(doc: str) -> str:
    start = doc.find(TOPPER_MARKER)
    if start == -1:
        return doc
    nxt = _SECTION_START.search(doc, start)
    if not nxt:
        return doc
    # Keep the indentation before the next section marker.
    end = doc.rfind("\n", start, nxt.start()) + 1 or nxt.start()
    return doc[:start] + doc[end:]


def _strip_topper_mjml(doc: str) -> str:
    start = doc.find(TOPPER_MARKER)
    if start == -1:
        return doc
    end = doc.find("</mj-section>", start)
    return doc if end == -1 else doc[:start] + doc[end + len("</mj-section>"):].lstrip("\n")


def _set_preview(doc: str, pattern: re.Pattern, text: str) -> str:
    return pattern.sub(lambda m: m.group(1) + html_lib.escape(text) + m.group(3), doc, count=1)


def ensure_exports(run_id: str) -> dict:
    """Idempotent: safe to call on old runs and repeatedly."""
    d = run_dir(run_id)
    orig = d / "original"
    if not orig.exists():
        orig.mkdir()
        for name in ("email.html", "email.mjml"):
            if (d / name).exists():
                shutil.copy2(d / name, orig / name)

    toppers = (d / "toppers.html").read_text() if (d / "toppers.html").exists() else ""
    options = parse_subject_lines(toppers)
    (d / "subject-lines.txt").write_text(subject_lines_text(options))
    preheader = options[0]["preheader"] if options else ""

    html_doc = _strip_topper_html((orig / "email.html").read_text())
    html_doc = _set_preview(html_doc, _HTML_PREVIEW, preheader)
    (d / "email.html").write_text(html_doc)

    if (orig / "email.mjml").exists():
        mjml_doc = _strip_topper_mjml((orig / "email.mjml").read_text())
        (d / "email.mjml").write_text(_set_preview(mjml_doc, _MJML_PREVIEW, preheader))

    return {"subject_lines": options, "placeholder_links": sorted(set(re.findall(r'href="(https?://(?:www\.)?example\.com[^"]*)"', html_doc)))}


def _asset_path(rel: str) -> Path | None:
    path = (STORAGE_DIR / rel).resolve()
    return path if STORAGE_DIR.resolve() in path.parents and path.is_file() else None


def standalone_html(run_id: str) -> bytes:
    """email.html with every local image inlined as a data URI."""
    doc = (run_dir(run_id) / "email.html").read_text()

    def inline(m: re.Match) -> str:
        path = _asset_path(m.group(1))
        if not path:
            return m.group(0)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"

    return _LOCAL_ASSET.sub(inline, doc).encode()


def bundle_zip(run_id: str) -> bytes:
    """email.html + images/ + subject-lines.txt (+ email.mjml)."""
    d = run_dir(run_id)
    doc = (d / "email.html").read_text()
    images: dict[str, Path] = {}

    def relink(m: re.Match) -> str:
        path = _asset_path(m.group(1))
        if not path:
            return m.group(0)
        name, n = path.name, 1
        while name in images and images[name] != path:
            n += 1
            name = f"{path.stem}-{n}{path.suffix}"
        images[name] = path
        return f"images/{name}"

    doc = _LOCAL_ASSET.sub(relink, doc)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("email.html", doc)
        z.write(d / "subject-lines.txt", "subject-lines.txt")
        if (d / "email.mjml").exists():
            z.writestr("email.mjml", _LOCAL_ASSET.sub(relink, (d / "email.mjml").read_text()))
        for name, path in images.items():
            z.write(path, f"images/{name}")
    return buf.getvalue()
