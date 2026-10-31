#!/usr/bin/env bash
# One-time setup: Python env, headless Chromium (visual QA), mjml CLI, local/.env.
set -euo pipefail
cd "$(dirname "$0")"

echo "→ Python 3.12 environment (.venv)"
if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -r local/requirements.txt
else
  PY=$(command -v python3.12 || true)
  if [ -z "$PY" ]; then
    echo "Python 3.12 not found. Install it (or uv: https://docs.astral.sh/uv/) and re-run." >&2
    exit 1
  fi
  "$PY" -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r local/requirements.txt
fi

echo "→ Headless Chromium for the visual QA step"
.venv/bin/python -m playwright install chromium

echo "→ mjml CLI (MJML → HTML)"
if ! command -v npm >/dev/null 2>&1; then
  echo "npm not found. Install Node.js 18+ and re-run." >&2
  exit 1
fi
(cd tools && npm ci --no-fund --no-audit)

if [ ! -f local/.env ]; then
  cp local/.env.example local/.env
  chmod 600 local/.env
  echo "→ Created local/.env"
fi

cat <<'EOF'

Done. Start the app with:

    ./run.sh

then open http://127.0.0.1:8765

Model access (pick per run in the form):
  • Claude Code login  needs `claude` installed and logged in   (claude auth status)
  • ChatGPT login      needs the ChatGPT/Codex app or codex CLI   (codex login status)
  • API keys           set ANTHROPIC_API_KEY in local/.env
EOF
