#!/bin/bash
# Local checks. Never pip-installs into system Python.
set -euo pipefail
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=/usr/bin/python3
fi
echo "== py_compile =="
"$PY" -m py_compile split_grids.py webapp.py
echo "py_compile ok"
echo "== pytest =="
if "$PY" -c "import pytest"; then
  "$PY" -m pytest -q
else
  echo "pytest not in this interpreter. Use a venv:"
  echo "  python3 -m venv .venv && .venv/bin/python -m pip install -r requirements-dev.txt"
  echo "  .venv/bin/python -m pytest -q"
fi
echo "== flask client =="
"$PY" -c "import webapp
c = webapp.app.test_client()
html = c.get('/').data.decode('utf-8', 'replace')
assert 'Framewipe' in html
assert 'Cleanroom' not in html
assert c.get('/api/capabilities').get_json()['app'] == 'Framewipe'
print('flask client ok')"
echo ALL_OK
