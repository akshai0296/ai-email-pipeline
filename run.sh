#!/usr/bin/env bash
# Start the local web UI at http://127.0.0.1:8765
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Run ./setup.sh first." >&2
  exit 1
fi
exec .venv/bin/python local/app.py
