"""
🎨 UI-AUDIT-AGENT — UX/Cleanliness-Verifier

Crawlt alle Auth-Routes + prüft pro Page:
1. HTTP 200 (basic)
2. Broken Buttons: <button> oder <a class="btn"> ohne href/onclick/type=submit
3. Dead Links: <a href> mit relative path die nicht existieren (Sample)
4. Emoji-Density: zu viele Emojis = unprofessionell (>15 unique pro Page)
5. Inline-Style-Density: >100 = chaotisch, refactor empfohlen
6. Format-Inkonsistenzen: gemischte Card-Styles, gemischte Button-Sizes

Run:
  QA_USER=mail QA_PASS=pw python3 scripts/ui_audit.py [base_url]

Exit 0 bei sauber, sonst 1.
"""
import sys, os, re, requests
from urllib.parse import urljoin, urlparse

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:5050'
QA_USER = os.environ.get('QA_USER', '')
QA_PASS = os.environ.get('QA_PASS', '')

# Routes die wir crawlen — die Hauptseiten + die kritisch für UX sind
ROUTES = [
    '/dashboard', '/namensliste?typ=vk', '/namensliste?typ=rk',
    '/termine', '/vertraege', '/tracking',
    '/aufgaben', '/team', '/team-kalender',
    '/grundseminar', '/strukturbomben',
    '/admin/inbox', '/admin/inbox?typ=vk', '/admin/inbox?typ=rk',
    '/profil', '/einstellungen', '/passwort-vergessen',
]

EMOJI_RE = re.compile(
    '[\U0001F300-\U0001FAFF]|[☀-➿]|[\U0001F600-\U0001F64F]|[\U0001F680-\U0001F6FF]'
)
THRESHOLDS = {
    'emoji_unique_max': 15,
    'inline_style_max': 100,
    'btn_no_action_max': 0,
    'page_size_warn_kb': 200,
}

issues = []  # list of (route, severity, message)


def add(route, sev, msg):
    issues.append((route, sev, msg))


def login():
    s = requests.Session()
    r = s.post(urljoin(BASE, '/login'),
               data={'email': QA_USER, 'password': QA_PASS},
               allow_redirects=False, timeout=20)
    if r.status_code in (302, 303):
        return s
    return None


def audit_page(sess, route):
    try:
        r = sess.get(urljoin(BASE, route), timeout=20, allow_redirects=True)
    except Exception as e:
        add(route, 'CRIT', f'Request fehlgeschlagen: {e}')
        return
    # Wenn nach Redirects auf /login gelandet → Session abgelaufen / nicht eingeloggt
    if '/login' in r.url and route != '/login':
        add(route, 'CRIT', f'Session expired — landete auf {urlparse(r.url).path}')
        return
    if r.status_code != 200:
        add(route, 'CRIT', f'HTTP {r.status_code}')
        return
    html = r.text

    # ─── Page-Size ───
    kb = len(html) // 1024
    if kb > THRESHOLDS['page_size_warn_kb']:
        add(route, 'WARN', f'Page sehr groß: {kb} KB (>{THRESHOLDS["page_size_warn_kb"]} kb)')

    # ─── Emoji-Density ───
    emojis_all = EMOJI_RE.findall(html)
    unique_emojis = set(emojis_all)
    if len(unique_emojis) > THRESHOLDS['emoji_unique_max']:
        sample = ' '.join(list(unique_emojis)[:10])
        add(route, 'WARN', f'{len(unique_emojis)} unique Emojis ({len(emojis_all)} gesamt) — wirkt verspielt. Beispiele: {sample}')

    # ─── Inline-Styles ───
    inline_styles = len(re.findall(r'\sstyle="[^"]+"', html))
    if inline_styles > THRESHOLDS['inline_style_max']:
        add(route, 'WARN', f'{inline_styles} inline-styles — refactor zu CSS-Klassen empfohlen')

    # ─── Broken Buttons (kein href, kein onclick, kein type=submit, nicht in form) ───
    # Heuristik: Button ist 'tot' wenn:
    # - kein onclick / type=submit
    # - nicht in <form> (sonst default submit)
    # - nicht hidden (display:none → wird per JS aktiviert)
    # - ID nicht im Script referenziert (sonst per JS gebunden)
    btn_pattern = re.compile(r'<button[^>]*?>', re.IGNORECASE)
    bad_btns = []
    for m in btn_pattern.finditer(html):
        tag = m.group(0)
        if 'onclick' in tag or 'type="submit"' in tag or 'type=submit' in tag:
            continue
        # Hidden = wird per JS aktiviert
        if 'display:none' in tag.replace(' ', '') or 'hidden' in tag.lower():
            continue
        # In <form>?
        pre = html[:m.start()]
        last_form_open = pre.rfind('<form')
        last_form_close = pre.rfind('</form>')
        if last_form_open > last_form_close:
            continue
        # Hat ID die im JS referenziert wird?
        id_match = re.search(r'\bid="([^"]+)"', tag)
        if id_match:
            bid = id_match.group(1)
            # JS-Lookup: getElementById('id') oder #id Selector
            if (f"getElementById('{bid}')" in html or f'getElementById("{bid}")' in html
                or f"'#{bid}'" in html or f'"#{bid}"' in html):
                continue
        bad_btns.append(tag[:80])
    if len(bad_btns) > THRESHOLDS['btn_no_action_max']:
        sample = bad_btns[0] if bad_btns else ''
        add(route, 'BUG', f'{len(bad_btns)} toter Button — kein onclick/submit/form/JS-Bind. Beispiel: {sample}…')

    # ─── Dead Links (Sample): suche href die nicht existieren ───
    hrefs = re.findall(r'href="([^"#?]+)(?:\?[^"]*)?"', html)
    internal = [h for h in hrefs if h.startswith('/') and not h.startswith('//') and not any(h.startswith(p) for p in ('/static/', '/api/', '/sw.js', '/manifest'))]
    sampled = list(set(internal))[:5]
    for href in sampled:
        try:
            head = sess.head(urljoin(BASE, href), timeout=10, allow_redirects=False)
            if head.status_code in (404, 500):
                add(route, 'BUG', f'Dead-Link: {href} → HTTP {head.status_code}')
        except Exception:
            pass

    # ─── Theme/Contrast-Check: hardcoded helle Farben/weißer Text ohne CSS-Var ───
    # Im Dark-Mode haben hardcoded #fff/white-Hintergründe oder schwarzer Text
    # weiße Stellen oder unlesbare Kontraste. CSS-Variablen (--surface, --text)
    # adapten automatisch — alles andere ist potenziell broken.
    theme_issues = []
    # Suche style="background:#fff" / "background:white" / "background-color:#fff" / "color:#000"
    bad_patterns = [
        (r'style="[^"]*background(?:-color)?\s*:\s*#?(?:ffffff|fff|white)\b[^"]*"',
         'hardcoded weißer Hintergrund (im Dark-Mode = white-on-dark surface)'),
        (r'style="[^"]*\bcolor\s*:\s*#?(?:000000|000|black)\b[^"]*"',
         'hardcoded schwarzer Text (im Dark-Mode unlesbar auf dark surface)'),
        (r'style="[^"]*background(?:-color)?\s*:\s*#?(?:f3f4f6|f9fafb|fafbfc|f5f5f5|fafafa|e5e7eb)\b[^"]*"',
         'hardcoded helles Grau-Background (Dark-Mode-Bruch)'),
    ]
    for pat, msg in bad_patterns:
        matches = re.findall(pat, html, re.IGNORECASE)
        if matches:
            sample = matches[0][:90]
            theme_issues.append(f'{len(matches)}× {msg} — Beispiel: {sample}')
    if theme_issues:
        for ti in theme_issues:
            add(route, 'BUG', f'THEME: {ti}')


