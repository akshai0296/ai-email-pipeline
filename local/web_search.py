"""Find web pages about something, for graph lookups that go beyond the brand's own site.

Only URLs come back from here; graph_modify fetches each page itself (through the egress
guard) and checks every quote against the page text, so a search result can't put an
unchecked fact into a graph. Where the URLs come from:

    BRAVE_SEARCH_API_KEY set   → Brave Search API
    Claude Code login          → the claude CLI with its WebSearch tool only
    API keys                   → the Messages API web search tool
    always                     → Wikipedia's search API (free, no key)

The ChatGPT login has no search here, so it gets Brave (if set) and Wikipedia.
"""

from __future__ import annotations

import json
import os
import re
from typing import Callable, Optional
from urllib.parse import quote_plus, urlparse

import bootstrap

WIKI_UA = "ai-email-pipeline/1.0 (https://github.com/akshai0296/ai-email-pipeline)"  # name + contact, per Wikimedia policy

SEARCH_PROMPT = """Search the web for pages that answer this, about {subject}:
"{request}"

Prefer the company's own site ({domain}), then retailers, then established news and review sites.
Skip forums, social media, video sites and pages that need a login.
Answer with JSON only: {{"urls": ["<up to {n} result URLs, most useful first>"]}}"""


def _urls_from(text: str, n: int) -> list[str]:
    m = re.search(r"\{.*\}", text, re.S)
    urls = []
    if m:
        try:
            urls = [u for u in json.loads(m.group(0)).get("urls", []) if isinstance(u, str)]
        except ValueError:
            pass
    urls = urls or re.findall(r"https://[^\s\"'<>)\]]+", text)
    return [u for u in dict.fromkeys(urls) if u.startswith("https://")][:n]


async def _brave(fetcher, query: str, n: int) -> list[str]:
    import httpx

    key = os.environ["BRAVE_SEARCH_API_KEY"]
    bootstrap.ALLOWED_HOSTS.add("api.search.brave.com")
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": n},
                             headers={"X-Subscription-Token": key, "Accept": "application/json"})
        r.raise_for_status()
    return [x["url"] for x in r.json().get("web", {}).get("results", [])][:n]


async def _claude_code(prompt: str, n: int) -> list[str]:
    import llm_backends

    return _urls_from(await llm_backends.claude_web_search(prompt), n)


async def _api(prompt: str, n: int) -> list[str]:
    import anthropic

    msg = await anthropic.AsyncAnthropic().messages.create(
        model=os.environ.get("BRAND_GRAPH_MODEL", "claude-sonnet-4-6"), max_tokens=1500,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(getattr(b, "text", "") or "" for b in msg.content)
    found = _urls_from(text, n)
    for b in msg.content:  # fall back to the raw results when the answer has no list
        for r in getattr(b, "content", None) or []:
            url = getattr(r, "url", None)
            if url and url not in found and len(found) < n:
                found.append(url)
    return found


async def _wikipedia(fetcher, subject: str, n: int) -> list[str]:
    import httpx

    bootstrap.ALLOWED_HOSTS.add("en.wikipedia.org")
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": WIKI_UA}) as client:
        r = await client.get("https://en.wikipedia.org/w/api.php",
                             params={"action": "query", "list": "search", "format": "json", "srlimit": n, "srsearch": subject})
        r.raise_for_status()
    hits = r.json().get("query", {}).get("search", [])
    return [f"https://en.wikipedia.org/wiki/{quote_plus(h['title'].replace(' ', '_'), safe='_()')}" for h in hits]


def headers_for(url: str) -> Optional[dict]:
    """Wikipedia asks bots to name themselves; other sites get the normal browser headers."""
    host = urlparse(url).hostname or ""
    return {"User-Agent": WIKI_UA} if host == "wikipedia.org" or host.endswith(".wikipedia.org") else None


async def find_pages(request: str, subject: str, domain: str, fetcher, log: Callable[[str], None], n: int = 4,
                     name: str = "") -> list[str]:
    """Up to n URLs (from any site) that may answer the request about the subject (name: the plain product or
    company name, for Wikipedia's search)."""
    prompt = SEARCH_PROMPT.format(subject=subject, request=request, domain=domain or "unknown", n=n)
    urls: list[str] = []
    tries = []
    if os.environ.get("BRAVE_SEARCH_API_KEY"):
        tries.append(("Brave", lambda: _brave(fetcher, f"{subject} {request}", n)))
    if bootstrap.LLM_BACKEND == "claude_code":
        tries.append(("Claude web search", lambda: _claude_code(prompt, n)))
    elif bootstrap.LLM_BACKEND == "api":
        tries.append(("web search", lambda: _api(prompt, n)))
    for name, run in tries:
        try:
            got = await run()
            log(f"Web search ({name}): {len(got)} result{'s' if len(got) != 1 else ''}")
            urls += got
        except Exception as exc:
            log(f"  {name} search failed: {str(exc).splitlines()[0][:120]}")
        if len(urls) >= n:
            break
    try:
        urls += await _wikipedia(fetcher, name or subject, 1)
    except Exception as exc:
        log(f"  Wikipedia search failed: {str(exc).splitlines()[0][:120]}")
    skip = re.compile(r"(^|\.)(youtube|facebook|instagram|tiktok|x|twitter|reddit|pinterest|linkedin)\.com$")
    return [u for u in dict.fromkeys(urls) if not skip.search(urlparse(u).hostname or "")][: n + 1]
