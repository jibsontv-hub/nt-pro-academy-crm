"""
🛡 QA-AGENT — Automatischer Route-Audit
Geht durch alle Flask-Routes der App + dokumentiert Status.
Run: python3 scripts/qa_audit.py [base_url]
"""
import sys
import re
import os
import requests
from urllib.parse import urljoin

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:5050'

# Routes aus app.py extrahieren
APP_PY = os.path.join(os.path.dirname(__file__), '..', 'app.py')
ROUTES = []
with open(APP_PY) as f:
    src = f.read()
for m in re.finditer(r"@app\.route\('([^']+)'(?:,\s*methods\s*=\s*\[([^\]]+)\])?\)", src):
    path = m.group(1)
    methods = m.group(2) or "'GET'"
    has_get = 'GET' in methods
    has_post = 'POST' in methods
    if has_get:
        # Skip: Routen mit dynamischen IDs übersprungen oder mit Test-ID 1 ersetzen
        path_test = re.sub(r'<int:\w+>', '1', path)
        path_test = re.sub(r'<\w+>', 'test', path_test)
        ROUTES.append((path_test, 'GET', path))

# Optional: Login als Admin (cookie-basiert)
session = requests.Session()
LOGIN_USER = os.environ.get('QA_USER', '')
LOGIN_PASS = os.environ.get('QA_PASS', '')

logged_in = False
if LOGIN_USER and LOGIN_PASS:
    try:
        r = session.post(urljoin(BASE_URL, '/login'),
                         data={'email': LOGIN_USER, 'password': LOGIN_PASS},
                         allow_redirects=False, timeout=10)
        if r.status_code in (302, 303):
            logged_in = True
            print(f'✓ Logged in as {LOGIN_USER}')
        else:
            print(f'⚠ Login failed: {r.status_code}')
    except Exception as e:
        print(f'⚠ Login exception: {e}')

# Stats
ok, warn, fail, skip = [], [], [], []
print(f'\n=== QA-AUDIT — {BASE_URL} — {len(ROUTES)} Routes ===\n')

for path, method, original in ROUTES:
    # Skip einige Routes die Setup brauchen
    if any(x in path for x in ['/static/', 'photo/delete', '/delete', '/deactivate']):
        skip.append(original)
        continue
    try:
        r = session.get(urljoin(BASE_URL, path), allow_redirects=False, timeout=15)
        code = r.status_code
        if code in (200, 304):
            ok.append((original, code))
            sym = '✓'
        elif code in (301, 302, 303, 307, 308):
            ok.append((original, code))
            sym = '→'
        elif code in (401, 403, 404):
            warn.append((original, code))
            sym = '⚠'
        elif code >= 500:
            fail.append((original, code))
            sym = '🔴'
        else:
            warn.append((original, code))
            sym = '?'
        print(f'{sym} [{code}] {original}')
    except Exception as e:
        fail.append((original, str(e)[:40]))
        print(f'💥 [ERR] {original}: {e}')

# Report
print(f'\n=== ZUSAMMENFASSUNG ===')
print(f'✓ OK:    {len(ok)}')
print(f'⚠ WARN:  {len(warn)} (Permission/404 — wahrscheinlich auth-protected wenn nicht eingeloggt)')
print(f'🔴 FAIL:  {len(fail)} (Crashes!)')
print(f'⏭ SKIP:  {len(skip)} (Test übersprungen)')

if fail:
    print(f'\n🔴 FEHLER (sofort fixen):')
    for path, code in fail:
        print(f'   - {path} → {code}')

if warn and not logged_in:
    print(f'\n⚠ Hinweis: viele WARN sind 302→Login (User nicht eingeloggt). Setze QA_USER + QA_PASS für vollen Test.')

sys.exit(0 if not fail else 1)
