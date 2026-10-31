"""Build one company's knowledge graph in its own process.

app.py starts this with LLM_BACKEND set, like worker.py. It is a separate
process because it opens the company's website: each host it reaches is added
to this process's egress allowlist only, so generation runs and the web server
keep the normal allowlist. Output lines:

    @@STAGE {"stage": "..."}
    @@RESULT {"id": "<brand id>"}            (or {"brand_id": ..., "product_id": ...})
    @@ERROR {"error": "...", "trace": "..."}

    .venv/bin/python local/brand_worker.py "Allbirds"
    .venv/bin/python local/brand_worker.py --product allbirds-com "Tree Runner"
    .venv/bin/python local/brand_worker.py --brief '{"mode": "write", "brand_id": "allbirds-com", ...}'
    .venv/bin/python local/brand_worker.py --edit '{"run_id": "...", "notes": [{"section": "hero", "text": "..."}]}'
    .venv/bin/python local/brand_worker.py --design website allbirds-com      (or guide; or emails <bid> '<json>')
    .venv/bin/python local/brand_worker.py --modify '{"brand_id": "allbirds-com", "product_id": null, "request": "add their sustainability facts"}'
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402  (reads LLM_BACKEND from the environment)
import brand_graph  # noqa: E402
import brief_writer  # noqa: E402
import product_graph  # noqa: E402


def emit(kind: str, payload: dict) -> None:
    print(f"@@{kind} {json.dumps(payload, default=str)}", flush=True)


def main(args: list[str]) -> int:
    log = lambda s: emit("STAGE", {"stage": s})  # noqa: E731
    try:
        if args[0] == "--brief":
            req = json.loads(args[1])
            brief = asyncio.run(brief_writer.write(**req))
            emit("RESULT", {"brief": brief, "checks": brief_writer.checklist(brief, bool(req.get("brand_id")))})
            return 0
        if args[0] == "--edit":
            import email_edit  # loads the renderer; only edits need it

            req = json.loads(args[1])
            entry = asyncio.run(email_edit.apply_notes(req["run_id"], req["notes"], allow_host=bootstrap.ALLOWED_HOSTS.add, log=log))
            emit("RESULT", {"run_id": req["run_id"], "version": entry})
            return 0
        if args[0] == "--modify":
            import graph_modify

            req = json.loads(args[1])
            proposal = asyncio.run(graph_modify.propose(req["brand_id"], req.get("product_id"), req["request"],
                                                        allow_host=bootstrap.ALLOWED_HOSTS.add, log=log, web=req.get("web")))
            emit("RESULT", {"proposal": proposal})
            return 0
        if args[0] == "--design":
            import brand_design

            source, brand_id = args[1], args[2]
            if source == "website":
                proposal = asyncio.run(brand_design.propose_from_website(brand_id, log=log))
            elif source == "guide":
                proposal = asyncio.run(brand_design.propose_from_guide(brand_id, log=log))
            else:
                import email_import

                proposal = asyncio.run(email_import.propose_from_emails(brand_id, json.loads(args[3]), log=log))
            emit("RESULT", {"proposal": proposal})
            return 0
        if args[0] == "--images":
            import image_search

            req = json.loads(args[1])
            results = asyncio.run(image_search.search(req["source"], req.get("query", ""), req.get("brand_id"), req.get("product_id"),
                                                      allow_host=bootstrap.ALLOWED_HOSTS.add, log=log))
            emit("RESULT", {"results": results})
            return 0
        if args[0] == "--describe-image":
            import image_search

            req = json.loads(args[1])
            emit("RESULT", asyncio.run(image_search.describe(req["path"], req.get("brand_id"), req.get("product_id"))))
            return 0
        if args[0] == "--fetch-image":
            import image_search

            req = json.loads(args[1])
            staged = asyncio.run(image_search.fetch(req.get("url", ""), req.get("title", ""), req.get("credit", ""), req.get("local", ""),
                                                    allow_host=bootstrap.ALLOWED_HOSTS.add))
            emit("RESULT", staged)
            return 0
        if args[0] == "--place":
            placed = asyncio.run(product_graph.place_products(args[1], args[2:] or None, allow_host=bootstrap.ALLOWED_HOSTS.add, log=log))
            emit("RESULT", {"id": args[1], "placed": placed})
            return 0
        if args[0] == "--check-product":
            res = asyncio.run(product_graph.check_for_changes(args[1], args[2], allow_host=bootstrap.ALLOWED_HOSTS.add, log=log))
            emit("RESULT", {"brand_id": args[1], "product_id": args[2], **res})
            return 0
        if args[0] == "--guide":
            applied = asyncio.run(brand_graph.apply_brand_guide(args[1], Path(args[2]), log=log))
            emit("RESULT", {"id": args[1], "applied": applied})
            return 0
        if args[0] == "--sequence":
            req = json.loads(args[1])
            emails = asyncio.run(brief_writer.write_sequence(**req))
            emit("RESULT", {"emails": emails})
            return 0
        if args[0] == "--product":
            graph = asyncio.run(product_graph.build(args[1], args[2], allow_host=bootstrap.ALLOWED_HOSTS.add, log=log))
            emit("RESULT", {"brand_id": graph["brand_id"], "product_id": graph["id"]})
            return 0
        graph = asyncio.run(brand_graph.build(args[0], allow_host=bootstrap.ALLOWED_HOSTS.add, log=log))
    except LookupError as exc:
        emit("ERROR", {"error": str(exc), "trace": ""})
        return 1
    except Exception as exc:
        emit("ERROR", {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-3000:]})
        return 1
    emit("RESULT", {"id": graph["id"]})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
