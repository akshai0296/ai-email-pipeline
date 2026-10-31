"""Version history for a finished email.

runs/<id>/versions/<n>.html with versions.json beside it. Version 1 is the email
as generated; every edit or restore adds one. The current version is always
runs/<id>/email.html and original/email.html (the export step rebuilds the
downloads from the latter). Light on imports so the web server can use it.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import export

def _dir(run_id: str) -> Path:
    return export.run_dir(run_id)


def versions(run_id: str) -> list[dict]:
    path = _dir(run_id) / "versions" / "versions.json"
    return json.loads(path.read_text()) if path.exists() else []


def _write_versions(run_id: str, items: list[dict]) -> None:
    folder = _dir(run_id) / "versions"
    folder.mkdir(exist_ok=True)
    (folder / "versions.json").write_text(json.dumps(items, indent=2))


def ensure_original(run_id: str) -> list[dict]:
    items = versions(run_id)
    if not items:
        folder = _dir(run_id) / "versions"
        folder.mkdir(exist_ok=True)
        shutil.copy2(_dir(run_id) / "email.html", folder / "1.html")
        checks = _dir(run_id) / "checks.json"
        items = [{"n": 1, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "note": "Original", "current": True,
                  "checks": json.loads(checks.read_text()) if checks.exists() else []}]
        _write_versions(run_id, items)
    return items


def save_version(run_id: str, html: str, note: str, checks: list[dict]) -> dict:
    items = ensure_original(run_id)
    n = max(v["n"] for v in items) + 1
    folder = _dir(run_id) / "versions"
    (folder / f"{n}.html").write_text(html)
    for v in items:
        v["current"] = False
    entry = {"n": n, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "note": note, "current": True, "checks": checks}
    items.append(entry)
    _write_versions(run_id, items)
    _make_current(run_id, html, checks)
    return entry


def restore(run_id: str, n: int) -> dict:
    items = ensure_original(run_id)
    src = _dir(run_id) / "versions" / f"{int(n)}.html"
    if not src.exists():
        raise FileNotFoundError(f"no version {n}")
    target = next(v for v in items if v["n"] == int(n))
    return save_version(run_id, src.read_text(), f"Restored version {n}", target.get("checks", []))


def _make_current(run_id: str, html: str, checks: list[dict]) -> None:
    d = _dir(run_id)
    (d / "original").mkdir(exist_ok=True)
    (d / "original" / "email.html").write_text(html)  # export rebuilds email.html and the downloads from this
    (d / "email.html").write_text(html)
    (d / "checks.json").write_text(json.dumps(checks, indent=2))
    export.ensure_exports(run_id)
