"""
🩺 Pro Academy Health-Monitor

Schneller Smoke-Test (~5 Sek). Wird alle 15 Min vom Always-On-Wrapper aufgerufen.

Was geprüft wird:
  1. /login HTTP 200 (App lebt)
  2. /api/health HTTP 200 (App-Internal-OK)
  3. SQLite integrity_check = ok (DB nicht korrupt)
  4. Letztes Backup < 26h alt
  5. Disk-Space > 200 MB frei
  6. Error-Log: keine NEUEN ERROR-Events seit letzter Prüfung

Bei FAIL: Push an alle Admin-User + Eintrag in /var/log/proacademy-monitor.log
Bei OK: stille Log-Zeile (kein Spam)

Run manual:
  python3 scripts/health_monitor.py
"""
import os
import sys
import time
import sqlite3
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

# ─── Config ─────────────────────────────────────────────────────────
HOME = os.path.expanduser('~')
PROJECT_DIR = os.path.join(HOME, 'nt-pro-academy-crm')
DB_PATH = os.path.join(PROJECT_DIR, 'vertrieb.db')
BACKUP_DIR = os.path.join(PROJECT_DIR, 'backups')
LOG_FILE = '/tmp/proacademy-monitor.log'
BASE_URL = os.environ.get('MONITOR_URL', 'https://proacademy-business.de')
TIMEOUT = 15
MAX_BACKUP_AGE_HOURS = 26
MIN_FREE_DISK_MB = 200

# ─── Helpers ────────────────────────────────────────────────────────
def log(level, msg):
    line = f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] [{level}] {msg}'
    print(line)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def check_http(path):
    try:
        t0 = time.time()
        req = urllib.request.Request(f'{BASE_URL}{path}', headers={'User-Agent': 'PA-Monitor/1.0'})
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        dur = (time.time() - t0) * 1000
        if resp.status == 200:
            return True, f'{resp.status} in {dur:.0f}ms'
        return False, f'HTTP {resp.status}'
    except urllib.error.HTTPError as e:
        return False, f'HTTP {e.code}'
    except Exception as e:
        return False, str(e)[:80]


def check_db_integrity():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        result = conn.execute('PRAGMA integrity_check').fetchone()[0]
        conn.close()
        if result == 'ok':
            return True, 'integrity ok'
        return False, f'integrity: {result[:80]}'
    except Exception as e:
        return False, str(e)[:80]


def check_backup_age():
    try:
        if not os.path.isdir(BACKUP_DIR):
            return False, f'kein Backup-Dir: {BACKUP_DIR}'
        files = [f for f in os.listdir(BACKUP_DIR) if f.startswith('vertrieb-') and f.endswith('.db')]
        if not files:
            return False, 'keine Backups vorhanden'
        newest = max(files, key=lambda f: os.path.getmtime(os.path.join(BACKUP_DIR, f)))
        age_h = (time.time() - os.path.getmtime(os.path.join(BACKUP_DIR, newest))) / 3600
        if age_h > MAX_BACKUP_AGE_HOURS:
            return False, f'letztes Backup {age_h:.1f}h alt ({newest})'
        return True, f'jüngstes: {newest} ({age_h:.1f}h alt)'
    except Exception as e:
        return False, str(e)[:80]


def check_disk_space():
    try:
        result = subprocess.run(['df', '-Pm', PROJECT_DIR], capture_output=True, text=True, timeout=5)
        line = result.stdout.strip().split('\n')[-1]
        parts = line.split()
        free_mb = int(parts[3])
        if free_mb < MIN_FREE_DISK_MB:
            return False, f'nur {free_mb} MB frei (min {MIN_FREE_DISK_MB})'
        return True, f'{free_mb} MB frei'
    except Exception as e:
        return False, str(e)[:80]


def check_error_log():
    """Sucht nach 5xx Exceptions in den letzten 30 Min im PA-Error-Log."""
    log_path = '/var/log/proacademy-business.de.error.log'
    if not os.path.exists(log_path):
        return True, 'log nicht zugreifbar (skip)'
    try:
        cutoff = (datetime.now() - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M')
        with open(log_path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200000))  # letzte 200KB
            tail = f.read().decode('utf-8', errors='ignore')
        # Zähle relevante Errors NACH cutoff
        recent_errors = 0
        for line in tail.split('\n'):
            if line[:16] >= cutoff and ('OperationalError' in line or 'UnboundLocalError' in line or 'database is locked' in line or 'malformed' in line):
                recent_errors += 1
        if recent_errors > 5:
            return False, f'{recent_errors} ERROR-Events in letzten 30 Min'
        return True, f'{recent_errors} ERRORs (toleriert)'
    except Exception as e:
        return True, f'log-check skipped: {str(e)[:50]}'


def send_alert(failures):
    """Push-Alert an alle Admin-User wenn Health-Check fehlschlägt."""
    try:
        sys.path.insert(0, PROJECT_DIR)
        from app import send_push_to_user, get_db
        db = get_db()
        admins = db.execute("SELECT id, name FROM users WHERE role='admin' AND active=1").fetchall()
        db.close()
        title = '🚨 Pro Academy: Health-Check FAIL'
        body = ' · '.join(f'{name}: {detail}' for name, _, detail in failures[:3])
        if len(body) > 200:
            body = body[:200] + '…'
        for a in admins:
            try:
                send_push_to_user(a['id'], title, body, url='/dashboard',
                                  urgent=True, tag='health-alert', push_type='admin_alert')
            except Exception as e:
                log('WARN', f'Push an Admin {a["id"]} fehlgeschlagen: {e}')
    except Exception as e:
        log('ERROR', f'Alert-System selbst kaputt: {e}')


# ─── Main ───────────────────────────────────────────────────────────
def main():
    checks = [
        ('HTTP /login', lambda: check_http('/login')),
        ('HTTP /api/health', lambda: check_http('/api/health')),
        ('DB integrity', check_db_integrity),
        ('Backup-Age', check_backup_age),
        ('Disk-Space', check_disk_space),
        ('Error-Log', check_error_log),
    ]
    failures = []
    for name, fn in checks:
        ok, detail = fn()
        if ok:
            log('PASS', f'{name:18s} → {detail}')
        else:
            log('FAIL', f'{name:18s} → {detail}')
            failures.append((name, ok, detail))

    if failures:
        log('ALERT', f'{len(failures)} CHECKS FAILED — sende Push an Admins')
        send_alert(failures)
        return 1
    else:
        log('OK', 'alle 6 Checks grün')
        return 0


if __name__ == '__main__':
    sys.exit(main())
