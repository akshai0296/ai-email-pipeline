"""Which model and effort each model-access mode uses ("Model access" in the UI).

Saved per mode in storage/settings.json. Every worker process gets the choice as environment
variables, so it applies to email runs, briefs, graph builds and edits alike:

    claude_code  CLAUDE_CODE_MODEL (an alias the claude CLI resolves to the latest model), LLM_EFFORT
    chatgpt      CHATGPT_MODEL, LLM_EFFORT       (the models your ChatGPT login offers, from Codex's list)
    api          FORCE_MODEL                     (Claude steps only; GPT steps keep the pipeline's model)

An empty model or effort means the pipeline's own choice for each step (and the Speed setting).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from bootstrap import STORAGE_DIR

SETTINGS = STORAGE_DIR / "settings.json"

CLAUDE_MODELS = [
    ("", "Pipeline's choice (Opus for most steps)"),
    ("opus", "Opus (latest)"),
    ("sonnet", "Sonnet (latest; faster)"),
    ("haiku", "Haiku (fastest; lighter work)"),
    ("fable", "Fable (needs usage credits on your Claude plan)"),
]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
API_MODELS = [
    ("", "Pipeline's choice"),
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
    ("claude-fable-5-1", "Claude Fable 5.1"),
]
# "ultra" hands work off to sub-agents, which the pipeline's one-shot calls don't use.
SKIP_EFFORTS = {"ultra", "none", "minimal"}


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def chatgpt_models() -> list[dict]:
    """The models your ChatGPT login offers (Codex's cached list), with the efforts each supports."""
    try:
        data = json.loads((_codex_home() / "models_cache.json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    for m in sorted(data.get("models", []), key=lambda m: m.get("priority", 99)):
        if m.get("visibility") != "list" or not m.get("slug"):
            continue
        efforts = [e["effort"] for e in m.get("supported_reasoning_levels", []) if e.get("effort") not in SKIP_EFFORTS]
        out.append({"id": m["slug"], "label": m.get("display_name") or m["slug"], "efforts": efforts,
                    "default_effort": m.get("default_reasoning_level")})
    return out


def _codex_default() -> tuple[str, str]:
    """(model, effort) from ~/.codex/config.toml."""
    import tomllib

    try:
        cfg = tomllib.loads((_codex_home() / "config.toml").read_text())
    except (OSError, ValueError):
        return "", ""
    return cfg.get("model", ""), cfg.get("model_reasoning_effort", "")


def options() -> dict:
    """What the UI offers for each mode."""
    models = chatgpt_models()
    default_model, default_effort = _codex_default()
    default = next((m for m in models if m["id"] == default_model), None)
    return {
        "claude_code": {"models": [{"id": i, "label": l, "efforts": CLAUDE_EFFORTS} for i, l in CLAUDE_MODELS],
                        "effort_note": "Effort is how long the model thinks before answering: higher is slower and usually better."},
        "chatgpt": {"models": [{"id": "", "label": f"Codex default ({default['label'] if default else default_model or 'its own choice'})",
                                "efforts": default["efforts"] if default else ["low", "medium", "high"],
                                "default_effort": default_effort}] + models,
                    "effort_note": "Reasoning effort for every step. The list depends on the model."},
        "api": {"models": [{"id": i, "label": l, "efforts": []} for i, l in API_MODELS],
                "effort_note": "With API keys each step keeps its own effort; only the Claude model can be chosen here."},
    }


def load() -> dict:
    try:
        return json.loads(SETTINGS.read_text()).get("models", {})
    except (OSError, ValueError):
        return {}


def get(backend: str) -> dict:
    c = load().get(backend) or {}
    return {"model": c.get("model", ""), "effort": c.get("effort", "")}


def save(backend: str, model: str, effort: str) -> dict:
    opts = options().get(backend)
    if not opts:
        raise ValueError(f"unknown model access {backend!r}")
    m = next((x for x in opts["models"] if x["id"] == model), None)
    if m is None:
        raise ValueError(f"{model!r} isn't offered for this model access")
    if effort and effort not in m["efforts"]:
        raise ValueError(f"effort {effort!r} isn't available for {m['label']}")
    try:
        data = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("models", {})[backend] = {"model": model, "effort": effort}
    SETTINGS.write_text(json.dumps(data, indent=2))
    return get(backend)


def env(backend: str) -> dict[str, str]:
    """Environment variables a worker needs for this mode's choice."""
    c = get(backend)
    out: dict[str, str] = {}
    if backend == "claude_code":
        if c["model"]:
            out["CLAUDE_CODE_MODEL"] = c["model"]
        if c["effort"]:
            out["LLM_EFFORT"] = c["effort"]
    elif backend == "chatgpt":
        if c["model"]:
            out["CHATGPT_MODEL"] = c["model"]
        if c["effort"]:
            out["LLM_EFFORT"] = c["effort"]
    elif backend == "api" and c["model"]:
        out["FORCE_MODEL"] = c["model"]
    return out


def label(backend: str) -> str:
    """Short text for a run, e.g. "Opus · high effort"."""
    c = get(backend)
    opts = options().get(backend, {"models": []})
    name = next((m["label"] for m in opts["models"] if m["id"] == c["model"]), c["model"]) if c["model"] else "pipeline's models"
    name = name.split(" (")[0]
    return f"{name} · {c['effort']} effort" if c["effort"] else name


def forced_model(backend: str) -> Optional[str]:
    return get(backend)["model"] or None
