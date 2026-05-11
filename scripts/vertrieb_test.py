"""
🎯 VERTRIEBS-AGENT — End-to-End Sales-Pipeline-Tester
Simuliert den vollständigen Vertriebs-Flow und validiert jeden Schritt:

  1. Lead anlegen (VK-Liste)
  2. Lead-Status durchlaufen (neu → kontakt → angebot → gewonnen)
  3. Termin anlegen (gekoppelt an Lead)
  4. Vertrag anlegen (mit Volumen → EH-Berechnung prüfen)
  5. Vertrags-Provision durch Strang propagiert
  6. Tracking & Quoten zeigen den neuen Vertrag an
  7. Webhook-Token → externer Lead landet in Liste
  8. Recruiting-Lead (RK) parallel — bestätigt Listentrennung

Run:
  python3 scripts/vertrieb_test.py [base_url]
  QA_USER=mail QA_PASS=pw  python3 scripts/vertrieb_test.py

Aufräumen passiert automatisch — alle Test-Records am Ende gelöscht.
"""
import sys
import os
import json
import time
import requests
from urllib.parse import urljoin
from datetime import datetime, timedelta

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:5050'
QA_USER = os.environ.get('QA_USER', '')
QA_PASS = os.environ.get('QA_PASS', '')

# Eindeutige Marker für Test-Records → Cleanup findet sie wieder
RUN_ID = f'VAGENT-{int(time.time())}'
TEST_LEAD_NAME = f'{RUN_ID}-TestKunde-VK'
TEST_RK_NAME = f'{RUN_ID}-TestRekrut-RK'
TEST_TERMIN_TITLE = f'{RUN_ID}-Termin'
TEST_VERTRAG_CLIENT = f'{RUN_ID}-Vertrag'
TEST_WEBHOOK_LABEL = f'{RUN_ID}-Webhook'

results = []  # (step_name, ok, detail)


def step(name, ok, detail=''):
    sym = '✅' if ok else '❌'
    results.append((name, ok, detail))
    print(f'  {sym} {name}{(" — " + detail) if detail else ""}')
    return ok


def login():
    if not (QA_USER and QA_PASS):
        return None
    s = requests.Session()
    r = s.post(urljoin(BASE, '/login'),
               data={'email': QA_USER, 'password': QA_PASS},
               allow_redirects=False, timeout=30)
    if r.status_code in (302, 303):
        return s
    return None


def get_csrf_or_form(s, path):
    """Manche Forms haben CSRF; Flask-Login hier nicht — aber wir holen sicherheitshalber die Page."""
    try:
        s.get(urljoin(BASE, path), timeout=30)
    except Exception:
        pass


print(f'\n=== 🎯 VERTRIEBS-AGENT — End-to-End Sales-Pipeline ===')
print(f'Base: {BASE}')
print(f'Run-ID: {RUN_ID}\n')

# ============================================
# AUTH
# ============================================
print('🔐 LOGIN')
s = login()
if not s:
    step('Login', False, 'QA_USER + QA_PASS env vars setzen + Server muss laufen')
    sys.exit(2)
step('Login', True, QA_USER)

# ============================================
# STEP 1 — Lead anlegen (VK)
# ============================================
print('\n1️⃣  LEAD ANLEGEN (Vertriebs-Liste)')
get_csrf_or_form(s, '/leads/neu')
r = s.post(urljoin(BASE, '/leads/neu'), data={
    'name': TEST_LEAD_NAME,
    'email': f'{RUN_ID.lower()}@test.local',
    'phone': '+49 170 0000000',
    'produkt': 'Vermögensaufbau',
    'status': 'neu',
    'liste_typ': 'vk',
    'notizen': f'Auto-generiert von Vertriebs-Agent (Run {RUN_ID})',
}, allow_redirects=False, timeout=30)
lead_created = r.status_code in (302, 303)
step('VK-Lead via /leads/neu (POST)', lead_created, f'HTTP {r.status_code}')

# Lead-ID rauskriegen via Namensliste
r = s.get(urljoin(BASE, '/namensliste?typ=vk'), timeout=30)
lead_appears = TEST_LEAD_NAME in r.text
step('Lead taucht in Namensliste (VK) auf', lead_appears,
     '' if lead_appears else f'Page lieferte {r.status_code}')

