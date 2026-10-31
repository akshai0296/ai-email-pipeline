"""Run the pipeline's model calls on a local CLI login instead of API keys.

    claude_code → `claude -p`    on your Claude Code login (Claude Pro/Max)
    chatgpt     → `codex exec`   on your ChatGPT login (Plus/Pro/Team)

Every call is one locked-down CLI run: one turn, nothing saved to session
history, images passed along, no API key in its environment. Claude Code runs
with no tools, MCP servers, CLAUDE.md, skills or hooks. Codex runs in a
read-only sandbox in an empty folder, told not to run commands, without your
config.toml or rules.

Installed by bootstrap when LLM_BACKEND is claude_code or chatgpt. Replaces:
  - langchain_anthropic.ChatAnthropic
  - anthropic.Anthropic / anthropic.AsyncAnthropic (messages.create)
  - langchain_openai.ChatOpenAI, when no OpenAI key is set

Custom tools work through the LangChain replacement by a text protocol: the
tool list goes into the system prompt, the model writes <tool_call> blocks, and
they come back as ordinary tool_calls on the AIMessage. Our own code runs the
tools; the CLIs still run with every built-in tool switched off. This is what
lets the designer agent (designer_tools/) run on a login. Server tools (web
search and the like) and tools passed to the raw anthropic SDK replacement
still raise ToolUseUnsupported.

For your own use only: neither Anthropic nor OpenAI allows products used by
other people to run on consumer logins. A hosted version needs API keys.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import tomllib
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

BACKENDS = ("claude_code", "chatgpt")
CONCURRENCY = int(os.environ.get("CLI_LLM_CONCURRENCY", os.environ.get("CLAUDE_CODE_CONCURRENCY", "3")))
TIMEOUT_S = int(os.environ.get("CLI_LLM_TIMEOUT", os.environ.get("CLAUDE_CODE_TIMEOUT", "900")))
DEFAULT_SYSTEM = (
    "You are a precise assistant running inside an automated content pipeline. "
    "Follow the instructions in the user message exactly and reply with only the requested output."
)
_EFFORTS = {"low", "medium", "high"}
_CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _effort(step_effort: Optional[str]) -> Optional[str]:
    """The effort chosen under Model access (LLM_EFFORT) wins over each step's own."""
    return os.environ.get("LLM_EFFORT", "").strip() or step_effort
_WORKDIR = tempfile.mkdtemp(prefix="cli-llm-")
_thread_sem = threading.BoundedSemaphore(CONCURRENCY)
_async_sems: dict[int, asyncio.Semaphore] = {}

ACTIVE: Optional[str] = None
STATS = {"calls": 0, "failures": 0, "tool_calls_refused": 0, "tool_turns": 0, "tool_call_retries": 0}


class CLILLMError(RuntimeError):
    pass


class ToolUseUnsupported(CLILLMError):
    pass


# ── binaries, models, environment ─────────────────────────────────────────────


def claude_bin() -> str:
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def _version(path: str) -> tuple:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ()
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else ()


_codex_bin_cache: Optional[str] = None


def codex_bin() -> str:
    """CODEX_BIN, else the newest of: codex on PATH, the one bundled in ChatGPT.app, Codex.app."""
    global _codex_bin_cache
    if os.environ.get("CODEX_BIN"):
        return os.environ["CODEX_BIN"]
    if _codex_bin_cache:
        return _codex_bin_cache
    candidates = [shutil.which("codex"), "/Applications/ChatGPT.app/Contents/Resources/codex", "/Applications/Codex.app/Contents/Resources/codex"]
    found = [c for c in candidates if c and Path(c).exists()]
    _codex_bin_cache = max(found, key=_version) if found else "codex"
    return _codex_bin_cache


def chatgpt_model() -> Optional[str]:
    """CHATGPT_MODEL, else `model` from ~/.codex/config.toml, else Codex's default."""
    if os.environ.get("CHATGPT_MODEL"):
        return os.environ["CHATGPT_MODEL"]
    cfg = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        return tomllib.loads(cfg.read_text()).get("model")
    except Exception:
        return None