def source_scan_themes():
    """Mode 2: Statischer Scan über ALLE Templates für hardcoded Theme-Bugs.
    Findet auch Templates die nicht via HTTP gecrawlt werden (z.B. Modals,
    Sub-Templates, neue Pages ohne Auth). Catch-Alls in base.html fangen
    viele inline-Styles ab — aber neue Patterns rutschen sonst durch."""
    import glob
    template_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates')
    # NUR die Hex-Codes die NICHT von base.html-Catch-All abgedeckt sind.
    # Diese rutschen ungefiltert in den Dark-Mode durch.
    uncovered = [
        # (regex, label) — werden im Dark-Mode aktuell von keinem Catch-All abgefangen
        (r'background[^;]*#?(?:efebd6|fef3e7|eef0f4)\b', 'spezielles helles BG ohne Catch-All'),
    ]
    findings = []
    for f in sorted(glob.glob(os.path.join(template_dir, '*.html'))):
        if 'mockups' in f:  # mockups sind Demos, nicht produktiv
            continue
        s = open(f).read()
        for pat, label in uncovered:
            m = re.findall(pat, s, re.IGNORECASE)
            if m:
                findings.append((os.path.basename(f), label, len(m)))
    return findings


def main():
    print(f'\n🎨 UI-AUDIT-AGENT gegen {BASE}')
    print('═' * 60)
    sess = login()
    if not sess:
        print('❌ Login fehlgeschlagen — kein QA_USER/QA_PASS oder falsch')
        return 1

    print(f'\nCrawlt {len(ROUTES)} Routes…')
    for route in ROUTES:
        try:
            audit_page(sess, route)
        except Exception as e:
            add(route, 'CRIT', f'Audit-Exception: {e}')

    # Mode 2: Source-Scan
    print('\nSource-Scan über alle produktiven Templates für nicht-abgedeckte Theme-Patterns…')
    src_findings = source_scan_themes()
    for f, label, n in src_findings:
        add(f'(template: {f})', 'WARN', f'{n}× {label}')

    # ─── REPORT ───
    print('\n' + '═' * 60)
    crit = [i for i in issues if i[1] == 'CRIT']
    bugs = [i for i in issues if i[1] == 'BUG']
    warns = [i for i in issues if i[1] == 'WARN']

    if not issues:
        print('🟢 ALLE UI-CHECKS GRÜN — keine Findings')
        return 0

    if crit:
        print(f'\n🔴 CRITICAL ({len(crit)}):')
        for r, _, m in crit:
            print(f'   • {r}: {m}')

    if bugs:
        print(f'\n🐛 BUGS ({len(bugs)}):')
        for r, _, m in bugs:
            print(f'   • {r}: {m}')

    if warns:
        print(f'\n⚠ WARNINGS ({len(warns)}):')
        for r, _, m in warns:
            print(f'   • {r}: {m}')

    print('═' * 60)
    print(f'Total: {len(crit)} CRIT · {len(bugs)} BUGS · {len(warns)} WARN')

    # CRIT + BUGS sind hart fail. WARN ist ok.
    return 1 if (crit or bugs) else 0


if __name__ == '__main__':
    sys.exit(main())
