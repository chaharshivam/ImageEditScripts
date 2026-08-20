#!/bin/bash
# Double-click this file to start Cleanroom.
# It opens your browser automatically. Close this Terminal window to stop.

cd "$(dirname "$0")" || exit 1

echo ""
echo "  Cleanroom — prep AI media for Instagram"
echo "  --------------------------------------------------"

PY=python3
command -v "$PY" >/dev/null 2>&1 || {
  echo "  ERROR: python3 not found."
  echo "  Install it with:  brew install python3"
  echo ""
  read -r -p "  Press Return to close." _
  exit 1
}

# Flask is the only extra dependency; install on first run.
if ! "$PY" -c "import flask" >/dev/null 2>&1; then
  echo "  First run: installing Flask (one time, ~1 MB)…"
  "$PY" -m pip install --user --quiet flask || {
    echo ""
    echo "  ERROR: could not install Flask. Try manually:"
    echo "      python3 -m pip install --user flask"
    echo ""
    read -r -p "  Press Return to close." _
    exit 1
  }
fi

if ! "$PY" -c "import PIL, numpy" >/dev/null 2>&1; then
  echo "  Installing Pillow and numpy…"
  "$PY" -m pip install --user --quiet -r requirements.txt
fi

command -v ffmpeg >/dev/null 2>&1 || {
  echo "  NOTE: ffmpeg not found — video features will be unavailable."
  echo "        Install with:  brew install ffmpeg"
}

echo ""
"$PY" webapp.py

echo ""
echo "  Server stopped."
read -r -p "  Press Return to close this window." _
