"""Write or improve an email brief from the saved company and product graphs.

    write    an idea (or nothing) + brand + product + audience → a full brief
    improve  the user's own brief → the same intent, made specific and complete

The brief stays plain, editable text: this only fills or rewrites the box. It
uses what the graphs know (voice, tagline, product facts, the photos on file)
and is told never to add facts they don't contain.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

import brand_graph as bg
import product_graph as pg

# The brief checklist. The UI's meter uses these same patterns (served by /api/brief/templates), and every brief
# the writer returns is checked against them, so a written brief shows every item met.
CHECKS = [
    ("Purpose", r"\b(launch|announce|introduc|remind|welcome|sale|back in stock|restock|new|win|celebrat|invite|thank|season|holiday"
                r"|why|story|promot|feature|highlight|showcase|spotlight|gift|collection|offer|update|news|follow.?up|re.?engage"
                r"|abandon|cart|last call|teaser|reveal|bestseller|fall|autumn|spring|summer|winter)",
     "say what the email is for (a launch, a restock, a welcome, a seasonal email...)"),
    ("Layout", r"\b(headline|hero|section|paragraph|block|grid|side by side|open (with|on)|then|close)",
     "describe the order of sections (open with..., then..., close with...)"),
    ("Images", r"\b(image|photo|shot|picture|visual)", "say which photo goes where"),
    ("CTA label", r"(cta|button)[^.]*[\"“'‘][^\"”'’]{2,40}[\"”'’]|[\"“][^\"”]{2,40}[\"”][^.]*(cta|button)",
     'give the button an exact label in double quotes, e.g. a "Shop now" button'),
]
LENGTH = (25, 220)


def checklist(text: str, grounded: bool) -> list[dict]:
    """Each checklist item: met or not, and what to do when it isn't."""
    words = len(text.split())
    out = [{"name": n, "ok": bool(re.search(p, text, re.I)), "tip": tip} for n, p, tip in CHECKS]
    out.append({"name": "Length", "ok": LENGTH[0] <= words <= LENGTH[1],
                "tip": f"add detail ({LENGTH[0]}+ words)" if words < LENGTH[0] else f"trim it (under ~{LENGTH[1]} words)"})
    out.append({"name": "Grounded", "ok": grounded, "tip": "pick a company so facts come from its graph"})
    return out


def missing(text: str) -> list[dict]:
    """What a brief still needs that writing can fix (Grounded depends on picking a company, not on the text)."""
    return [c for c in checklist(text, True) if not c["ok"]]


SYSTEM = """You write briefs for an AI email designer. A brief says what one marketing email is for and how it is laid out.
A good brief: one line of purpose and mood, then the layout top to bottom (headline, which image where, copy blocks, captions, CTA with its exact label).
Plain text, 60-140 words, no headings or bullet lists, no subject lines.
Every brief covers all of these, and they are checked:
- Purpose: the first sentence names what the email is for in a plain word (launch, restock, welcome, seasonal, reminder, introduce...).
- Layout: the sections in order, e.g. "open with ..., then ..., close with ...".
- Images: which photo goes where (say "photo" or "shot").
- CTA: one button with its exact label in double quotes, e.g. a "Shop the Wool Runner" button.
Use only facts given to you. Never invent prices, specs, discounts, dates or claims."""

FIX_SYSTEM = """You complete email briefs. Keep the brief's intent, facts and order, and add what it is missing.
The result reads like a normal brief written in one go: plain flowing text, 60-140 words, no labels or headings
("Layout:", "CTA:"), and never mentions a checklist or word counts. Never add facts that aren't in the brief or the context."""


async def _ask_brief(system: str, prompt: str, tag: str = "brief") -> list[str]:
    """Briefs as plain text between <brief> tags. (Not JSON: a brief quotes its button label, and models often
    leave those quotes unescaped inside a JSON string.)"""
    import anthropic  # the CLI backends swap this for their own client

    msg = await anthropic.AsyncAnthropic().messages.create(
        model=bg.MODEL, max_tokens=3000, system=system, messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    )
    text = "".join(getattr(b, "text", "") for b in msg.content)
    return [b.strip() for b in re.findall(rf"<{tag}>(.*?)</{tag}>", text, re.S) if b.strip()]


