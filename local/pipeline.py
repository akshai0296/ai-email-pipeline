"""Run the email pipeline end to end, the way production chains it.

    brief + brand + images
        → _generate_email_blueprint   (Query Agent V3 blueprint tool, Claude)
        → enrich_blueprint_design_flow (art-director pass)
        → run_message_from_scratch     (MJML render graph → HTML)

Production reads the brand from Postgres and claims from Pinecone. Here the
brand comes from a small dict (see ``build_brand_info``) and the claim list is
empty, which puts the pipeline in its "unbranded / non-clinical" mode.
"""

from __future__ import annotations

import copy
import json
import mimetypes
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import bootstrap  # noqa: F401  (must run before any src.* import)
import export
from bootstrap import STORAGE_DIR, LocalStorageAdapter

from src.content_generation_new.application.Agentic_Workflows.message_generator_from_scratch_v0_1 import (
    _build_email_brand_context_payload,
    run_message_from_scratch,
)
from src.content_generation_new.application.Agentic_Workflows.query_agent_v3.tools.blueprint import (
    _generate_email_blueprint,
    enrich_blueprint_design_flow,
)

bootstrap.assert_keys_untouched()

IMAGE_ROLES = ("hero_image", "product_image", "lifestyle_image", "header_image", "design_asset", "information_image")


@dataclass
class BrandSpec:
    name: str
    tagline: str = ""
    background: str = "#0B1118"
    text: str = "#FFFFFF"
    accent: str = "#C9DCF2"
    accent_text: str = "#0B1118"
    font: str = "Helvetica Neue"
    font_stack: str = "'Helvetica Neue', Helvetica, Arial, sans-serif"
    voice: str = ""
    rules: list[str] = field(default_factory=list)
    # From the design layers (brand_design.bible_parts): type scale, spacing, button, section patterns, client rules.
    design: Optional[dict] = None


@dataclass
class ImageSpec:
    """An image already saved under local/storage."""

    key: str
    role: str = "product_image"
    description: str = ""


