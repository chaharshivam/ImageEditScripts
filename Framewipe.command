#!/bin/bash
# Double-click this file to start Framewipe.
# It opens your browser automatically. Close this Terminal window to stop.

cd "$(dirname "$0")" || exit 1

echo ""
echo "  Framewipe — prep frames locally. Nothing uploaded."
echo "  --------------------------------------------------"

PY=python3
command -v "$PY" >/dev/null 2>&1 || {
  echo "  ERROR: python3 not found."
  echo "  Install it with:  brew install python3"
  echo ""
  read -r -p "  Press Return to close." _
  exit 1
}

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

VENV=".venv"
VPY="$VENV/bin/python"

if [ ! -x "$VPY" ]; then
  echo "  First run: creating a local virtualenv in $VENV …"
  "$PY" -m venv "$VENV" || {
    echo ""
    echo "  ERROR: could not create $VENV."
    echo "  Try:  python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
    echo ""
    read -r -p "  Press Return to close." _
    exit 1
  }
  echo "  Installing dependencies into $VENV …"
  "$VPY" -m pip install --upgrade pip >/dev/null 2>&1 || true
  "$VPY" -m pip install -r requirements.txt || {
    echo ""
    echo "  ERROR: pip install failed. Try manually:"
    echo "      $VPY -m pip install -r requirements.txt"
    echo ""
    read -r -p "  Press Return to close." _
    exit 1
  }
fi

if ! "$VPY" -c "import flask, PIL, numpy" >/dev/null 2>&1; then
  echo "  Installing dependencies into $VENV …"
  "$VPY" -m pip install -r requirements.txt || {
    echo ""
    echo "  ERROR: pip install failed."
    echo ""
    read -r -p "  Press Return to close." _
    exit 1
  }
fi

command -v ffmpeg >/dev/null 2>&1 || {
  echo "  NOTE: ffmpeg not found — video features will be unavailable."
  echo "        Install with:  brew install ffmpeg"
}

echo ""
"$VPY" webapp.py
status=$?

echo ""
echo "  Server stopped."
read -r -p "  Press Return to close this window." _
exit $status
