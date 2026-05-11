"""
👤 HUMAN-WALKTHROUGH-AGENT — User-Journey-Tester
Simuliert echte User-Pfade von Anfang bis Ende.
Run: python3 scripts/journey_test.py [base_url]
"""
import sys
import os
import requests
from urllib.parse import urljoin

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:5050'
ADMIN_EMAIL = os.environ.get('QA_USER', '')
ADMIN_PASS = os.environ.get('QA_PASS', '')

results = []


def step(journey, n, name, ok, detail=''):
    sym = '✅' if ok else '❌'
    results.append((journey, n, name, ok, detail))
    print(f'  {sym} {n}. {name}{(" — " + detail) if detail else ""}')


def login_as(email, password):
    s = requests.Session()
    r = s.post(urljoin(BASE, '/login'), data={'email': email, 'password': password},
               allow_redirects=False, timeout=30)
    return s if r.status_code in (302, 303) else None


print(f'=== HUMAN-WALKTHROUGH — {BASE} ===\n')

# ============================================
# JOURNEY 1: Public-Pages für Nicht-Eingeloggte
# ============================================
print('🚪 JOURNEY 1: Anonymous User (öffentliche Seiten)')
public_paths = [('/', 'Home redirect'), ('/login', 'Login-Page'),
                ('/registrieren', 'Self-Registration'), ('/datenschutz', 'Datenschutz'),
                ('/start', 'Public Lead-Capture')]
for i, (p, name) in enumerate(public_paths, 1):
    try:
        r = requests.get(urljoin(BASE, p), allow_redirects=False, timeout=30)
        ok = r.status_code in (200, 302, 303)
        step('anonymous', i, f'{name} ({p})', ok, f'HTTP {r.status_code}' if not ok else '')
    except Exception as e:
        step('anonymous', i, f'{name} ({p})', False, str(e)[:50])

# ============================================
# JOURNEY 2: Admin-Login + Hauptpfade
# ============================================
print('\n👨‍💼 JOURNEY 2: Admin-Workflow')
if not (ADMIN_EMAIL and ADMIN_PASS):
    print('  ⏭ SKIP: setze QA_USER + QA_PASS env vars')
else:
    s = login_as(ADMIN_EMAIL, ADMIN_PASS)
    if not s:
        step('admin', 0, 'Login fehlgeschlagen', False, 'Credentials prüfen')
    else:
        step('admin', 0, 'Login OK', True)
        admin_paths = [
            ('/dashboard', 'Dashboard lädt'),
            ('/team', 'Team-Liste'),
            ('/namensliste', 'Namensliste'),
            ('/vertraege', 'Verträge-Liste'),
            ('/termine', 'Termine'),
            ('/struktur', 'Struktur-Baum'),
            ('/news', 'Struktur-News'),
            ('/tracking', 'Tracking & Quoten'),
            ('/team-kalender', 'Team-Kalender'),
            ('/team/inaktiv', 'Inaktiv-Liste'),
            ('/coach', 'KI-Coach'),
            ('/assistent', 'NTcoach Chat'),
            ('/profil', 'Profil'),
            ('/push-settings', 'Push-Einstellungen'),
            ('/admin/genehmigungen', 'Stufen-Genehmigungen'),
            ('/admin/vorschlaege', 'Partner-Vorschläge'),
            ('/admin/push', 'Admin-Push'),
            ('/admin/aktivitaet', 'Aktivitäts-Log'),
            ('/admin/inbox', 'Bewerber-Inbox'),
        ]
        for i, (p, name) in enumerate(admin_paths, 1):
            try:
                r = s.get(urljoin(BASE, p), allow_redirects=False, timeout=30)
                ok = r.status_code == 200
                step('admin', i, f'{name} ({p})', ok, f'HTTP {r.status_code}' if not ok else '')
            except Exception as e:
                step('admin', i, f'{name} ({p})', False, str(e)[:50])

# ============================================
# JOURNEY 3: API-Endpoints
# ============================================
print('\n🔌 JOURNEY 3: API-Endpoints')
api_paths = [('/api/health', 'Health Check'),
             ('/api/push/vapid-key', 'Push-Key (public)'),
             ('/manifest.json', 'PWA Manifest'),
             ('/sw.js', 'Service Worker'),
             ('/favicon.svg', 'Favicon')]
for i, (p, name) in enumerate(api_paths, 1):
    try:
        r = requests.get(urljoin(BASE, p), timeout=30)
        ok = r.status_code == 200
        step('api', i, f'{name} ({p})', ok, f'HTTP {r.status_code}' if not ok else '')
    except Exception as e:
        step('api', i, f'{name} ({p})', False, str(e)[:50])

# ============================================
# REPORT
# ============================================
print(f'\n=== JOURNEY-REPORT ===')
total = len(results)
passed = sum(1 for r in results if r[3])
failed = total - passed
print(f'Total Schritte: {total}')
print(f'✅ Erfolgreich:  {passed}')
print(f'❌ Fehler:       {failed}')

if failed:
    print(f'\n❌ FEHLER-DETAILS:')
    for j, n, name, ok, detail in results:
        if not ok:
            print(f'  [{j}] {name}: {detail}')

sys.exit(0 if not failed else 1)
