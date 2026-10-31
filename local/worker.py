"""Run one email generation in its own process.

app.py starts this with LLM_BACKEND set for the run, so every run can use a
different backend. Progress goes to stdout as lines app.py understands:

    @@STAGE {"stage": "..."}
    @@RESULT {...}
    @@ERROR {"error": "...", "trace": "..."}

Everything else on stdout is the pipeline's own log output.

    .venv/bin/python local/worker.py path/to/job.json
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
import email_checks  # noqa: E402
import export  # noqa: E402
import product_graph  # noqa: E402
import pipeline  # noqa: E402


def emit(kind: str, payload: dict) -> None:
    print(f"@@{kind} {json.dumps(payload, default=str)}", flush=True)


def main(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text())
    brand = pipeline.BrandSpec(**spec["brand"])
    images = [pipeline.ImageSpec(**img) for img in spec.get("images", [])]
    brief = spec["brief"]
    brand_g = product_g = None
    image_descriptions = {img.key: img.description for img in images}
    if spec.get("brand_id"):
        brand_g = brand_graph.load(spec["brand_id"])
        if spec.get("product_id"):
            product_g = product_graph.load(spec["brand_id"], spec["product_id"])
        # Which logos go in (product_graph.LOGO_MODES): the brand's header logo by default,
        # the product's instead when it runs as its own brand, or both.
        mode = product_graph.logo_mode(product_g)
        # The saved company graph supplies the header logo, links and footer details.
        extra = brand_graph.email_inputs(brand_g, brand.background, logo_graph=product_g if mode == "product" else None)
        brand.rules = [*brand.rules, *extra["rules"]]
        if extra.get("brief"):
            brief = f"{brief}\n\n{extra['brief']}"
        if product_g:
            # The product graph supplies its photos and the facts the copy may use.
            prod = product_graph.email_inputs(product_g)
            if any(img.role == "hero_image" for img in images):
                for p in prod["images"]:
                    p["role"] = "product_image" if p["role"] == "hero_image" else p["role"]
            images = [pipeline.ImageSpec(**p) for p in prod["images"]] + images
            brand.rules = [*brand.rules, *prod["rules"]]
            brief = f"{brief}\n\n{prod['brief']}"
        if mode == "both":
            product_logo = brand_graph.pick_logo(product_g, brand.background)
            product_name = product_graph.product(product_g).get("name", "the product")
            brand_name = brand_graph.company(brand_g).get("name") or brand.name
            images.insert(0, pipeline.ImageSpec(
                key=product_logo["file"], role="design_asset",
                description=(f"The {product_name} product logo (a product mark, not the email header). Place it once, centered, "
                             f"about 180-220px wide, directly above the product's hero image. The {brand_name} logo stays in the header."),
            ))
            brand.rules.append(f"Show the {product_name} product logo once, above its hero image; the {brand_name} logo stays as the header logo.")
        if mode == "product":
            # The product leads; the brand still signs off, small, in the footer.
            brand_logo = brand_graph.pick_logo(brand_g, brand.background)
            brand_name = brand_graph.company(brand_g).get("name") or brand.name
            if brand_logo:
                images.insert(0, pipeline.ImageSpec(
                    key=brand_logo["file"], role="design_asset",
                    description=(f"The {brand_name} logo as a small sign-off: place it once in the footer, centered, about "
                                 "80-100px wide. It is not the header logo."),
                ))
                brand.rules.append(f"Sign off with the small {brand_name} logo in the footer; the header logo is the product's.")
        if extra["image"]:
            images.insert(0, pipeline.ImageSpec(**extra["image"]))
        emit("STAGE", {"stage": f"Using saved graphs: {spec['brand_id']}" + (f" / {spec['product_id']}" if spec.get("product_id") else "")})
    image_descriptions.update({img.key: img.description for img in images})

    async def after_render(html: str, blueprint: dict):
        ctx = email_checks.Context(
            brand_spec=brand, brief=brief, brand=brand_g, product=product_g,
            image_descriptions=image_descriptions,
            subject_lines=export.parse_subject_lines(blueprint.get("email_toppers") or ""),
            allow_host=bootstrap.ALLOWED_HOSTS.add, log=lambda s: emit("STAGE", {"stage": s}),
            fact_check=spec.get("fact_check", True),
        )
        return await email_checks.run_all(html, ctx)

    # The design layers: generic section patterns and email-client rules, plus the brand's own tokens and notes.
    import brand_design

    brand.design = brand_design.bible_parts(
        spec.get("brand_id"), {"background": brand.background, "text": brand.text, "accent": brand.accent,
                               "accent_text": brand.accent_text}, brand.font_stack, template=spec.get("template", ""))
    emit("STAGE", {"stage": f"Starting ({bootstrap.BACKEND_LABELS[bootstrap.LLM_BACKEND]})"})
    try:
        res = asyncio.run(
            pipeline.generate_email(
                brief, brand, images,
                audience=spec.get("audience") or "Consumer",
                audience_description=spec.get("audience_description") or "",
                run_id=spec["run_id"],
                on_stage=lambda s: emit("STAGE", {"stage": s}),
                after_render=after_render,
            )
        )
    except Exception as exc:
        emit("ERROR", {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-3000:]})
        return 1

    stats = {}
    if bootstrap.LLM_BACKEND != "api":
        import llm_backends

        stats = dict(llm_backends.STATS)
    emit(
        "RESULT",
        {
            "backend": bootstrap.LLM_BACKEND,
            "timings": res["timings"],
            "model_calls": stats,
            "sections": [
                {"type": s.get("type") or s.get("section_id"), "headline": s.get("headline"), "copy": s.get("copy_outline")}
                for s in res["blueprint"].get("content_blueprint", [])
                if isinstance(s, dict)
            ],
            "warnings": (res["manifest"].get("warnings") or [])[:20],
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
