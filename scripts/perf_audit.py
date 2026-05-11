"""
⚡ Pro Academy Performance-Audit

Misst alle wichtigen Routes 3× live (cold+warm+warm) und ranked nach ms.

Usage:
  QA_USER=mail QA_PASS=pw python3 scripts/perf_audit.py [base_url]

Output: ms-Timing + p50/max + Empfehlungen für Top-3 Bottlenecks.
"""
import sys, os, time, requests, statistics
from urllib.parse import urljoin

BASE = sys.argv[1] if len(sys.argv) > 1 else 'https://proacademy-business.de'
QA_USER = os.environ.get('QA_USER', '')
QA_PASS = os.environ.get('QA_PASS', '')

# Top-Routes nach Häufigkeit + Geschäfts-Impact
ROUTES = [
    ('/login', 'GET', None, False),  # öffentlich
    ('/start', 'GET', None, False),
    ('/api/health', 'GET', None, False),
    ('/manifest.json', 'GET', None, False),
    ('/sw.js', 'GET', None, False),
    ('/dashboard', 'GET', None, True),  # admin
    ('/team', 'GET', None, True),
    ('/namensliste', 'GET', None, True),
    ('/termine', 'GET', None, True),
    ('/vertraege', 'GET', None, True),
    ('/tracking', 'GET', None, True),
    ('/team-kalender', 'GET', None, True),
    ('/team-kalender?root=all', 'GET', None, True),
    ('/profil', 'GET', None, True),
    ('/admin/inbox', 'GET', None, True),
    ('/admin/genehmigungen', 'GET', None, True),
]

def login():
    if not QA_USER or not QA_PASS:
        print('⚠ QA_USER/QA_PASS env vars setzen für Auth-Routes')
        return None
    s = requests.Session()
    r = s.post(urljoin(BASE, '/login'), data={'email': QA_USER, 'password': QA_PASS},
               allow_redirects=False, timeout=30)
    if r.status_code in (302, 303):
        return s
    print(f'✗ Login fehlgeschlagen: HTTP {r.status_code}')
    return None

def time_route(session_or_none, path, method='GET', n=3):
    """Misst n× hintereinander, returned (cold_ms, warm_ms, max_ms, status)."""
    sess = session_or_none or requests
    times = []
    status = 0
    for _ in range(n):
        try:
            t0 = time.time()
            r = sess.request(method, urljoin(BASE, path), allow_redirects=False, timeout=30)
            dur = (time.time() - t0) * 1000
            times.append(dur)
            status = r.status_code
        except Exception as e:
            return (None, None, None, 'ERR ' + str(e)[:50])
    return (times[0], statistics.median(times[1:]) if len(times) > 1 else times[0], max(times), status)


def main():
    print(f'\n⚡ PA-Performance-Audit gegen {BASE}')
    print('─' * 80)
    s = login() if any(needs_auth for _, _, _, needs_auth in ROUTES) else None
    print(f'{"Route":<35s} {"Cold":>8s} {"Warm-p50":>10s} {"Max":>8s} {"Status":>8s}')
    print('─' * 80)
    results = []
    for path, method, body, needs_auth in ROUTES:
        sess = s if needs_auth else None
        cold, warm, mx, status = time_route(sess, path, method, n=3)
        if cold is None:
            print(f'{path:<35s} {"ERR":>8s} {"":>10s} {"":>8s} {status:>8s}')
            continue
        results.append((path, cold, warm, mx, status))
        marker = '🔴' if warm > 1500 else ('⚠️' if warm > 800 else ('🟡' if warm > 300 else '✓'))
        print(f'{marker} {path:<33s} {cold:>6.0f}ms {warm:>8.0f}ms {mx:>6.0f}ms {str(status):>8s}')

    print('─' * 80)
    if results:
        print('\n🏆 TOP 5 LANGSAMSTE (Warm-p50):')
        for path, cold, warm, mx, status in sorted(results, key=lambda r: -r[2])[:5]:
            print(f'  {warm:>6.0f}ms  {path:<35s} (cold {cold:.0f}ms · max {mx:.0f}ms)')

if __name__ == '__main__':
    main()
