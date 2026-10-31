"""Speed modes: which model each pipeline step uses.

    best      every step on the pipeline's own choice (mostly Opus)              default
    balanced  mechanical steps on a faster model; planning, copy, design, art
              direction and visual QA stay on the strongest one
    fast      every Claude step on the faster model

Steps are recognized by the opening of their prompt, so this works for all three
model access modes (it hooks the same places as consumer_framing.py). The worker
sets SPEED_MODE for each run.
"""

from __future__ import annotations

import functools
import inspect
import os
import re
from collections import Counter

FAST_API_MODEL = os.environ.get("FAST_MODEL", "claude-sonnet-4-6")
STATS: Counter = Counter()

# Prompt openings of steps that do mechanical work.
LIGHT_STEPS = [
    ("template picker", re.compile(r"You are an expert email template selector")),
    ("rule mapper", re.compile(r"You are a (?:pharmaceutical brand-compliance|brand-consistency) art director")),
    ("image shortlist", re.compile(r"You are (?:an art director finalizing brand imagery for ONE section|curating brand-library imagery)")),
    ("asset pruner", re.compile(r"You are doing a focused asset-restraint pass")),
    ("css helper", re.compile(r"You are a CSS typographic system helper")),
    ("mjml validator", re.compile(r"You are an expert MJML (?:\(Mailjet Markup Language\) )?(?:validator|fixer)")),
    ("subject lines", re.compile(r"email topper \(subject line and preheader\)")),
]


def mode() -> str:
    m = os.environ.get("SPEED_MODE", "best").strip().lower()
    return m if m in ("best", "balanced", "fast") else "best"


def light_step(text: str) -> str | None:
    head = (text or "")[:6000]
    return next((name for name, pat in LIGHT_STEPS if pat.search(head)), None)


def _prompt_text(system, payload) -> str:
    parts = [system if isinstance(system, str) else str(system or "")]
    parts.append(str(payload)[:6000])
    return " ".join(parts)


def _route(model: str, text: str, cli: bool) -> str:
    if FORCED_API_MODEL and not cli and "claude" in model.lower():
        return FORCED_API_MODEL  # a model chosen under Model access beats the speed setting
    m = mode()
    if m == "best" or ("claude" not in model.lower() and not cli):  # API mode: only Claude models are swapped
        return model
    if m == "balanced":
        step = light_step(text)
        if not step:
            return model
        STATS[step] += 1
    else:
        STATS["fast"] += 1
    return "sonnet" if cli else FAST_API_MODEL


FORCED_API_MODEL = os.environ.get("FORCE_MODEL", "").strip()  # chosen under Model access (API keys)


def install(llm_backends=None) -> None:
    if mode() == "best" and (llm_backends is not None or not FORCED_API_MODEL):
        return
    if llm_backends is not None:  # Claude Code / ChatGPT login: model is a hint the CLI adapter maps
        real_sync, real_async = llm_backends.run_sync, llm_backends.run_async

        def run_sync(system, blocks, model_hint, effort=None):
            return real_sync(system, blocks, _route(model_hint, _prompt_text(system, blocks), True), effort)

        async def run_async(system, blocks, model_hint, effort=None):
            return await real_async(system, blocks, _route(model_hint, _prompt_text(system, blocks), True), effort)

        llm_backends.run_sync, llm_backends.run_async = run_sync, run_async
        return
    try:  # API keys: rewrite the model on Anthropic SDK calls (LangChain's ChatAnthropic uses them too)
        from anthropic.resources.messages import messages as am

        for cls in (am.Messages, am.AsyncMessages):
            for name in ("create", "stream"):
                fn = getattr(cls, name, None)
                if fn is None or getattr(fn, "_model_routing", False):
                    continue
                setattr(cls, name, _wrap(fn))
    except ImportError:
        pass


def _wrap(fn):
    def pick(kw):
        if "model" in kw:
            kw = {**kw, "model": _route(kw["model"], _prompt_text(kw.get("system"), kw.get("messages")), False)}
        return kw

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def asyn(*a, **kw):
            return await fn(*a, **pick(kw))
        asyn._model_routing = True
        return asyn

    @functools.wraps(fn)
    def sync(*a, **kw):
        return fn(*a, **pick(kw))
    sync._model_routing = True
    return sync
