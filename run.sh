#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  printf '%s\n' 'Buat environment terlebih dahulu:' '  python3 -m venv .venv' '  .venv/bin/python -m pip install -r requirements.txt' '  .venv/bin/python -m playwright install chromium'
  exit 1
fi
exec .venv/bin/python app.py "$@"
