"""
🔧 IMPROVEMENT-AGENT — Read-only Optimizer-Scout

Scannt das Projekt nach Verbesserungs-Möglichkeiten und SCHLÄGT sie vor.
Macht KEINE automatischen Code-Änderungen — du entscheidest was umgesetzt wird.

Findings:
1. Outdated Python-Pakete (pip list --outdated)
2. Security-Alerts via pip-audit (wenn installiert)
3. Performance-Hotspots: TOP 5 längste Routes (aus app.py)
4. Code-Smells: TODO/FIXME/XXX-Kommentare ohne Owner
5. Tote Files: Templates die nirgends mehr extends/include sind
6. Cache-Effizienz: Zeigt ob L2-Cache Files veraltet/zu viele

Run:
  python3 scripts/improve_audit.py [--verbose]

Exit code:
  0 = nur Vorschläge, keine kritischen Findings
  1 = mind. 1 SECURITY-Finding (CVE-relevant) → User sollte handeln
"""
import sys, os, re, sqlite3, subprocess, glob

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, 'vertrieb.db')

suggestions = []  # (severity, category, message)
critical = 0


def add(sev, cat, msg):
    global critical
    suggestions.append((sev, cat, msg))
    if sev == 'SECURITY':
        critical += 1


# ─── 1. Outdated packages ───
def check_outdated():
    print('1️⃣  Outdated Python-Pakete…')
    try:
        r = subprocess.run(['python3', '-m', 'pip', 'list', '--outdated', '--format=columns'],
                          capture_output=True, text=True, timeout=30)
        lines = [l for l in (r.stdout or '').splitlines() if l and not l.startswith('Package') and not l.startswith('---')]
        if not lines:
            print('   ✓ Alle Pakete aktuell')
            return
        # Filter auf Pakete die wir tatsächlich nutzen
        used_pkgs = set()
        for f in glob.glob(os.path.join(REPO, '*.py')) + glob.glob(os.path.join(REPO, 'scripts/*.py')):
            try:
                src = open(f).read()
                for m in re.finditer(r'^(?:from|import)\s+([a-zA-Z0-9_]+)', src, re.MULTILINE):
                    used_pkgs.add(m.group(1).lower())
            except Exception:
                pass
        relevant = []
        for line in lines[:25]:  # cap
            parts = line.split()
            if not parts:
                continue
            pkg = parts[0].lower().replace('-', '_')
            # Match imports loose
            if any(pkg.startswith(u) or u.startswith(pkg) for u in used_pkgs):
                relevant.append(' '.join(parts[:3]))
        if relevant:
            add('UPDATE', 'deps', f'{len(relevant)} verwendete Pakete haben Updates: {", ".join(relevant[:8])}{"…" if len(relevant) > 8 else ""}')
            print(f'   ⚠ {len(relevant)} verwendete Pakete outdated')
        else:
            print('   ✓ Verwendete Pakete aktuell')
    except Exception as e:
        print(f'   ⚠ pip-Check fail: {e}')


# ─── 2. pip-audit (CVEs) ───
def check_security():
    print('2️⃣  Security-Audit (pip-audit)…')
    try:
        r = subprocess.run(['pip-audit', '--format=columns', '--progress-spinner', 'off'],
                          capture_output=True, text=True, timeout=60)
        out = (r.stdout or '') + (r.stderr or '')
        if 'No known vulnerabilities' in out or 'no vulnerabilities found' in out.lower():
            print('   ✓ Keine bekannten Vulnerabilities')
            return
        # Zeilen mit "GHSA" oder "CVE" sind echte Findings
        cves = [l for l in out.splitlines() if 'GHSA-' in l or 'CVE-' in l]
        if cves:
            add('SECURITY', 'cve', f'{len(cves)} CVE/GHSA-Findings: {cves[0][:200]}')
            print(f'   🔴 {len(cves)} CVE/GHSA-Findings')
        else:
            print('   ✓ pip-audit lief, keine CVEs gemeldet')
    except FileNotFoundError:
        print('   ⓘ pip-audit nicht installiert — `pip install pip-audit` für Security-Scan')
    except Exception as e:
        print(f'   ⚠ pip-audit fail: {e}')


# ─── 3. Performance: längste Routes (Heuristik via line-count zwischen @app.route) ───
def check_perf_hotspots():
    print('3️⃣  Performance-Hotspots…')
    src = open(os.path.join(REPO, 'app.py')).read().splitlines()
    routes = []  # (route_path, def_name, line_count)
    cur_route = None
    cur_def = None
    cur_start = 0
    for i, line in enumerate(src):
        m = re.match(r"@app\.route\(['\"]([^'\"]+)['\"]", line)
        if m:
            if cur_route:
                routes.append((cur_route, cur_def, i - cur_start))
            cur_route = m.group(1)
            cur_start = i
            cur_def = None
        m2 = re.match(r'def ([a-zA-Z0-9_]+)\(', line)
        if m2 and cur_def is None:
            cur_def = m2.group(1)
    routes.sort(key=lambda r: -r[2])
    top5 = routes[:5]
    if top5:
        msg = ' · '.join(f'{r[0]} ({r[2]} Zeilen)' for r in top5)
        add('PERF', 'long_routes', f'TOP-5 längste Routes — Refactor-Kandidaten: {msg}')
        print(f'   ⓘ TOP längste: {top5[0][0]} ({top5[0][2]}Z)')