def claude_model(name: Optional[str]) -> str:
    forced = os.environ.get("CLAUDE_CODE_MODEL", "").strip()
    if forced:
        return forced
    n = (name or "").lower()
    if "haiku" in n:
        return "haiku"
    if "sonnet" in n:
        return "sonnet"
    if "opus" in n or "fable" in n:
        return "opus"
    return "sonnet"  # GPT / o-series helper steps routed here


def _child_env() -> dict[str, str]:
    """Drop API keys so the CLIs use their logins, and drop variables inherited
    from a parent Claude Code session."""
    env = {}
    for k, v in os.environ.items():
        if k.startswith(("ANTHROPIC_", "OPENAI_")) or k == "CLAUDECODE":
            continue
        if k.startswith(("CLAUDE_CODE_", "CLAUDE_AGENT_SDK", "CLAUDE_EFFORT", "CLAUDE_PID", "CLAUDE_PREVIEW")):
            continue
        env[k] = v
    return env


# ── content conversion ────────────────────────────────────────────────────────


def _image_block_from_url(url: str) -> dict:
    if url.startswith("data:"):
        header, b64 = url.split(",", 1)
        media = header[5:].split(";")[0] or "image/png"
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}}
    import httpx

    resp = httpx.get(url, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    media = resp.headers.get("content-type", "").split(";")[0] or mimetypes.guess_type(url)[0] or "image/png"
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": base64.b64encode(resp.content).decode()}}


def _to_blocks(content: Any, tool_names: Optional[dict] = None) -> list[dict]:
    """Anthropic-style blocks for the CLIs. tool_use / tool_result blocks become
    text in the <tool_call> protocol; tool_names maps tool_use ids to names."""
    tool_names = {} if tool_names is None else tool_names
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, dict):
        content = [content]
    blocks: list[dict] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            blocks.append({"type": "text", "text": str(item)})
            continue
        t = item.get("type")
        if t == "text":
            blocks.append({"type": "text", "text": item.get("text", "")})
        elif t == "image" and isinstance(item.get("source"), dict):
            src = item["source"]
            blocks.append(_image_block_from_url(src["url"]) if src.get("type") == "url" else {"type": "image", "source": src})
        elif t == "image" and item.get("source_type") == "base64":  # LangChain standard block
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": item.get("mime_type", "image/png"), "data": item["data"]}})
        elif t == "image" and item.get("url"):
            blocks.append(_image_block_from_url(item["url"]))
        elif t == "image_url":
            iu = item.get("image_url")
            blocks.append(_image_block_from_url(iu["url"] if isinstance(iu, dict) else iu))
        elif t == "document":
            blocks.append({k: v for k, v in item.items() if k in ("type", "source", "title", "context")})
        elif t == "tool_use":
            tool_names[item.get("id")] = item.get("name")
            blocks.append({"type": "text", "text": _render_tool_call(item.get("name"), item.get("id"), item.get("input") or {})})
        elif t == "tool_result":
            tid = item.get("tool_use_id")
            error = ' error="true"' if item.get("is_error") else ""
            blocks.append({"type": "text", "text": f'<tool_result name="{tool_names.get(tid, "")}" id="{tid}"{error}>\n'})
            blocks.extend(_to_blocks(item.get("content"), tool_names))
            blocks.append({"type": "text", "text": "\n</tool_result>"})
        elif t in ("thinking", "redacted_thinking"):
            continue
        else:
            blocks.append({"type": "text", "text": json.dumps(item, default=str)})
    return blocks


def _flatten(turns: list[tuple[str, Any]]) -> list[dict]:
    """One user message. Multi-turn histories become a labelled transcript."""
    if len(turns) == 1:
        return _to_blocks(turns[0][1])
    blocks: list[dict] = []
    tool_names: dict = {}
    for role, content in turns:
        blocks.append({"type": "text", "text": f"\n[{role}]\n"})
        blocks.extend(_to_blocks(content, tool_names))
    return blocks


