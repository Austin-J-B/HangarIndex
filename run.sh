#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  system_python=python3
elif command -v python >/dev/null 2>&1; then
  system_python=python
else
  echo "Python 3.11 or newer is required. Install Python and try again." >&2
  exit 1
fi

if ! "$system_python" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  "$system_python" -m venv .venv
fi

if ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "Python 3.11 or newer is required by .venv. Remove .venv and run this script again." >&2
  exit 1
fi

.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/python app.py