# ─── 4. TODO/FIXME ohne Owner ───
def check_todos():
    print('4️⃣  TODO/FIXME-Kommentare…')
    todos = []
    for f in glob.glob(os.path.join(REPO, '*.py')) + glob.glob(os.path.join(REPO, 'templates/*.html')):
        try:
            for ln, line in enumerate(open(f), 1):
                m = re.search(r'(TODO|FIXME|XXX|HACK)[\s:\-]', line, re.IGNORECASE)
                if m and not re.search(r'(TODO|FIXME)\s*\([^)]+\)', line):  # ohne Owner
                    todos.append(f'{os.path.basename(f)}:{ln}')
        except Exception:
            pass
    if todos:
        add('TODO', 'unowned', f'{len(todos)} TODO/FIXME ohne Owner. Erste 5: {", ".join(todos[:5])}')
        print(f'   ⓘ {len(todos)} ungetaggte TODOs')
    else:
        print('   ✓ Keine ungetaggten TODOs')


# ─── 5. Tote Templates ───
def check_dead_templates():
    print('5️⃣  Tote Templates…')
    template_dir = os.path.join(REPO, 'templates')
    all_templates = {os.path.basename(f) for f in glob.glob(os.path.join(template_dir, '*.html'))}
    referenced = set()
    # render_template + extends + include
    for f in glob.glob(os.path.join(REPO, '*.py')) + glob.glob(os.path.join(template_dir, '*.html')):
        try:
            src = open(f).read()
            for m in re.finditer(r"(?:render_template|extends|include)\s*\(?\s*['\"]([^'\"]+\.html)['\"]", src):
                referenced.add(m.group(1))
        except Exception:
            pass
    dead = all_templates - referenced
    # base.html ist immer "tot" weil über extends nicht direkt geladen — checke ob extends-Target
    dead_real = set()
    for d in dead:
        is_extend_target = False
        for f in glob.glob(os.path.join(template_dir, '*.html')):
            try:
                if f"extends \"{d}\"" in open(f).read() or f"extends '{d}'" in open(f).read():
                    is_extend_target = True
                    break
            except Exception:
                pass
        if not is_extend_target:
            dead_real.add(d)
    if dead_real:
        add('CLEANUP', 'dead_templates', f'{len(dead_real)} ungenutzte Templates: {", ".join(sorted(dead_real)[:8])}')
        print(f'   ⚠ {len(dead_real)} tote Templates')
    else:
        print('   ✓ Alle Templates referenziert')


# ─── 6. Cache-Effizienz ───
def check_cache():
    print('6️⃣  L2-Filesystem-Cache…')
    cache_dir = '/tmp/proacademy-cache'
    if not os.path.isdir(cache_dir):
        print('   ⓘ Kein L2-Cache-Verzeichnis (vermutlich noch nie gestartet)')
        return
    files = glob.glob(os.path.join(cache_dir, '*.pkl'))
    if not files:
        print('   ⓘ L2-Cache leer')
        return
    import time
    now = time.time()
    old = sum(1 for f in files if (now - os.path.getmtime(f)) > 3600)
    total_size = sum(os.path.getsize(f) for f in files) // 1024
    print(f'   ⓘ {len(files)} Cache-Files · {total_size} KB · {old} älter als 1h')
    if old > len(files) * 0.5:
        add('CACHE', 'stale', f'>50% L2-Files >1h alt — evtl. cleanup-cron sinnvoll')


def main():
    print('\n🔧 IMPROVEMENT-AGENT — read-only Optimizer-Scout')
    print('═' * 60)
    print()
    check_outdated()
    print()
    check_security()
    print()
    check_perf_hotspots()
    print()
    check_todos()
    print()
    check_dead_templates()
    print()
    check_cache()

    # ─── REPORT ───
    print('\n' + '═' * 60)
    if not suggestions:
        print('🟢 Keine Verbesserungsvorschläge — Projekt ist clean')
        return 0
    by_sev = {'SECURITY': '🔴', 'UPDATE': '⬆', 'PERF': '⚡', 'CLEANUP': '🧹', 'TODO': '📝', 'CACHE': '💾'}
    for sev, cat, msg in suggestions:
        print(f'  {by_sev.get(sev, "•")} [{sev}] {msg}')
    print('═' * 60)
    print(f'Total: {len(suggestions)} Vorschläge · {critical} kritisch (Security)')
    return 1 if critical > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