def save_upload(data: bytes, filename: str, run_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-") or "image"
    key = f"uploads/{run_id}/{safe}"
    LocalStorageAdapter().upload_file_object(key, data)
    return key


def build_design_bible(brand: BrandSpec, audience: str) -> dict:
    """Design Bible v2 document: the structured brand guide the pipeline reads."""
    rules = [{"category": "brand", "rule": r} for r in brand.rules if r.strip()]
    rules.append(
        {
            "category": "compliance",
            "rule": "This is a consumer brand, not a pharmaceutical product. Do not add ISI, "
            "safety information, clinical references, footnotes or HCP language.",
        }
    )
    rules.append(
        {
            "category": "layout",
            "rule": "Image-and-text rows (a photo in one column, copy in the other): the copy column holds an eyebrow, a "
            "headline and at most about 40 words of body text, so it stands about as tall as the photo. Longer copy goes in "
            "a full-width text section of its own, or under a full-width photo. Set vertical-align=\"top\" on both columns so "
            "the photo lines up with the headline instead of floating beside tall copy.",
        }
    )
    bible = {
        "schema_version": 2,
        "meta": {"brand": brand.name, "company": brand.name, "audience": [audience]},
        "overview": brand.tagline or None,
        "colors": {
            "palette": [
                {"name": "Background", "hex": brand.background, "tier": "primary"},
                {"name": "Text", "hex": brand.text, "tier": "neutral"},
                {"name": "Accent", "hex": brand.accent, "tier": "accent"},
                {"name": "Accent Text", "hex": brand.accent_text, "tier": "neutral"},
            ],
            "roles": {
                "background": "Background",
                "text": "Text",
                "heading": "Text",
                "accent": "Accent",
                "cta_fill": "Accent",
                "cta_label": "Accent Text",
            },
        },
        "surfaces": [
            {"name": "brand", "background": "Background", "group": "dark" if _is_dark(brand.background) else "light",
             "heading_color": "Text", "body_color": "Text"},
        ],
        "typography": {"brand_font": brand.font, "email_stack": brand.font_stack},
        "layout": {"canvas": {"desktop_width": "600px", "mobile_width": "375px", "mobile_breakpoint": "480px"}},
        "components": {
            "button": {"shape": {"radius": "0px"}, "variants": [{"id": "primary", "fill_role": "cta_fill", "label_role": "cta_label"}]}
        },
        "content": {"voice": [v for v in [brand.voice] if v]},
        "other_rules": rules,
        "creative_freedom_mode": True,
    }
    return _with_design(bible, brand.design)


def _with_design(bible: dict, design: Optional[dict]) -> dict:
    """Fill the Design Bible from the design layers: generic patterns and client rules, the brand's tokens."""
    if not design:
        return bible
    bible["typography"]["scale"] = design["scale"]
    bible["layout"]["spacing"] = design["spacing"]
    bible["layout"]["constraints"] = design["constraints"]
    bible["components"]["button"]["shape"] = design["button_shape"]
    bible["components"]["items"] = design["components"]
    bible["other_rules"] = [*bible["other_rules"], *design["rules"]]
    if design.get("surface"):
        bible["colors"]["palette"].append({"name": "Surface", "hex": design["surface"], "tier": "secondary"})
        bible["surfaces"].append({"name": "tint", "background": "Surface", "group": "dark" if _is_dark(design["surface"]) else "light",
                                  "heading_color": "Text", "body_color": "Text"})
    return bible


def _is_dark(hex_color: str) -> bool:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return True
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 128


def build_brand_info(brand: BrandSpec, audience: str, audience_description: str = "") -> dict:
    return {
        "id": "",
        "name": brand.name,
        "brand_name": brand.name,
        "drug_name": brand.name,
        "has_isi": False,
        "has_claims": False,
        "design_bible": {"design_bible_v2": build_design_bible(brand, audience)},
        # The blueprint step lists these under TARGET AUDIENCE to tailor message and tone.
        "brand_configured_target_audience_description": {audience: audience_description} if audience_description else {},
    }


def build_gathered_context(brief: str, audience: str, images: list[ImageSpec]) -> dict:
    adapter = LocalStorageAdapter()
    analyses, decisions = [], {}
    for img in images:
        name = img.key.rsplit("/", 1)[-1]
        role = img.role if img.role in IMAGE_ROLES else "product_image"
        analyses.append(
            {
                "filename": name,
                "detected_type": role,
                "suggested_role": role,
                "summary": img.description or f"{role.replace('_', ' ')} supplied by the user",
                "s3_url": adapter.url(img.key),
                "mime_type": mimetypes.guess_type(name)[0] or "image/png",
            }
        )
        decisions[name] = {"image_use_mode": role}
    return {
        "audience": audience,
        "key_message": brief,
        "_original_query": brief,
        "distribution_type": "hq",
        "content_type": "email",
        "_file_analyses": analyses,
        "_file_use_decisions": decisions,
    }


async def generate_email(
    brief: str,
    brand: BrandSpec,
    images: Optional[list[ImageSpec]] = None,
    audience: str = "Consumer",
    audience_description: str = "",
    run_id: Optional[str] = None,
    on_stage: Optional[Callable[[str], None]] = None,
    after_render: Optional[Callable[[str, dict], Awaitable[tuple[str, list[dict]]]]] = None,
) -> dict[str, Any]:
    """Return {'run_id', 'html_url', 'blueprint', 'manifest', 'timings'} and write files to storage/runs/<id>/."""
    run_id = run_id or uuid.uuid4().hex[:12]
    images = images or []
    stage = on_stage or (lambda _s: None)
    timings: dict[str, float] = {}
    out_dir = STORAGE_DIR / "runs" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    brand_info = build_brand_info(brand, audience, audience_description)
    gathered = build_gathered_context(brief, audience, images)
    brand_context = _build_email_brand_context_payload(brand_info)

    stage("Planning the email (blueprint)")
    t = time.monotonic()
    blueprint = await _generate_email_blueprint(brand_info, gathered, claims=[], file_extractions=None, domain_rules=None)
    timings["blueprint_s"] = round(time.monotonic() - t, 1)
    if not isinstance(blueprint, dict) or blueprint.get("error") or not blueprint.get("content_blueprint"):
        (out_dir / "blueprint.json").write_text(json.dumps(blueprint, indent=2, default=str))
        raise RuntimeError(f"Blueprint step failed: {blueprint.get('error') if isinstance(blueprint, dict) else blueprint}")

    stage("Art direction")
    t = time.monotonic()
    try:
        blueprint = await enrich_blueprint_design_flow(
            blueprint=blueprint,
            brand_info=brand_info,
            brand_context=brand_context,
            gathered_context=gathered,
            skip_if_enriched=True,
        )
    except Exception as exc:  # production treats this as non-fatal too
        stage(f"Art direction skipped: {exc}")
    timings["art_direction_s"] = round(time.monotonic() - t, 1)
    (out_dir / "blueprint.json").write_text(json.dumps(blueprint, indent=2, default=str))

    stage("Designing and rendering (MJML → HTML, visual QA)")
    t = time.monotonic()
    result = await run_message_from_scratch(
        claims=[],
        brand_info=brand_info,
        user_query=brief,
        brand_rule_context=brand_context,
        media_type="email",
        operation_id="",
        target_audience=audience,
        use_mjml=True,
        pre_generated_blueprint=copy.deepcopy(blueprint),
        email_distribution_type=blueprint.get("email_distribution_type") or "hq",
        pre_selected_header_mjml=blueprint.get("template_header_mjml") or None,
        pre_selected_footer_mjml=blueprint.get("template_footer_mjml") or None,
        # Set here rather than left to the backend default, so a run can be compared with and without it.
        use_designer_agent=os.environ.get("DESIGNER_AGENT", "1").strip() != "0",
    )
    timings["render_s"] = round(time.monotonic() - t, 1)

    html = result.get("html") or ""
    if not html:
        raise RuntimeError("Render step returned no HTML. Check the log for the failing node.")
    if after_render:
        # Checks on the finished HTML (facts, footer, links, logo, contrast, inbox), fixing what they safely can.
        stage("Checking the email (facts, footer, links, logo, contrast)")
        t = time.monotonic()
        try:
            html, report = await after_render(html, blueprint)
        except Exception as exc:  # a failed check never costs the user the email
            report = [{"check": "Checks", "status": "skipped", "detail": f"{type(exc).__name__}: {exc}"}]
        timings["checks_s"] = round(time.monotonic() - t, 1)
        (out_dir / "checks.json").write_text(json.dumps(report, indent=2))  # shown in the UI's Checks panel
    (out_dir / "email.html").write_text(html)
    (out_dir / "email.mjml").write_text(result.get("mjml_document") or result.get("mjml") or "")
    (out_dir / "manifest.json").write_text(json.dumps(result.get("manifest", {}), indent=2, default=str))
    (out_dir / "toppers.html").write_text(blueprint.get("email_toppers") or "")
    # Split subject lines out of the email; write subject-lines.txt.
    shutil.rmtree(out_dir / "original", ignore_errors=True)
    exports = export.ensure_exports(run_id)

    (out_dir / "timings.json").write_text(json.dumps(timings))  # the app's time estimates come from these
    stage("Done")
    return {
        "run_id": run_id,
        "html_url": LocalStorageAdapter().url(f"runs/{run_id}/email.html"),
        "blueprint": blueprint,
        "manifest": result.get("manifest", {}),
        "timings": timings,
        "subject_lines": exports["subject_lines"],
    }