# ── tool calls as text ────────────────────────────────────────────────────────
# The CLIs can't take custom tool definitions, so the tools are described in the
# system prompt and the model writes each call as a <tool_call> block. Arguments
# go in <param> tags rather than one JSON object: string arguments (whole MJML
# sections, say) are taken verbatim, so the model never has to escape quotes and
# newlines inside JSON.

_TOOLS_PROMPT = """\
# Tools

You can call these tools:

{tools}

To call a tool, write a block in exactly this form. Put each argument in its own <param> tag. Write string arguments as plain text, \
with nothing escaped; write numbers, booleans, arrays and objects as JSON:

<tool_call name="tool_name">
<param name="argument_name">value</param>
</tool_call>

Rules:
- You may write some text first, then one or more <tool_call> blocks. End your reply after the last block. The results come back in \
the next [user] turn, inside <tool_result> blocks.
- Call only the tools listed above, with the arguments their schema describes.
- When you don't need a tool, reply normally, with no <tool_call> block.
- Earlier turns may appear as a transcript labelled [user] and [assistant]. Write only your next [assistant] turn, without the label.
{choice}"""

_CALL_RE = re.compile(r'<tool_call\s+name="([^"]+)"[^>]*>(.*?)</tool_call>', re.S)
_PARAM_RE = re.compile(r'<param\s+name="([^"]+)"\s*>(.*?)</param>', re.S)