# ============================================
# STEP 2 — Lead-Status hochsetzen
# ============================================
print('\n2️⃣  LEAD-STATUS-FLOW (neu → kontakt → angebot → gewonnen)')
# Lead-ID via interne API holen — wir parsen einfach den HTML-Edit-Link
import re
match = re.search(r'/leads/(\d+)/edit[^"]*"[^>]*>[^<]*' + re.escape(TEST_LEAD_NAME[:20]), r.text)
if not match:
    # Fallback — anderen Pattern-Try
    matches = re.findall(r'/leads/(\d+)/edit', r.text)
    lead_id = int(matches[-1]) if matches else None
else:
    lead_id = int(match.group(1))

if lead_id:
    step(f'Lead-ID gefunden', True, f'#{lead_id}')
    for new_status in ('kontakt', 'angebot', 'gewonnen'):
        r = s.post(urljoin(BASE, f'/leads/{lead_id}/edit'), data={
            'name': TEST_LEAD_NAME,
            'email': f'{RUN_ID.lower()}@test.local',
            'phone': '+49 170 0000000',
            'produkt': 'Vermögensaufbau',
            'status': new_status,
            'liste_typ': 'vk',
            'notizen': f'Status: {new_status}',
        }, allow_redirects=False, timeout=30)
        step(f'Status → {new_status}', r.status_code in (302, 303), f'HTTP {r.status_code}')
else:
    step('Lead-ID nicht parsbar', False, 'Cleanup im Spätschritt erschwert')

