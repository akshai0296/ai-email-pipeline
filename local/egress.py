"""Which model APIs the Python processes may reach.

A provider's API host is reachable only when its key is set. On a Claude Code or
ChatGPT login the model calls go through the CLI (a separate process), so a
backend step that falls back to another provider (the Gemini backup in visual QA,
say) fails locally instead of sending the email to a provider you never set up.
"""

from __future__ import annotations

from collections.abc import Mapping

MODEL_API_HOSTS = {
    "ANTHROPIC_API_KEY": "api.anthropic.com",
    "OPENAI_API_KEY": "api.openai.com",
    "GOOGLE_API_KEY": "generativelanguage.googleapis.com",
}


def model_api_hosts(environ: Mapping[str, str]) -> set[str]:
    """The API hosts whose keys are set (bootstrap fills missing keys with "unset")."""
    return {host for key, host in MODEL_API_HOSTS.items() if environ.get(key, "unset") not in ("", "unset")}
