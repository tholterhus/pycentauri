#!/usr/bin/env bash
# Pre-deploy gate: everything that burned us this session, checked in one
# place. Run BEFORE every deploy; deploy only on green.
set -u
cd /opt/pycentauri
FAIL=0

step() { printf "\n== %s ==\n" "$1"; }

step "ruff check"
venv/bin/ruff check --cache-dir /tmp/ruff-cache . || FAIL=1

step "ruff format"
venv/bin/ruff format --cache-dir /tmp/ruff-cache --check . || FAIL=1

step "mypy (strict)"
venv/bin/mypy --cache-dir /tmp/mypy-cache src || FAIL=1

step "pytest"
venv/bin/python -m pytest tests/ -q -p no:cacheprovider || FAIL=1

step "dead-reference audit (JS ids vs markup)"
python3 - <<'PY'
import re, sys
js = open("src/pycentauri/web/app.js", encoding="utf-8").read()
html = open("src/pycentauri/web/index.html", encoding="utf-8").read()
ids = set(re.findall(r'\$\("([a-z0-9-]+)"\)', js)) | set(re.findall(r'getElementById\("([a-z0-9-]+)"\)', js))
html_ids = set(re.findall(r'id="([a-z0-9-]+)"', html))
dead = sorted(i for i in ids if i not in html_ids)
if dead:
    print("DEAD references:", dead)
    sys.exit(1)
print("ok —", len(ids), "ids checked")
PY
[ $? -ne 0 ] && FAIL=1

step "app.js syntax"
node --check src/pycentauri/web/app.js || FAIL=1

printf "\n====================\n"
if [ "$FAIL" -eq 0 ]; then echo "PREFLIGHT GREEN — deploy ok"; else echo "PREFLIGHT RED — fix before deploying"; fi
exit $FAIL
