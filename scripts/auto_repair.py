"""
🔧 Pro Academy Auto-Repair-Engine

Wird vom Health-Monitor aufgerufen wenn ein Check fehlschlägt.
Versucht den Failure automatisch zu beheben.

REPAIR-MODUS A (Stilles Heal):
  - Repair erfolgreich → KEINE Push (nur Log)
  - Repair fehlgeschlagen → Push an Admin mit Anweisung
  - Nicht-heilbarer Failure-Type → Push an Admin mit Stacktrace

ANTI-LOOP-SCHUTZ:
  - Max 3 Repair-Attempts pro failure_type pro Stunde.
  - Sonst Eskalation (Push „braucht echten Eingriff — Loop-Schutz").

API:
  attempt_repair(failure_type, detail_str) → dict {
      'attempted': bool,
      'success': bool,
      'message': str,
      'repair_kind': str | None,
  }
"""
import os
import sys
import json
import time
import sqlite3
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

HOME = os.path.expanduser('~')
PROJECT_DIR = os.path.join(HOME, 'nt-pro-academy-crm')
DB_PATH = os.path.join(PROJECT_DIR, 'vertrieb.db')
BACKUP_DIR = os.path.join(PROJECT_DIR, 'backups')
WSGI_PATH = '/var/www/proacademy-business_de_wsgi.py'
REPAIR_LOG = '/tmp/proacademy-repair-history.log'
LOOP_STATE = '/tmp/proacademy-repair-state.json'

MAX_ATTEMPTS_PER_HOUR = 3