async def complete(brief: str, context: str = "", attempts: int = 2) -> str:
    """Rewrite a brief until every checklist item the text can fix is met (or attempts run out)."""
    for _ in range(attempts):
        miss = missing(brief)
        if not miss:
            break
        todo = "\n".join(f"- {c['tip']}" for c in miss)
        out = await _ask_brief(FIX_SYSTEM, f"""Brief:
{brief}

Rewrite it so that it also does these:
{todo}

Context (the only facts you may use): {context[:4000] or '(none)'}

Write the full rewritten brief between <brief> and </brief>, nothing else.""")
        if out:
            brief = out[0]
    return brief


def _context(brand_id: Optional[str], product_id: Optional[str]) -> str:
    parts = []
    if brand_id:
        g = bg.load(brand_id)
        c = bg.company(g)
        parts.append(
            f"Company: {c.get('name')} ({g.get('domain')}). {c.get('description', '')}\n"
            f"Tagline: {c.get('tagline') or '(none)'}\n"
            f"Voice: {(bg.node(g, 'voice') or {}).get('text', '(none)')}\n"
            f"Style rules: {json.dumps([n['text'] for n in bg.nodes(g, 'Rule')])}\n"
            f"Collections: {json.dumps([n['name'] for n in bg.nodes(g, 'Collection')])}"
            + (f"\nMore about the company, by topic: {json.dumps(bg.branches(g))}" if bg.branches(g) else "")
        )
    if brand_id and product_id:
        p = pg.load(brand_id, product_id)
        v = pg.values(p)
        parts.append(
            f"Product: {v['name']}{' at ' + v['price'] if v['price'] else ''}\n"
            f"About it: {v['description']}\n"
            f"Facts: {json.dumps(v['features'])}\n"
            f"Variants: {json.dumps(v['variants'])}\n"
            + (f"More facts by topic: {json.dumps(v['branches'])}\n" if v["branches"] else "")
            + "Product photos on file (the email will get these): "
            + json.dumps([f"{i['role']}: {i['description']}" for i in v["images"]])
        )
    return "\n\n".join(parts) or "No company or product chosen."


TEMPLATES = {
    "launch": ("Product launch", "Announce something new: a big headline naming it, the hero product shot, a short paragraph on what is new, "
               "two or three key facts as short lines, a detail photo, one CTA to the product."),
    "restock": ("Back in stock", "Tell people a favorite is back: headline that it's back, hero shot, one line on why people love it, "
                "availability only if known, one CTA to get it."),
    "winback": ("Win-back", "Bring a past customer back: a warm personal headline, a reminder of what they liked about the brand, "
                "one clear reason to return now (something new or a benefit), product shot, one CTA."),
    "seasonal": ("Seasonal / holiday", "Tie the product to the season or a holiday: seasonal headline, gift or occasion angle, hero shot, "
                 "short copy, one CTA. Mention an offer only if the brief gives one."),
    "welcome": ("Welcome", "Welcome a new subscriber: welcoming headline, the brand story in two sentences, two or three things that make "
                "the products different, one bestseller with its photo, one CTA to shop."),
    "collection": ("New collection", "Introduce a collection: collection headline, hero image, two product photos side by side with short "
                   "captions, one CTA to the collection."),
    "spotlight": ("Product spotlight", "Explain one product in depth: benefit-led headline, hero shot, three short blocks each with one fact "
                  "and a photo where available, one CTA."),
    "cart": ("Cart reminder", "Remind someone about a product they looked at: friendly reminder headline, product photo, name and price, "
             "two reassurance points from the facts (e.g. shipping, care), one CTA to finish the order."),
}