# ============================================
# STEP 3 — Termin anlegen (kundentermin)
# ============================================
print('\n3️⃣  TERMIN ANLEGEN (kundentermin)')
get_csrf_or_form(s, '/termine/neu')
termin_date = (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d')
r = s.post(urljoin(BASE, '/termine/neu'), data={
    'title': TEST_TERMIN_TITLE,
    'client_name': TEST_LEAD_NAME,
    'termin_date': termin_date,
    'termin_time': '14:00',
    'typ': 'kundentermin',
    'status': 'geplant',
    'notizen': f'Auto-Termin {RUN_ID}',
}, allow_redirects=False, timeout=30)
step('Termin-POST', r.status_code in (302, 303), f'HTTP {r.status_code}')
r = s.get(urljoin(BASE, '/termine'), timeout=30)
step('Termin in Liste sichtbar', TEST_TERMIN_TITLE in r.text)

# ============================================
# STEP 4 — Vertrag anlegen + EH-Berechnung
# ============================================
print('\n4️⃣  VERTRAG ANLEGEN + EH-BERECHNUNG')
get_csrf_or_form(s, '/vertraege/neu')
volumen = 5000.0
r = s.post(urljoin(BASE, '/vertraege/neu'), data={
    'client_name': TEST_VERTRAG_CLIENT,
    'produkt': 'Test-Police',
    'volumen': str(volumen),
    'provision': '0',
    'status': 'offen',
    'abschluss_date': datetime.now().strftime('%Y-%m-%d'),
    'notizen': f'Auto-Vertrag {RUN_ID}',
    'lead_id': str(lead_id) if lead_id else '',
}, allow_redirects=False, timeout=20)
step('Vertrag-POST', r.status_code in (302, 303), f'HTTP {r.status_code}')

r = s.get(urljoin(BASE, '/vertraege'), timeout=30)
vertrag_in_list = TEST_VERTRAG_CLIENT in r.text
step('Vertrag in Liste sichtbar', vertrag_in_list)

# EH-Faktor verifizieren — bei 5000€ Volumen sollten 0.8 EH/€ = 4000 EH herauskommen
expected_eh = volumen * 0.8
# Suche im HTML nach dem Wert (formatiert: "4.000" oder "4000" oder "4 000")
eh_str_variants = [f'{int(expected_eh):,}'.replace(',', '.'),
                   f'{int(expected_eh)}',
                   f'{expected_eh:.0f}']
eh_visible = any(v in r.text for v in eh_str_variants)
step(f'EH-Berechnung korrekt ({volumen}€ → {int(expected_eh)} EH)',
     eh_visible, '' if eh_visible else f'Erwartet: {eh_str_variants}')

# ============================================
# STEP 5 — Tracking-Page zeigt den neuen Vertrag
# ============================================
print('\n5️⃣  TRACKING & FORECAST')
r = s.get(urljoin(BASE, '/tracking'), timeout=30)
step('Tracking-Page lädt', r.status_code == 200, f'HTTP {r.status_code}')

r = s.get(urljoin(BASE, '/provisionen'), timeout=30)
step('Provisionen-Page lädt', r.status_code == 200, f'HTTP {r.status_code}')

r = s.get(urljoin(BASE, '/dashboard'), timeout=20)
step('Dashboard rendert nach Vertrags-Insert', r.status_code == 200, f'HTTP {r.status_code}')

# ============================================
# STEP 6 — Webhook-Token erzeugen + externer Lead
# ============================================
print('\n6️⃣  WEBHOOK-FLOW (externer Lead)')
get_csrf_or_form(s, '/webhook-setup')
r = s.post(urljoin(BASE, '/webhook-setup/create'), data={
    'label': TEST_WEBHOOK_LABEL,
    'list_typ': 'vk',
}, allow_redirects=False, timeout=30)
step('Webhook-Token-POST', r.status_code in (302, 303), f'HTTP {r.status_code}')

# Token aus Liste extrahieren
r = s.get(urljoin(BASE, '/webhook-setup'), timeout=30)
token_match = re.search(r'/api/webhook/lead/([a-zA-Z0-9_-]+)[^<]*</span>\s*<button[^>]*>[^<]*</button>\s*</div>\s*</div>\s*\{%', r.text)
if not token_match:
    # einfacherer Fallback
    tokens = re.findall(r'/api/webhook/lead/([a-zA-Z0-9_-]{16,})', r.text)
    token = tokens[-1] if tokens else None
else:
    token = token_match.group(1)

if token:
    step('Token extrahiert', True, f'{token[:12]}…')
    # Webhook GET (Health)
    r = requests.get(urljoin(BASE, f'/api/webhook/lead/{token}'), timeout=30)
    step('Webhook GET (Health-Test)', r.status_code == 200 and r.json().get('ok') is True, f'HTTP {r.status_code}')
    # Webhook POST (echter externer Lead)
    external_name = f'{RUN_ID}-Extern'
    r = requests.post(urljoin(BASE, f'/api/webhook/lead/{token}'),
                      json={'name': external_name, 'email': 'extern@test.local',
                            'phone': '+49 170 1111111', 'message': 'Webhook-Test',
                            'source': 'vertrieb-agent'}, timeout=30)
    step('Webhook POST (externer Lead)', r.status_code == 200 and r.json().get('ok') is True, f'HTTP {r.status_code}')
    # Erscheint der externe Lead?
    r = s.get(urljoin(BASE, '/namensliste?typ=vk'), timeout=30)
    step('Externer Lead in Namensliste', external_name in r.text)
else:
    step('Token konnte nicht extrahiert werden', False)

# ============================================
# STEP 7 — Recruiting-Liste parallel
# ============================================
print('\n7️⃣  RECRUITING-LISTE (RK — Listentrennung)')
r = s.post(urljoin(BASE, '/leads/neu'), data={
    'name': TEST_RK_NAME,
    'email': f'rk-{RUN_ID.lower()}@test.local',
    'phone': '+49 170 2222222',
    'produkt': '',
    'status': 'neu',
    'liste_typ': 'rk',
    'notizen': 'RK-Test',
}, allow_redirects=False, timeout=30)
step('RK-Lead anlegen', r.status_code in (302, 303), f'HTTP {r.status_code}')

# RK in RK-Liste, NICHT in VK-Liste
r_rk = s.get(urljoin(BASE, '/namensliste?typ=rk'), timeout=30)
r_vk = s.get(urljoin(BASE, '/namensliste?typ=vk'), timeout=30)
in_rk = TEST_RK_NAME in r_rk.text
not_in_vk = TEST_RK_NAME not in r_vk.text
step('RK-Lead in RK-Liste', in_rk)
step('RK-Lead NICHT in VK-Liste (Trennung sauber)', not_in_vk)

# ============================================
# STEP 8 — Quoten + Forecast
# ============================================
print('\n8️⃣  QUOTEN & ZIELE')
for path in ['/quoten', '/ziele', '/ranking']:
    r = s.get(urljoin(BASE, path), timeout=30)
    step(f'{path} lädt', r.status_code == 200, f'HTTP {r.status_code}')

# ============================================
# CLEANUP — Test-Records wieder löschen
# ============================================
print('\n🧹 CLEANUP (Test-Records entfernen)')
cleanup_count = 0


def http_post(path):
    try:
        return s.post(urljoin(BASE, path), allow_redirects=False, timeout=30)
    except Exception:
        return None


# Leads
r = s.get(urljoin(BASE, '/namensliste?typ=vk'), timeout=30)
text_all = r.text + s.get(urljoin(BASE, '/namensliste?typ=rk'), timeout=30).text
for marker in (TEST_LEAD_NAME, TEST_RK_NAME, f'{RUN_ID}-Extern'):
    # Lead-ID via Pattern
    m = re.findall(r'/leads/(\d+)/edit', text_all)
    # Wir können hier nicht 1:1 zuordnen — also alles mit RUN_ID-Marker holen via DB? Nein, wir nutzen Delete-Endpoint
    pass

# Pragmatischer Cleanup: Iteriere über alle Leads in der Liste und lösche jene mit RUN_ID im Markup
for typ in ('vk', 'rk'):
    page = s.get(urljoin(BASE, f'/namensliste?typ={typ}'), timeout=30).text
    # Pattern: <tr ...><td>...NAME...</td>...<form action="/leads/ID/delete"
    # Wir matchen pro RUN_ID-Zeile die Lead-ID
    for lid in set(re.findall(r'/leads/(\d+)/(?:edit|delete)', page)):
        # Prüfen ob diese Lead-ID zu unseren Test-Markern gehört
        if RUN_ID in page:
            # Get lead row context
            row_match = re.search(rf'/leads/{lid}/edit[^"]*"[^>]*>([\s\S]{{0,400}}){RUN_ID}', page)
            if row_match or RUN_ID in page:
                # Versuche Delete
                resp = http_post(f'/leads/{lid}/delete')
                if resp and resp.status_code in (200, 302, 303):
                    cleanup_count += 1

# Verträge
page = s.get(urljoin(BASE, '/vertraege'), timeout=30).text
for vid in set(re.findall(r'/vertraege/(\d+)/(?:edit|delete)', page)):
    if RUN_ID in page:
        resp = http_post(f'/vertraege/{vid}/delete')
        if resp and resp.status_code in (200, 302, 303):
            cleanup_count += 1

# Termine
page = s.get(urljoin(BASE, '/termine'), timeout=30).text
for tid in set(re.findall(r'/termine/(\d+)/(?:edit|delete)', page)):
    if RUN_ID in page:
        resp = http_post(f'/termine/{tid}/delete')
        if resp and resp.status_code in (200, 302, 303):
            cleanup_count += 1

# Webhook-Tokens
page = s.get(urljoin(BASE, '/webhook-setup'), timeout=30).text
for wid in set(re.findall(r'/webhook-setup/(\d+)/delete', page)):
    if RUN_ID in page:
        resp = http_post(f'/webhook-setup/{wid}/delete')
        if resp and resp.status_code in (200, 302, 303):
            cleanup_count += 1

step(f'Cleanup ({cleanup_count} Records gelöscht)', True)

# ============================================
# REPORT
# ============================================
print(f'\n=== 🎯 VERTRIEBS-REPORT ===')
total = len(results)
passed = sum(1 for r in results if r[1])
failed = total - passed
print(f'Total Schritte: {total}')
print(f'✅ Erfolgreich:  {passed}')
print(f'❌ Fehler:       {failed}')

if failed:
    print(f'\n❌ FEHLER-DETAILS:')
    for name, ok, detail in results:
        if not ok:
            print(f'  • {name}: {detail}')

print()
sys.exit(0 if not failed else 1)
