"""Consumer-brand personas for the backend's pharma prompts.

The backend's email prompts open by casting the model as a pharma writer ("a
senior medical copywriter ... for pharmaceutical brands to HCP audiences"). For
the consumer brands this app makes emails for, that framing pulls copy toward a
clinical voice and fights the audience you pick. Rather than edit backend/, this
swaps those persona lines on the way to the model, for every model access mode:

    Claude Code / ChatGPT login   llm_backends.run_sync / run_async
    API keys                      anthropic Messages(.create/.stream), openai chat.completions / responses
                                  (LangChain's ChatAnthropic / ChatOpenAI go through these SDKs)

Only the exact persona sentences below change; the rest of each prompt is
untouched. ``STATS`` counts which ones fired. Set CONSUMER_FRAMING=0 to turn it off.
"""

from __future__ import annotations

import functools
import os
import re
from collections import Counter
from typing import Any

STATS: Counter = Counter()

REWRITES: list[tuple[str, re.Pattern, str]] = [
    (
        "planner",
        re.compile(
            r"You are a \*\*senior medical copywriter\*\* with 15\+ years of experience writing award-winning promotional "
            r"emails for pharmaceutical brands to HCP audiences \(US and EU\)\. You have shipped hundreds of campaigns across "
            r"oncology, immunology, respiratory, cardiology, and rare disease\. You know what makes an HCP stop scrolling, "
            r"what makes them trust a message, and what earns the click\. You are compliant-aware, peer-to-peer in voice, "
            r"and allergic to marketing fluff\."
        ),
        "You are a **senior consumer-brand copywriter** with 15+ years of experience writing award-winning marketing "
        "emails for direct-to-consumer and retail brands. You have shipped hundreds of campaigns: launches, seasonal "
        "drops, welcome series, win-backs and loyalty programs. You know what makes a reader stop scrolling, what makes "
        "them trust a brand, and what earns the click. You write in the brand's own voice for the audience named below, "
        "keep every claim to the facts you are given, and are allergic to marketing fluff.",
    ),
    (
        "art director",
        re.compile(r"You are an award-winning pharmaceutical email \*\*art director\*\*\. The copy and structure of an HCP email blueprint are already finalized\."),
        "You are an award-winning consumer-brand email **art director**. The copy and structure of the email blueprint are already finalized.",
    ),
    (
        "imagery",
        re.compile(r"You are an art director finalizing brand imagery for ONE section of a pharma HCP email\."),
        "You are an art director finalizing brand imagery for ONE section of a consumer-brand marketing email.",
    ),
    (
        "rule mapper",
        re.compile(r"You are a pharmaceutical brand-compliance art director\."),
        "You are a brand-consistency art director for consumer brands.",
    ),
    (
        "architect",
        re.compile(r"You are a \*\*Pharma Email Blueprint Architect\*\*\."),
        "You are a **Consumer Email Blueprint Architect**.",
    ),
    (
        "header",
        re.compile(r"You are an expert pharmaceutical content designer\."),
        "You are an expert consumer-brand email designer.",
    ),
    (
        "designer",
        re.compile(r"You are a \*\*Senior Medical Content Designer & Creative Director\*\* specializing in pharmaceutical email design"),
        "You are a **Senior Email Designer & Creative Director** specializing in consumer-brand marketing email design",
    ),
    (
        "section designer",
        re.compile(
            r"You are a \*\*Senior Medical Content Designer\*\* who specializes in pharmaceutical-marketing emails"
            r"(.*?), and (.+?)-focused regulatory compliance\.",
        ),
        r"You are a **Senior Email Designer** who specializes in consumer-brand marketing emails\1, and messaging for \2.",
    ),
    (
        "subject lines",
        re.compile(r"You are a \*\*Senior Pharmaceutical Marketing Compliance Expert\*\*[\s\S]{0,1500}?clinically accurate\."),
        "You are a **senior email marketer** who writes subject lines and preheaders for consumer brands, with deep "
        "knowledge of inbox behavior, what earns an open, and the CAN-SPAM Act.\n\n"
        "Your task is to generate 5 different versions of email topper (subject line and preheader) that are on-brand, "
        "honest, specific to this email and written for its audience.\n\n"
        "This is a consumer brand, not a medicine. In the rules below, skip everything about drug or generic names, "
        "indications, fair balance, ISI or safety information: none of it applies. The rules against deceptive "
        "prefixes, false personalization, \"cure\" and unsubstantiated superlatives still apply.",
    ),
    (
        "visual QA",
        re.compile(r"(You are doing (?:the final design pass|a focused asset-restraint pass) on the rendered CONTENT SECTIONS of a) pharma HCP email"),
        r"\1 consumer-brand marketing email",
    ),
    (
        "image shortlist",
        re.compile(r"You are curating brand-library imagery for a pharmaceutical HCP email\."),
        "You are curating brand-library imagery for a consumer-brand marketing email.",
    ),
    (
        "upload designer",
        re.compile(r"You are designing a pharmaceutical email section using content from user-uploaded documents\."),
        "You are designing a consumer-brand email section using content from user-uploaded documents.",
    ),
    (
        "template picker",
        re.compile(r"You are an expert email template selector for pharmaceutical marketing emails\."),
        "You are an expert email template selector for consumer-brand marketing emails.",
    ),
]