def _log(level, msg):
    line = f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] [{level}] {msg}'
    print(line)
    try:
        with open(REPAIR_LOG, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _load_state():
    try:
        with open(LOOP_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        with open(LOOP_STATE, 'w') as f:
            json.dump(state, f)
    except Exception:
        pass


def _check_loop(failure_type):
    """Anti-Loop: max 3 Repair-Versuche pro failure_type in letzter Stunde."""
    now = time.time()
    state = _load_state()
    history = state.get(failure_type, [])
    # alte Attempts (>1h) entfernen
    history = [t for t in history if now - t < 3600]
    if len(history) >= MAX_ATTEMPTS_PER_HOUR:
        return False, f'Loop-Schutz: bereits {len(history)}× in letzter Stunde versucht'
    history.append(now)
    state[failure_type] = history
    _save_state(state)
    return True, ''


# ─── Repair-Aktionen ────────────────────────────────────────────────

def _repair_db_integrity(detail):
    """SQLite-DB beschädigt → .recover + swap (wie heute Nacht manuell)."""
    try:
        # 1. Backup der korrupten DB
        ts = datetime.now().strftime('%H%M%S')
        corrupt_copy = os.path.join(PROJECT_DIR, f'vertrieb-AUTO-CORRUPT-{ts}.db')
        shutil.copy2(DB_PATH, corrupt_copy)
        # 2. .recover via subprocess (sqlite3-CLI)
        recover_sql = f'/tmp/auto-recover-{ts}.sql'
        result = subprocess.run(
            ['sqlite3', corrupt_copy, '.recover'],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0 and not result.stdout:
            return False, f'.recover fehlgeschlagen: {result.stderr[:200]}'
        with open(recover_sql, 'w') as f:
            f.write(result.stdout)
        # 3. Neue DB aus Recovery-SQL
        new_db = os.path.join(PROJECT_DIR, f'vertrieb-AUTO-NEW-{ts}.db')
        if os.path.exists(new_db):
            os.remove(new_db)
        with open(recover_sql) as f:
            sql_content = f.read()
        result2 = subprocess.run(
            ['sqlite3', new_db],
            input=sql_content, capture_output=True, text=True, timeout=120
        )
        # 4. Integrity-Check der neuen DB
        check = subprocess.run(
            ['sqlite3', new_db, 'PRAGMA integrity_check'],
            capture_output=True, text=True, timeout=30
        )
        if check.stdout.strip() != 'ok':
            return False, f'Recovered DB immer noch corrupt: {check.stdout[:100]}'
        # 5. Live-DB ersetzen
        # WAL/SHM weg
        for ext in ('.db-shm', '.db-wal'):
            p = DB_PATH + ext.replace('.db', '')
            if os.path.exists(p):
                os.remove(p)
        # Atomic-Swap via rename
        os.replace(new_db, DB_PATH)
        # 6. WSGI touch → App neu laden
        Path(WSGI_PATH).touch()
        return True, 'DB recovered, swapped, WSGI reloaded'
    except Exception as e:
        return False, f'Repair-Exception: {str(e)[:150]}'


def _repair_disk_low(detail):
    """Disk voll → /tmp aufräumen + alte CORRUPT-DBs + Backups>30d."""
    try:
        cleaned_mb = 0
        # /tmp Cleanup (eigene Dateien)
        for f in os.listdir('/tmp'):
            if f.startswith('proacademy-') or f.startswith('auto-recover-'):
                try:
                    p = os.path.join('/tmp', f)
                    if os.path.isfile(p) and time.time() - os.path.getmtime(p) > 86400:
                        cleaned_mb += os.path.getsize(p) / 1024 / 1024
                        os.remove(p)
                except Exception:
                    pass
        # Alte CORRUPT-DBs (>7d)
        for f in os.listdir(PROJECT_DIR):
            if 'CORRUPT' in f and f.endswith('.db'):
                try:
                    p = os.path.join(PROJECT_DIR, f)
                    if time.time() - os.path.getmtime(p) > 7 * 86400:
                        cleaned_mb += os.path.getsize(p) / 1024 / 1024
                        os.remove(p)
                except Exception:
                    pass
        # Backups älter als 30d (db_backup.sh macht das auch — defensiv hier)
        if os.path.isdir(BACKUP_DIR):
            for f in os.listdir(BACKUP_DIR):
                if f.startswith('vertrieb-') and f.endswith('.db'):
                    try:
                        p = os.path.join(BACKUP_DIR, f)
                        if time.time() - os.path.getmtime(p) > 30 * 86400:
                            cleaned_mb += os.path.getsize(p) / 1024 / 1024
                            os.remove(p)
                    except Exception:
                        pass
        return True, f'{cleaned_mb:.1f} MB Cleanup'
    except Exception as e:
        return False, f'Disk-Cleanup-Exception: {str(e)[:150]}'


def _repair_backup_old(detail):
    """Letztes Backup zu alt → ad-hoc Backup-Script triggern."""
    try:
        script = os.path.join(PROJECT_DIR, 'scripts', 'db_backup.sh')
        if not os.path.exists(script):
            return False, f'Script fehlt: {script}'
        result = subprocess.run(
            ['bash', script], capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return False, f'Backup-Script Exit {result.returncode}: {result.stderr[:200]}'
        return True, f'Backup ad-hoc erstellt: {result.stdout.strip()[:100]}'
    except Exception as e:
        return False, f'Backup-Exception: {str(e)[:150]}'


def _repair_app_hung(detail):
    """App antwortet nicht → WSGI-Touch (Reload)."""
    try:
        Path(WSGI_PATH).touch()
        return True, 'WSGI getouched (App reloadet)'
    except Exception as e:
        return False, f'Touch-Exception: {str(e)[:150]}'


def _repair_push_subs_invalid(detail):
    """Failed Push-Subs (410 Gone) → DB-Cleanup."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        # Wir können hier nicht erkennen welche invalid sind ohne ne Probe-Push
        # → wir vertrauen auf send_push's eigenen 410-Cleanup-Pfad
        # → hier nur duplicate Subs entfernen (wenn jemand 5× registriert)
        cur = conn.execute('''
            DELETE FROM push_subscriptions
            WHERE id NOT IN (
                SELECT MIN(id) FROM push_subscriptions GROUP BY user_id, endpoint
            )
        ''')
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return True, f'{deleted} duplicate Push-Subs entfernt'
    except Exception as e:
        return False, f'Push-Cleanup-Exception: {str(e)[:150]}'


# ─── Repair-Mapping ────────────────────────────────────────────────

REPAIRS = {
    'db_integrity': ('DB-Recovery via .recover + swap', _repair_db_integrity),
    'disk_low':     ('Disk-Cleanup (/tmp + alte CORRUPT + Backups>30d)', _repair_disk_low),
    'backup_old':   ('Backup-Script ad-hoc triggern', _repair_backup_old),
    'app_hung':     ('WSGI-Touch (App-Reload)', _repair_app_hung),
    'push_subs':    ('Duplicate Push-Subs entfernen', _repair_push_subs_invalid),
}


def attempt_repair(failure_type, detail=''):
    """Versucht Failure zu beheben.
    Returns dict {attempted, success, message, repair_kind}."""
    if failure_type not in REPAIRS:
        return {
            'attempted': False, 'success': False,
            'message': f'kein Auto-Repair für "{failure_type}" — manueller Eingriff nötig',
            'repair_kind': None,
        }
    repair_kind, fn = REPAIRS[failure_type]
    # Anti-Loop
    ok, loop_msg = _check_loop(failure_type)
    if not ok:
        return {
            'attempted': False, 'success': False,
            'message': loop_msg,
            'repair_kind': repair_kind,
        }
    _log('REPAIR', f'Versuche: {failure_type} → {repair_kind} ({detail[:80]})')
    success, repair_msg = fn(detail)
    level = 'OK' if success else 'FAIL'
    _log(level, f'{failure_type}: {repair_msg}')
    return {
        'attempted': True, 'success': success,
        'message': repair_msg, 'repair_kind': repair_kind,
    }


if __name__ == '__main__':
    # CLI-Test: python3 auto_repair.py <failure_type> [detail]
    if len(sys.argv) < 2:
        print('Verfügbare Repair-Types:')
        for k, (kind, _) in REPAIRS.items():
            print(f'  {k:15s} → {kind}')
        sys.exit(0)
    ft = sys.argv[1]
    det = sys.argv[2] if len(sys.argv) > 2 else ''
    result = attempt_repair(ft, det)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result.get('success') else 1)