SEQUENCES = {
    "welcome": ("Welcome series", [("Welcome", "day 0", "welcome and brand story"), ("Bestsellers", "day 2", "the products people love most"),
                                   ("Why us", "day 5", "what makes the brand different, with a gentle nudge to shop")]),
    "launch": ("Launch", [("Teaser", "3 days before", "something is coming, build curiosity without full details"),
                          ("Launch day", "launch day", "full reveal with facts and photos"),
                          ("Last call", "4 days after", "reminder for people who haven't clicked, lead with the strongest fact")]),
    "winback": ("Win-back", [("We miss you", "day 0", "warm check-in, remind them what they liked"),
                             ("What's new", "day 4", "what changed since they last bought"),
                             ("Last nudge", "day 9", "one final, short reason to come back")]),
    "cart": ("Cart recovery", [("Still thinking?", "1 hour after", "friendly reminder with the product"),
                               ("Answers", "1 day after", "answer likely doubts using the facts")]),
}


async def write(mode: str, text: str, audience: str, audience_description: str,
                brand_id: Optional[str] = None, product_id: Optional[str] = None,
                uploaded: Optional[list[dict]] = None, template: str = "") -> str:
    uploads = [f"{u.get('role', 'image')}: {u.get('description') or 'no description'}" for u in (uploaded or [])]
    task = (
        "Rewrite the user's brief below into a complete brief. Keep their intent, wording they chose, structure and CTA "
        "if they gave one; fill in what is missing (layout order, image placement, CTA label)."
        if mode == "improve" else
        "Write a brief for one email. If the user gave an idea below, build the email around it; "
        "otherwise pick the most natural email for this audience and product (e.g. a launch, a reminder, a story)."
    )
    if template in TEMPLATES:
        name, layout = TEMPLATES[template]
        task += f"\n\nCampaign template: {name}. Typical shape: {layout} Adapt it to the product and the user's idea."
    prompt = f"""{task}

Audience: {audience}{' (' + audience_description + ')' if audience_description else ''}

{_context(brand_id, product_id)}

Images the user uploaded: {json.dumps(uploads) if uploads else "(none)"}

User's {'brief' if mode == 'improve' else 'idea'}: {text.strip() or '(none)'}

Refer to photos by what they show (e.g. "the angled hero shot"), and only to photos listed above.
Write the brief between <brief> and </brief>, nothing else."""
    out = await _ask_brief(SYSTEM, prompt)
    if not out:
        raise ValueError("the model returned an empty brief")
    brief = out[0]
    return await complete(brief, _context(brand_id, product_id))


async def write_sequence(kind: str, text: str, audience: str, audience_description: str,
                         brand_id: Optional[str] = None, product_id: Optional[str] = None,
                         uploaded: Optional[list[dict]] = None) -> list[dict]:
    """Briefs for a short series of emails that tell one story, each editable before generating."""
    if kind not in SEQUENCES:
        raise ValueError(f"unknown sequence {kind!r}")
    title, steps = SEQUENCES[kind]
    plan = "\n".join(f"{i + 1}. {name} (send {when}): {job}" for i, (name, when, job) in enumerate(steps))
    uploads = [f"{u.get('role', 'image')}: {u.get('description') or 'no description'}" for u in (uploaded or [])]
    prompt = f"""Write the briefs for a {title} sequence of {len(steps)} emails that read as one story:
{plan}

Each email must have a distinct job and headline idea, avoid repeating the same opening, and build on the previous one.

Audience: {audience}{' (' + audience_description + ')' if audience_description else ''}

{_context(brand_id, product_id)}

Images the user uploaded: {json.dumps(uploads) if uploads else "(none)"}

User's idea for the sequence: {text.strip() or '(none)'}

Write each email's brief between <brief> and </brief>, exactly {len(steps)} of them, in order, nothing else."""
    briefs = await _ask_brief(SYSTEM, prompt)
    if len(briefs) != len(steps):
        raise ValueError(f"the model returned {len(briefs)} briefs instead of {len(steps)}")
    emails = [{"name": name, "send": when, "brief": b} for b, (name, when, _) in zip(briefs, steps)]
    context = _context(brand_id, product_id)
    fixed = await asyncio.gather(*[complete(e["brief"].strip(), context) for e in emails])
    for e, brief in zip(emails, fixed):
        e["brief"] = brief
        e["checks"] = checklist(brief, bool(brand_id))
    return emails
