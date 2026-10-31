"""Make the email pipeline backend importable and runnable on one machine.

Import this module before anything under ``src.`` or ``config.``. It:

1. loads your API keys from ``local/.env``;
2. fills every other required setting with an inert placeholder;
3. swaps Postgres, S3 and the prompt-config store for in-process stand-ins;
4. lets the pipeline fetch files from the local server (its SSRF guard
   otherwise blocks loopback addresses);
5. turns on the Design Bible v2 feature flag so brand palettes/fonts apply.

Nothing here talks to the original production infrastructure.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import egress

LOCAL_DIR = Path(__file__).resolve().parent
ROOT_DIR = LOCAL_DIR.parent
BACKEND_DIR = ROOT_DIR / "backend"
STORAGE_DIR = LOCAL_DIR / "storage"
STORAGE_DIR.mkdir(exist_ok=True)

HOST = os.environ.get("EMAIL_LOCAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("EMAIL_LOCAL_PORT", "8765"))
BASE_URL = f"http://{HOST}:{PORT}"
FILES_URL = f"{BASE_URL}/files"

# Claude runs every step a finished email depends on (plan, copy, design, MJML
# repair, visual QA). OpenAI writes the subject-line options; Google is a backup
# model for a few steps and picks design-library images, which runs don't use.
REQUIRED_KEYS = ("ANTHROPIC_API_KEY",)
OPTIONAL_KEYS = ("OPENAI_API_KEY", "GOOGLE_API_KEY")
ALL_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS

# Settings the backend refuses to start without. None of them are used on the
# email path, so placeholders are enough.
_PLACEHOLDER_SETTINGS = {
    "ENV": "development",
    "AUTH0_DOMAIN": "local.invalid",
    "AUTH0_CLIENT_ID": "local",
    "AUTH0_CLIENT_SECRET": "local",
    "AWS_REGION": "us-east-1",
    "AZURE_OPENAI_API_KEY": "unset",
    "AZURE_OPENAI_ENDPOINT": "https://unset.invalid/",
    "AZURE_OPENAI_API_VERSION": "2024-05-01-preview",
    "AZURE_RESOURCE_NAME": "unset",
    "AZURE_OPENAI_MODEL": "unset",
    "AZURE_FORM_RECOGNIZER_ENDPOINT": "https://unset.invalid/",
    "AZURE_FORM_RECOGNIZER_KEY": "unset",
    "CEREBRAS_API_KEY": "unset",
    "PINECONE_API_KEY": "unset",
    "JINA_API_KEY": "unset",
    "EXA_API_KEY": "unset",
    "PERPLEXITY_AI_API_KEY": "unset",
    "SERP_API_KEY": "unset",
    "ILOVEPDF_PUBLIC_KEY": "unset",
    "LANGCHAIN_ENDPOINT": "https://unset.invalid",
    "LANGCHAIN_PROJECT": "local",
    "LANGCHAIN_TRACING_V2": "false",
    "CONVERTAPI_API_CREDENTIALS": "unset",
    "PLAYWRIGHT_NODE_URL": "http://127.0.0.1:1",
    "VIEW_FILE_EMAIL": "local@local.invalid",
    "FIGMA_ENCRYPTION_KEY": "unset",
    "TO_DESIGN_API_KEY": "unset",
    "DEEPIMAGE_API_KEY": "unset",
    "SLACK_BOT_TOKEN": "unset",
    "CUSTOM_SCRIPT_RUNNER_API_URL": "http://127.0.0.1:1",
    "DD_TRACE_ENABLED": "false",
}

# Pin these blank (or to a dead loopback port) whatever the environment says, so
# a stray variable can't point this app at a hosted service or someone's account.
# Empty MJML credentials make the renderer use the local mjml library instead
# of api.mjml.io.
_FORCED_SETTINGS = {
    "MJML_API_APP_ID": "",
    "MJML_API_SECRET_KEY": "",
    "REMOVE_BG_API_KEY": "",
    "QWEN_API_KEY": "",
    "BRAINTRUST_ORG": "",
    "PLAYWRIGHT_NODE_URL_1": "http://127.0.0.1:1",
    "PLAYWRIGHT_NODE_URL_2": "http://127.0.0.1:1",
    "PLAYWRIGHT_NODE_URL_3": "http://127.0.0.1:1",
    "BACKEND_URL": "http://127.0.0.1:1",
    "ADMIN_PORTAL_URL": "http://127.0.0.1:1",
    "TINYMCE_EXPORT_SERVICE_URL": "http://127.0.0.1:1",
    "SHARE_BASE_URL": "http://127.0.0.1:1",
}

# Python-level outbound traffic is limited to these hosts (plus loopback), and
# to the model APIs whose keys are set (added below, once local/.env is loaded).
# The headless browser used for visual QA is a separate process and can still
# load fonts/images referenced by the email itself.
ALLOWED_HOSTS = {
    "openaipublic.blob.core.windows.net",  # tiktoken encodings
    "localhost",
    "127.0.0.1",
    "::1",
} | {h.strip() for h in os.environ.get("EXTRA_ALLOWED_HOSTS", "").split(",") if h.strip()}
BLOCKED_HOSTS: dict[str, int] = {}


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        # Variables already set in the process win: app.py passes each run's
        # LLM_BACKEND this way, and shell exports should beat the file.
        if value and key not in os.environ:
            os.environ[key] = value


def _unset(key: str) -> bool:
    return os.environ.get(key, "unset") in ("", "unset")


def missing_keys() -> list[str]:
    if LLM_BACKEND != "api":
        return []
    return [k for k in REQUIRED_KEYS if _unset(k)]


def missing_optional_keys() -> list[str]:
    if LLM_BACKEND != "api":
        # OpenAI steps are routed to the CLI too; Gemini stays an optional backup.
        return [k for k in ("GOOGLE_API_KEY",) if _unset(k)]
    return [k for k in OPTIONAL_KEYS if _unset(k)]


_load_env_file(LOCAL_DIR / ".env")
# genai.Client() reads GOOGLE_API_KEY or GEMINI_API_KEY; accept either.
if os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]
for _k in ALL_KEYS:
    os.environ.setdefault(_k, "unset")
ALLOWED_HOSTS |= egress.model_api_hosts(os.environ)
for _k, _v in _PLACEHOLDER_SETTINGS.items():
    os.environ.setdefault(_k, _v)
os.environ.update(_FORCED_SETTINGS)
# Production installs the Node mjml CLI globally; here it lives in tools/.
os.environ["PATH"] = f"{ROOT_DIR / 'tools' / 'node_modules' / '.bin'}{os.pathsep}{os.environ.get('PATH', '')}"


def _install_egress_guard() -> None:
    import socket

    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else str(host or "")
        if name and name not in ALLOWED_HOSTS:
            BLOCKED_HOSTS[name] = BLOCKED_HOSTS.get(name, 0) + 1
            raise socket.gaierror(socket.EAI_NONAME, f"blocked by local egress guard: {name}")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = guarded_getaddrinfo


_install_egress_guard()

# config/settings.py calls load_dotenv(), which walks up from backend/ looking
# for a .env. An empty one in backend/ stops it from picking up unrelated files.
(BACKEND_DIR / ".env").touch(exist_ok=True)
os.chdir(BACKEND_DIR)
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_KEY_SNAPSHOT = {k: os.environ.get(k) for k in ALL_KEYS}

# How model calls are made:
#   api          → ANTHROPIC_API_KEY (and OpenAI/Gemini keys if set)
#   claude_code  → the local `claude` CLI on your Claude Code login, no API key
#   chatgpt      → the local `codex` CLI on your ChatGPT login, no API key
#   auto         → api when ANTHROPIC_API_KEY is set, otherwise claude_code
BACKEND_CHOICES = ("claude_code", "chatgpt", "api")
_backend = os.environ.get("LLM_BACKEND", "auto").strip().lower()
LLM_BACKEND = _backend if _backend in BACKEND_CHOICES else ("claude_code" if _unset("ANTHROPIC_API_KEY") else "api")
if str(LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(LOCAL_DIR))
if LLM_BACKEND != "api":
    import llm_backends

    llm_backends.install(LLM_BACKEND, route_openai=_unset("OPENAI_API_KEY"))

BACKEND_LABELS = {
    "claude_code": "Claude Code login",
    "chatgpt": "ChatGPT login (Codex)",
    "api": "API keys",
}


def _status_for(backend: str) -> dict:
    if backend == "api":
        ready = not _unset("ANTHROPIC_API_KEY")
        detail = "ANTHROPIC_API_KEY set" if ready else "set ANTHROPIC_API_KEY in local/.env to use this"
        return {"id": "api", "label": BACKEND_LABELS["api"], "ready": ready, "detail": detail}
    import llm_backends

    s = llm_backends.login_status(backend)
    return {"id": backend, "label": BACKEND_LABELS[backend], "ready": s["ok"], "detail": s["detail"]}


_status_cache: dict = {}


def all_backend_status(max_age_s: float = 60.0) -> list[dict]:
    """Readiness of every backend, for the UI. Login checks run the CLIs, so cache briefly."""
    import time

    if _status_cache.get("at", 0) > time.time() - max_age_s:
        return _status_cache["value"]
    value = [_status_for(b) for b in BACKEND_CHOICES]
    _status_cache.update(at=time.time(), value=value)
    return value


def backend_status() -> dict:
    """Status of the backend this process was started with."""
    s = _status_for(LLM_BACKEND)
    return {"backend": LLM_BACKEND, "ready": s["ready"], "detail": s["detail"]}


# ── Database: a session that answers every query with "nothing found" ─────────


class _NullResult:
    """Stands in for Query / Result / ScalarResult objects."""

    def __getattr__(self, _name: str):
        return lambda *a, **k: self

    def __iter__(self):
        return iter(())

    def __bool__(self) -> bool:
        return False

    def __len__(self) -> int:
        return 0

    def all(self, *a, **k):
        return []

    def fetchall(self, *a, **k):
        return []

    def first(self, *a, **k):
        return None

    def one_or_none(self, *a, **k):
        return None

    def fetchone(self, *a, **k):
        return None

    def scalar(self, *a, **k):
        return None

    def scalar_one_or_none(self, *a, **k):
        return None

    def count(self, *a, **k):
        return 0


class NullSession:
    def query(self, *a, **k):
        return _NullResult()

    def execute(self, *a, **k):
        return _NullResult()

    def scalars(self, *a, **k):
        return _NullResult()

    def scalar(self, *a, **k):
        return None

    def get(self, *a, **k):
        return None

    def __getattr__(self, _name: str):
        # add, commit, rollback, flush, refresh, close, merge, delete, expunge…
        return lambda *a, **k: None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _null_get_db():
    yield NullSession()


@contextmanager
def _null_scope(*_a, **_k):
    yield NullSession()


async def _null_run_db(fn, *, tenant_slug=None):
    return fn(NullSession())


def _patch_database() -> None:
    from src.shared.databases.sql_database import base

    base.get_db = _null_get_db
    base.SessionLocal = lambda *a, **k: NullSession()
    base.get_tenant_session = lambda *a, **k: NullSession()
    base.get_tenant_read_session = lambda *a, **k: NullSession()
    base.session_scope = _null_scope
    base.read_only_session_scope = _null_scope
    base.run_db = _null_run_db


# ── S3: files live in local/storage and are served by the local app ───────────


class LocalStorageAdapter:
    """Implements the StoragePort methods the email path uses."""

    def _path(self, key: str) -> Path:
        key = (key or "").lstrip("/")
        if key.startswith(FILES_URL + "/"):
            key = key[len(FILES_URL) + 1 :]
        path = (STORAGE_DIR / key).resolve()
        if STORAGE_DIR.resolve() not in path.parents and path != STORAGE_DIR.resolve():
            raise ValueError(f"storage key escapes storage dir: {key!r}")
        return path

    def url(self, key: str) -> str:
        if key.startswith(("http://", "https://")):
            return key
        return f"{FILES_URL}/{key.lstrip('/')}"

    def upload_file_object(self, file_key: str, file_obj, content_type: Optional[str] = None, *a, **k):
        path = self._path(file_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = file_obj.read() if hasattr(file_obj, "read") else bytes(file_obj)
        path.write_bytes(data)
        return file_key

    def reupload_file(self, temp_file_path: str, file_key: str, content_type: Optional[str] = None):
        with open(temp_file_path, "rb") as fh:
            return self.upload_file_object(file_key, fh, content_type)

    def get_download_url(self, file_key: str, expiration: int = 3600, *a, **k) -> str:
        return self.url(file_key)

    get_download_url_original = get_download_url
    get_download_url_for_content = get_download_url
    get_inline_url_for_content = get_download_url
    generate_presigned_download_url = get_download_url

    def generate_presigned_upload_url(self, file_key: str, *a, **k) -> dict:
        return {"url": self.url(file_key), "fields": {}}

    def delete_file(self, file_key: str) -> None:
        self._path(file_key).unlink(missing_ok=True)

    def list_files(self, prefix: Optional[str] = None):
        root = self._path(prefix or "")
        if not root.exists():
            return []
        return [str(p.relative_to(STORAGE_DIR)) for p in root.rglob("*") if p.is_file()]

    def get_file_info(self, file_key: str):
        path = self._path(file_key)
        return {
            "key": file_key,
            "size": path.stat().st_size if path.exists() else 0,
            "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        }


def _patch_storage() -> None:
    from src.file_management.application.services.s3_storage_service import S3StorageService
    from src.shared.dependencies import s3 as s3_dep

    service = S3StorageService(LocalStorageAdapter())
    s3_dep.get_s3_service = lambda: service

    from src.content_generation_new.utils import helper

    helper.get_s3_service = lambda: service

    def _download(key: str) -> io.BytesIO:
        return io.BytesIO(LocalStorageAdapter()._path(key).read_bytes())

    helper.download_file_from_s3 = _download


# ── URL guard: allow the local file server, keep blocking everything else ─────


def _patch_url_guard() -> None:
    from src.shared.utils import url_security

    original = url_security.validate_fetch_url

    def validate_fetch_url(url: str) -> None:
        if isinstance(url, str) and url.startswith(FILES_URL + "/"):
            return
        original(url)

    url_security.validate_fetch_url = validate_fetch_url


# ── Prompt config: built-in prompts, optional per-step model overrides ────────


def _patch_prompt_config() -> None:
    from src.pipeline_config.application import prompt_config_singleton as pcs
    from src.pipeline_config.application.prompt_config_service import PromptConfigService
    from src.pipeline_config.domain.entities.prompt_config import ResolvedStepConfig

    overrides_path = LOCAL_DIR / "model_overrides.json"
    overrides: dict[str, str] = {}
    if overrides_path.exists():
        overrides = {k: v for k, v in json.loads(overrides_path.read_text()).items() if not k.startswith("_")}

    class LocalPromptConfigService(PromptConfigService):
        def resolve(self, pipeline, step_name, brand_id=None, prompt_key="main"):
            model = overrides.get(f"{pipeline}:{step_name}:{prompt_key}") or overrides.get(f"{pipeline}:{step_name}")
            if not model:
                return super().resolve(pipeline, step_name, brand_id=brand_id, prompt_key=prompt_key)
            return ResolvedStepConfig(
                pipeline=pipeline,
                step_name=step_name,
                prompt_key=prompt_key,
                prompt_template="",
                model_name=model,
            )

    pcs._service_instance = LocalPromptConfigService(repository=pcs._NoOpRepository())


def _patch_feature_flags() -> None:
    from src.content_generation.application.other.design_bible import runtime_context

    runtime_context.design_bible_v2_flag_enabled = lambda: True


# Order matters: later patches import modules that bind get_db at import time.
_patch_database()
_patch_url_guard()
_patch_feature_flags()
_patch_prompt_config()
_patch_storage()

# The backend's prompts cast the model as a pharma writer; this app makes consumer-brand
# emails, so swap those persona lines on every model call (CONSUMER_FRAMING=0 turns it off).
import consumer_framing  # noqa: E402

consumer_framing.install()
if LLM_BACKEND != "api":
    consumer_framing.install_cli(llm_backends)

# Speed mode for this process (SPEED_MODE=best|balanced|fast, set per run by app.py).
import model_routing  # noqa: E402

model_routing.install(llm_backends if LLM_BACKEND != "api" else None)


def assert_keys_untouched() -> None:
    """Fail loudly if any imported module overwrote the user's API keys."""
    changed = [k for k, v in _KEY_SNAPSHOT.items() if os.environ.get(k) != v]
    if changed:
        raise RuntimeError(f"An imported module overwrote these API keys: {changed}. Refusing to run.")