def _tool_specs(tools: Any) -> list[dict]:
    """Anthropic-format specs ({name, description, input_schema}) from Anthropic dicts,
    OpenAI dicts, pydantic models or functions."""
    specs: list[dict] = []
    for tool in tools or []:
        if isinstance(tool, dict) and "input_schema" in tool:
            specs.append({"name": tool["name"], "description": tool.get("description", ""), "input_schema": tool["input_schema"]})
            continue
        if isinstance(tool, dict) and tool.get("type") not in (None, "function"):
            _refuse_tools()  # an Anthropic server tool such as web search: only the API runs those
        from langchain_core.utils.function_calling import convert_to_openai_tool

        fn = convert_to_openai_tool(tool)["function"]
        specs.append({"name": fn["name"], "description": fn.get("description", ""),
                      "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return specs


def _choice_rule(choice: Any) -> tuple[str, bool]:
    """(prompt line, whether a tool call is required) for Anthropic or OpenAI style tool_choice."""
    name = None
    if isinstance(choice, dict):
        kind = choice.get("type")
        if kind == "any":
            return "- This turn you must call at least one tool.", True
        if kind == "none":
            return "- Don't call any tool this turn.", False
        name = choice.get("name") if kind == "tool" else (choice.get("function") or {}).get("name") if kind == "function" else None
    elif choice in ("any", "required") or choice is True:
        return "- This turn you must call at least one tool.", True
    elif choice == "none":
        return "- Don't call any tool this turn.", False
    elif isinstance(choice, str) and choice != "auto":
        name = choice
    if name:
        return f"- This turn you must call the tool {name}.", True
    return "", False


def _tools_system(system: Optional[str], specs: list[dict], choice: Any) -> str:
    tools = "\n\n".join(
        f'<tool name="{s["name"]}">\n{(s.get("description") or "").strip()}\n'
        f'Arguments (JSON Schema): {json.dumps(s.get("input_schema") or {}, ensure_ascii=False)}\n</tool>'
        for s in specs
    )
    return f"{(system or DEFAULT_SYSTEM).strip()}\n\n{_TOOLS_PROMPT.format(tools=tools, choice=_choice_rule(choice)[0])}"


def _render_tool_call(name: Any, tool_id: Any, args: dict) -> str:
    params = "".join(
        f'<param name="{k}">{v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}</param>\n' for k, v in args.items()
    )
    return f'<tool_call name="{name}" id="{tool_id}">\n{params}</tool_call>'


def _is_string_param(schema: dict) -> bool:
    kind = (schema or {}).get("type")
    return kind == "string" or (isinstance(kind, list) and "string" in kind)


def _param_value(raw: str, schema: Optional[dict]) -> Any:
    if schema is not None and _is_string_param(schema):
        return raw.removeprefix("\n").removesuffix("\n")
    try:
        return json.loads(raw)
    except ValueError:
        return raw.strip()


def _parse_tool_calls(text: str, specs: list[dict]) -> tuple[str, list[dict]]:
    """(the reply's text with the blocks removed, LangChain tool_calls)."""
    props = {s["name"]: (s.get("input_schema") or {}).get("properties") or {} for s in specs}
    calls = []
    for m in _CALL_RE.finditer(text):
        name, body = m.group(1), m.group(2)
        args = {k: _param_value(v, props.get(name, {}).get(k)) for k, v in _PARAM_RE.findall(body)}
        if not args and body.strip().startswith("{"):  # a model that wrote JSON anyway
            try:
                parsed = json.loads(body)
                args = parsed.get("input", parsed) if isinstance(parsed, dict) else {}
            except ValueError:
                pass
        calls.append({"name": name, "args": args, "id": f"toolu_cli_{uuid.uuid4().hex[:16]}", "type": "tool_call"})
    if calls:  # anything after the last call is the model guessing at results; drop it
        text = text[: text.rindex("</tool_call>") + len("</tool_call>")]
    clean = _CALL_RE.sub("", text)
    clean = re.sub(r"```[a-z]*\s*```", "", clean).strip()  # fences left empty around removed blocks
    return clean, calls


def _tool_reply_error(text: str, choice: Any) -> Optional[str]:
    """Why a reply can't be used as-is, or None."""
    if text.count("<tool_call") != len(_CALL_RE.findall(text)):
        return 'a <tool_call> block was not closed with </tool_call>, or its opening tag had no name="..."'
    if _choice_rule(choice)[1] and not _CALL_RE.search(text):
        return "this turn needed a tool call and the reply had none"
    return None


def _retry_turns(turns: list[tuple[str, Any]], reply: str, error: str) -> list[tuple[str, Any]]:
    STATS["tool_call_retries"] += 1
    note = f"Your last reply couldn't be used: {error}. Send the whole reply again, writing each tool call exactly in the <tool_call> form."
    return [*turns, ("assistant", reply), ("user", note)]


# ── Claude Code ───────────────────────────────────────────────────────────────


def _claude_job(system: Optional[str], blocks: list[dict], model_hint: str, effort: Optional[str], tmp: Path):
    sys_file = tmp / "system.txt"
    sys_file.write_text(system.strip() if system and system.strip() else DEFAULT_SYSTEM)
    cmd = [
        claude_bin(), "-p",
        "--output-format", "stream-json", "--verbose",
        "--input-format", "stream-json",
        "--system-prompt-file", str(sys_file),
        "--tools", "",
        "--disallowedTools", "mcp__*",
        "--strict-mcp-config",
        "--safe-mode",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--max-turns", "1",
        "--model", claude_model(model_hint),
    ]
    effort = _effort(effort)
    if effort in _CLAUDE_EFFORTS:
        cmd += ["--effort", effort]
    payload = (json.dumps({"type": "user", "message": {"role": "user", "content": blocks or [{"type": "text", "text": "(empty)"}]}}) + "\n").encode()
    return cmd, payload


def _claude_parse(stdout: str, stderr: str, returncode: int, tmp: Path) -> str:
    result = None
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "result":
                result = ev
    if result is None:
        raise CLILLMError(f"claude CLI returned no result (exit {returncode}): {(stderr or stdout)[-600:]}")
    if result.get("is_error") or result.get("subtype") != "success":
        raise CLILLMError(f"claude CLI error ({result.get('subtype')}): {str(result.get('result'))[:600]}")
    return result.get("result") or ""


# ── ChatGPT (Codex) ───────────────────────────────────────────────────────────

_CODEX_PREAMBLE = (
    "Answer directly from this message. Do not run shell commands, read or write files, "
    "or browse; none are needed."
)


def _codex_job(system: Optional[str], blocks: list[dict], model_hint: str, effort: Optional[str], tmp: Path):
    parts = [f"<system>\n{_CODEX_PREAMBLE}\n\n{(system or DEFAULT_SYSTEM).strip()}\n</system>\n"]
    images: list[str] = []
    for b in blocks:
        if b["type"] == "text":
            parts.append(b["text"])
        elif b["type"] == "image" and b["source"].get("type") == "base64":
            ext = mimetypes.guess_extension(b["source"].get("media_type", "image/png")) or ".png"
            path = tmp / f"image-{len(images) + 1}{ext}"
            path.write_bytes(base64.b64decode(b["source"]["data"]))
            images.append(str(path))
            parts.append(f"[attached image {len(images)}]")
        elif b["type"] == "document":
            parts.append("[a PDF document was supplied here but can't be attached through ChatGPT login mode]")
    cmd = [
        codex_bin(), "exec",
        "--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
        "-s", "read-only", "--color", "never",
        "-C", str(tmp),
        "-c", f"model_reasoning_effort={_effort(effort) if _effort(effort) else 'medium'}",
        "-o", str(tmp / "reply.txt"),
    ]
    model = chatgpt_model()
    if model:
        cmd += ["-m", model]
    for img in images:
        cmd += ["-i", img]
    cmd.append("-")
    return cmd, "\n".join(parts).encode()


def _codex_parse(stdout: str, stderr: str, returncode: int, tmp: Path) -> str:
    reply = tmp / "reply.txt"
    if returncode == 0 and reply.exists():
        return reply.read_text()
    errors = [ln for ln in (stdout + "\n" + stderr).splitlines() if "ERROR" in ln or "error" in ln.lower()]
    raise CLILLMError(f"codex exec failed (exit {returncode}): {(' | '.join(errors) or (stderr or stdout))[-600:]}")


_JOBS = {"claude_code": (_claude_job, _claude_parse), "chatgpt": (_codex_job, _codex_parse)}


async def claude_web_search(prompt: str, timeout_s: float = 120) -> str:
    """One Claude Code call that may use its WebSearch tool and nothing else (no files, shell or fetch).

    Used to find pages for a graph lookup; the pages themselves are then fetched and checked by
    our own code, so a quote from them can be verified.
    """
    with tempfile.TemporaryDirectory(dir=_WORKDIR) as d:
        tmp = Path(d)
        cmd, payload = _claude_job("You find web pages with a web search. Answer with JSON only.",
                                   [{"type": "text", "text": prompt}], "sonnet", None, tmp)
        cmd[cmd.index("--tools") + 1] = "WebSearch"
        cmd[cmd.index("--max-turns") + 1] = "4"
        cmd += ["--allowedTools", "WebSearch"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=tmp, env=_child_env(),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(payload), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            raise CLILLMError(f"web search timed out after {timeout_s:.0f}s")
        return _claude_parse(out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode, tmp)


def run_sync(system: Optional[str], blocks: list[dict], model_hint: str, effort: Optional[str] = None) -> str:
    build, parse = _JOBS[ACTIVE]
    STATS["calls"] += 1
    with tempfile.TemporaryDirectory(dir=_WORKDIR) as d:
        tmp = Path(d)
        cmd, payload = build(system, blocks, model_hint, effort, tmp)
        try:
            with _thread_sem:
                proc = subprocess.run(cmd, input=payload, capture_output=True, cwd=tmp, env=_child_env(), timeout=TIMEOUT_S)
            return parse(proc.stdout.decode(errors="replace"), proc.stderr.decode(errors="replace"), proc.returncode, tmp)
        except Exception:
            STATS["failures"] += 1
            raise


async def run_async(system: Optional[str], blocks: list[dict], model_hint: str, effort: Optional[str] = None) -> str:
    build, parse = _JOBS[ACTIVE]
    loop = asyncio.get_running_loop()
    sem = _async_sems.setdefault(id(loop), asyncio.Semaphore(CONCURRENCY))
    STATS["calls"] += 1
    with tempfile.TemporaryDirectory(dir=_WORKDIR) as d:
        tmp = Path(d)
        cmd, payload = build(system, blocks, model_hint, effort, tmp)
        try:
            async with sem:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=tmp, env=_child_env(),
                )
                try:
                    out, err = await asyncio.wait_for(proc.communicate(payload), timeout=TIMEOUT_S)
                except asyncio.TimeoutError:
                    proc.kill()
                    raise CLILLMError(f"{ACTIVE} CLI timed out after {TIMEOUT_S}s")
            return parse(out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode, tmp)
        except Exception:
            STATS["failures"] += 1
            raise


# ── login checks ──────────────────────────────────────────────────────────────


def login_status(backend: str) -> dict:
    """{'ok': bool, 'detail': str}"""
    try:
        if backend == "claude_code":
            if not Path(claude_bin()).exists():
                return {"ok": False, "detail": "claude CLI not found. Install Claude Code or set CLAUDE_BIN."}
            out = subprocess.run([claude_bin(), "auth", "status"], capture_output=True, env=_child_env(), timeout=30)
            info = json.loads(out.stdout or b"{}")
            if not info.get("loggedIn"):
                return {"ok": False, "detail": "claude CLI isn't logged in. Run `claude auth login`."}
            return {"ok": True, "detail": f"Claude Code login ({info.get('subscriptionType') or info.get('authMethod')})"}
        if backend == "chatgpt":
            binary = codex_bin()
            if not Path(binary).exists() and not shutil.which(binary):
                return {"ok": False, "detail": "codex CLI not found. Install the ChatGPT or Codex app, or the codex CLI."}
            out = subprocess.run([binary, "login", "status"], capture_output=True, text=True, env=_child_env(), timeout=30)
            text = (out.stdout + out.stderr).strip()
            if out.returncode != 0 or "ChatGPT" not in text:
                return {"ok": False, "detail": "codex isn't logged in with ChatGPT. Run `codex login`."}
            return {"ok": True, "detail": f"ChatGPT login via Codex ({chatgpt_model() or 'default model'})"}
    except Exception as exc:
        return {"ok": False, "detail": f"couldn't check {backend} login: {exc}"}
    return {"ok": False, "detail": f"unknown backend {backend}"}


# ── drop-in replacements ──────────────────────────────────────────────────────

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402


def _refuse_tools() -> None:
    STATS["tool_calls_refused"] += 1
    raise ToolUseUnsupported("custom tools are not supported through a CLI login")


def _split_langchain(messages: Iterable[BaseMessage]) -> tuple[Optional[str], list[tuple[str, Any]]]:
    system_parts, turns = [], []
    for m in messages:
        if isinstance(m, SystemMessage):
            system_parts.append(m.content if isinstance(m.content, str) else json.dumps(m.content))
        elif isinstance(m, ToolMessage):  # LangChain-style tool result
            result = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content, "is_error": m.status == "error"}
            turns.append(("user", [result]))
        elif isinstance(m, AIMessage) and m.tool_calls and not _has_tool_use(m.content):
            # LangChain keeps the calls beside the content; put them back in the turn.
            content = _to_blocks(m.content) + [
                {"type": "tool_use", "id": tc.get("id"), "name": tc["name"], "input": tc.get("args") or {}} for tc in m.tool_calls
            ]
            turns.append(("assistant", content))
        else:
            turns.append(("assistant" if m.type == "ai" else "user", m.content))
    return ("\n\n".join(system_parts) or None), turns


def _has_tool_use(content: Any) -> bool:
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)