def rewrite(text: str) -> str:
    if not isinstance(text, str) or "You are" not in text:
        return text
    for name, pattern, replacement in REWRITES:
        text, n = pattern.subn(replacement, text)
        if n:
            STATS[name] += n
    return text


_TEXT_KEYS = {"text", "content", "system", "instructions", "input"}


def _deep(obj: Any) -> Any:
    """Rewrite the prompt text inside an SDK payload (strings, message lists, content blocks)."""
    if isinstance(obj, str):
        return rewrite(obj)
    if isinstance(obj, list):
        return [_deep(x) for x in obj]
    if isinstance(obj, dict):
        return {k: (_deep(v) if k in _TEXT_KEYS or k == "messages" else v) for k, v in obj.items()}
    return obj


def _wrap_kwargs(fn):
    @functools.wraps(fn)
    def sync(*a, **kw):
        return fn(*a, **{k: (_deep(v) if k in ("system", "messages", "input", "instructions") else v) for k, v in kw.items()})

    @functools.wraps(fn)
    async def asyn(*a, **kw):
        return await fn(*a, **{k: (_deep(v) if k in ("system", "messages", "input", "instructions") else v) for k, v in kw.items()})

    import inspect

    return asyn if inspect.iscoroutinefunction(fn) else sync


def install() -> None:
    if os.environ.get("CONSUMER_FRAMING", "1").strip() in ("0", "false", "no"):
        return
    try:
        from anthropic.resources.messages import messages as am

        for cls in (am.Messages, am.AsyncMessages):
            for name in ("create", "stream"):
                if hasattr(cls, name) and not getattr(getattr(cls, name), "_consumer_framing", False):
                    wrapped = _wrap_kwargs(getattr(cls, name))
                    wrapped._consumer_framing = True
                    setattr(cls, name, wrapped)
    except ImportError:
        pass
    try:
        from openai.resources.chat import completions as oc
        from openai.resources import responses as orr

        targets = [(oc.Completions, "create"), (oc.AsyncCompletions, "create"),
                   (orr.Responses, "create"), (orr.AsyncResponses, "create")]
        for cls, name in targets:
            if hasattr(cls, name) and not getattr(getattr(cls, name), "_consumer_framing", False):
                wrapped = _wrap_kwargs(getattr(cls, name))
                wrapped._consumer_framing = True
                setattr(cls, name, wrapped)
    except ImportError:
        pass


def install_cli(llm_backends) -> None:
    """Same rewrite for the Claude Code / ChatGPT login adapters."""
    if os.environ.get("CONSUMER_FRAMING", "1").strip() in ("0", "false", "no"):
        return
    if getattr(llm_backends.run_sync, "_consumer_framing", False):
        return
    real_sync, real_async = llm_backends.run_sync, llm_backends.run_async

    def fix(system, blocks):
        return rewrite(system) if system else system, [
            {**b, "text": rewrite(b["text"])} if b.get("type") == "text" else b for b in blocks
        ]

    def run_sync(system, blocks, model_hint, effort=None):
        system, blocks = fix(system, blocks)
        return real_sync(system, blocks, model_hint, effort)

    async def run_async(system, blocks, model_hint, effort=None):
        system, blocks = fix(system, blocks)
        return await real_async(system, blocks, model_hint, effort)

    run_sync._consumer_framing = run_async._consumer_framing = True
    llm_backends.run_sync, llm_backends.run_async = run_sync, run_async