def _chat_result(text: str, specs: Optional[list[dict]] = None) -> ChatResult:
    if not specs:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])
    clean, calls = _parse_tool_calls(text, specs)
    return ChatResult(generations=[ChatGeneration(message=AIMessage(content=clean, tool_calls=calls))])


class CLIChat(BaseChatModel):
    """Stands in for ChatAnthropic / ChatOpenAI. Ignores API-only options
    (api_key, max_tokens, thinking, temperature, timeout, …)."""

    model: str = "claude-opus-5"
    effort: Optional[str] = None

    def __init__(self, **kwargs: Any):
        model = kwargs.get("model") or kwargs.get("model_name") or "claude-opus-5"
        effort = kwargs.get("effort") or (kwargs.get("model_kwargs") or {}).get("effort")
        reasoning = kwargs.get("reasoning") or {}
        if not effort and isinstance(reasoning, dict):
            effort = reasoning.get("effort")
        super().__init__(model=str(model), effort=effort if effort in _EFFORTS else None)

    @property
    def _llm_type(self) -> str:
        return f"{ACTIVE}-cli"

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any):
        return self.bind(tools=_tool_specs(tools), tool_choice=tool_choice, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        system, turns = _split_langchain(messages)
        specs = _tool_specs(kwargs.get("tools"))
        if not specs:
            return _chat_result(run_sync(system, _flatten(turns), self.model, self.effort))
        STATS["tool_turns"] += 1
        system, choice = _tools_system(system, specs, kwargs.get("tool_choice")), kwargs.get("tool_choice")
        reply = run_sync(system, _flatten(turns), self.model, self.effort)
        error = _tool_reply_error(reply, choice)
        if error:  # one retry; a reply still unreadable after it is used for whatever calls parse
            reply = run_sync(system, _flatten(_retry_turns(turns, reply, error)), self.model, self.effort)
        return _chat_result(reply, specs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        system, turns = _split_langchain(messages)
        specs = _tool_specs(kwargs.get("tools"))
        if not specs:
            return _chat_result(await run_async(system, _flatten(turns), self.model, self.effort))
        STATS["tool_turns"] += 1
        system, choice = _tools_system(system, specs, kwargs.get("tool_choice")), kwargs.get("tool_choice")
        reply = await run_async(system, _flatten(turns), self.model, self.effort)
        error = _tool_reply_error(reply, choice)
        if error:  # one retry; a reply still unreadable after it is used for whatever calls parse
            reply = await run_async(system, _flatten(_retry_turns(turns, reply, error)), self.model, self.effort)
        return _chat_result(reply, specs)


def _anthropic_message(model: str, text: str):
    from anthropic.types import Message, TextBlock, Usage

    return Message.model_construct(
        id=f"msg_local_{uuid.uuid4().hex[:16]}", type="message", role="assistant", model=model,
        content=[TextBlock.model_construct(type="text", text=text, citations=None)],
        stop_reason="end_turn", stop_sequence=None, usage=Usage.model_construct(input_tokens=0, output_tokens=0),
    )


def _anthropic_payload(messages: list[dict], system: Any) -> tuple[Optional[str], list[dict]]:
    if isinstance(system, list):
        system = "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    return system, _flatten([(m.get("role", "user"), m.get("content")) for m in messages])


def _effort_from(kwargs: dict) -> Optional[str]:
    oc = kwargs.get("output_config") or {}
    return oc.get("effort") if isinstance(oc, dict) else None


class _AsyncMessages:
    async def create(self, *, model: str, messages: list[dict], system: Any = None, tools: Any = None, **kwargs: Any):
        if tools:
            _refuse_tools()
        sys_text, blocks = _anthropic_payload(messages, system)
        return _anthropic_message(model, await run_async(sys_text, blocks, model, _effort_from(kwargs)))


class _SyncMessages:
    def create(self, *, model: str, messages: list[dict], system: Any = None, tools: Any = None, **kwargs: Any):
        if tools:
            _refuse_tools()
        sys_text, blocks = _anthropic_payload(messages, system)
        return _anthropic_message(model, run_sync(sys_text, blocks, model, _effort_from(kwargs)))


class CLIAsyncAnthropic:
    def __init__(self, *a: Any, **k: Any):
        self.messages = _AsyncMessages()


class CLIAnthropic:
    def __init__(self, *a: Any, **k: Any):
        self.messages = _SyncMessages()


def install(backend: str, route_openai: bool) -> None:
    global ACTIVE
    if backend not in BACKENDS:
        raise ValueError(f"unknown CLI backend {backend!r}")
    ACTIVE = backend

    import anthropic
    import langchain_anthropic
    import langchain_anthropic.chat_models as _lca

    langchain_anthropic.ChatAnthropic = _lca.ChatAnthropic = CLIChat
    anthropic.AsyncAnthropic = CLIAsyncAnthropic
    anthropic.Anthropic = CLIAnthropic

    if route_openai:
        import langchain_openai
        import langchain_openai.chat_models as _lco

        langchain_openai.ChatOpenAI = _lco.ChatOpenAI = CLIChat
