from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session, send_file, send_from_directory, Response
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import hashlib
import hmac
import subprocess
import gzip as _gzip
import os
import secrets
import csv
import io
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from datetime import date, datetime, timedelta
import json

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-only-change-in-production-2024')

# Session-Security
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('FLASK_DEBUG', '1') != '1',  # nur HTTPS in production
    PERMANENT_SESSION_LIFETIME=timedelta(days=60),  # 60 Tage Session — User bleibt eingeloggt
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16 MB max upload
    SEND_FILE_MAX_AGE_DEFAULT=86400,  # static assets cached 24h browser-side
    TEMPLATES_AUTO_RELOAD=(os.environ.get('FLASK_DEBUG', '1') == '1'),  # production: aus
    # Flask-Login Remember-Me — User bleibt selbst nach Browser/Handy-Restart drin
    REMEMBER_COOKIE_DURATION=timedelta(days=60),
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SECURE=os.environ.get('FLASK_DEBUG', '1') != '1',
    REMEMBER_COOKIE_SAMESITE='Lax',
    REMEMBER_COOKIE_REFRESH_EACH_REQUEST=True,  # Cookie verlängert sich bei jedem Request
)

# Performance: GZIP-Compression für Text-Responses (3-5× kleinere Payload)
@app.after_request
def gzip_response(response):
    try:
        accept = (request.headers.get('Accept-Encoding') or '').lower()
        if 'gzip' not in accept:
            return response
        if response.status_code < 200 or response.status_code >= 300:
            return response
        if 'Content-Encoding' in response.headers:
            return response
        ctype = (response.content_type or '').lower()
        if not any(t in ctype for t in ('text/html', 'application/json', 'text/css',
                                        'application/javascript', 'text/javascript',
                                        'application/xml', 'text/plain')):
            return response
        if not response.is_sequence and not hasattr(response, 'data'):
            return response
        data = response.get_data()
        if len(data) < 500:  # < 500B: gzip-Overhead lohnt nicht
            return response
        compressed = _gzip.compress(data, compresslevel=6)
        response.set_data(compressed)
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Content-Length'] = str(len(compressed))
        vary = response.headers.get('Vary', '')
        if 'Accept-Encoding' not in vary:
            response.headers['Vary'] = (vary + ', Accept-Encoding').strip(', ')
    except Exception:
        pass
    return response


# Performance: globale Cache-Headers für statische Assets
@app.after_request
def add_cache_headers(response):
    try:
        # Static assets: aggressives Browser-Caching
        if request.path.startswith('/static/'):
            # Bilder/Fonts: 1 Jahr immutable (Browser cached aggressive — bei Änderung URL bumpen)
            if any(request.path.endswith(ext) for ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.woff', '.woff2', '.ttf', '.otf', '.ico')):
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            # CSS/JS: 7 Tage (Updates kommen häufiger)
            elif any(request.path.endswith(ext) for ext in ('.css', '.js')):
                response.headers['Cache-Control'] = 'public, max-age=604800'
            else:
                response.headers['Cache-Control'] = 'public, max-age=3600'
        # HTML: kein langes Cachen, aber Validators erlaubt
        elif response.content_type and 'text/html' in response.content_type:
            # /dashboard darf 30s Browser-Cache (Reload = instant)
            if request.path == '/dashboard':
                response.headers.setdefault('Cache-Control', 'private, max-age=30, must-revalidate')
            else:
                response.headers.setdefault('Cache-Control', 'private, max-age=0, must-revalidate')
        # Security-Headers für alle Responses
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    except Exception:
        pass
    return response

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Rate-Limiting für Login (in-memory)
_LOGIN_ATTEMPTS = {}
_LOGIN_LOCK = threading.Lock() if 'threading' in dir() else None

# === Canonical-URL ===
# Wird in E-Mails / Push-Bodies / Public-Anleitungen verwendet wo kein
# request-Context da ist. Override per env var falls nötig.
CANONICAL_URL = os.environ.get('CANONICAL_URL', 'https://proacademy-business.de')
CANONICAL_HOST = CANONICAL_URL.replace('https://', '').replace('http://', '').rstrip('/')

def is_login_blocked(ip_or_email):
    if not _LOGIN_LOCK:
        return False
    with _LOGIN_LOCK:
        entry = _LOGIN_ATTEMPTS.get(ip_or_email)
        if not entry:
            return False
        if time.time() - entry['first_at'] > 900:  # 15 min Window
            del _LOGIN_ATTEMPTS[ip_or_email]
            return False
        return entry['count'] >= 10

def record_login_attempt(ip_or_email, success=False):
    if not _LOGIN_LOCK:
        return
    with _LOGIN_LOCK:
        if success:
            _LOGIN_ATTEMPTS.pop(ip_or_email, None)
            return
        entry = _LOGIN_ATTEMPTS.get(ip_or_email, {'count': 0, 'first_at': time.time()})
        entry['count'] += 1
        _LOGIN_ATTEMPTS[ip_or_email] = entry

# DB-Pfad: lokal im Projektordner, in Production auf persistenter Disk
DATA_DIR = os.environ.get('DATA_DIR') or os.path.dirname(__file__)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'vertrieb.db')

# === KARRIERE-STUFEN ===
# Basis: 1€ Kundenvolumen = 0,8 EH (Einheiten)
EH_FAKTOR = 0.8

CAREER_LEVELS = [
    {'level': 1, 'name': 'Repräsentant',           'short': 'REP',  'min_eh': 0,     'commission': 5.00,  'color': '#94a3b8',
     'rules': []},
    {'level': 2, 'name': 'Leitender Repräsentant', 'short': 'LREP', 'min_eh': 1000,  'commission': 9.50,  'color': '#3b82f6',
     'rules': [
         {'type': 'gesamt_eh', 'target': 1000,
          'label': 'Gesamt-EH (eigen + Team)', 'hint': 'Egal wie aufgeteilt'}
     ]},
    {'level': 3, 'name': 'Hauptrepräsentant',      'short': 'HREP', 'min_eh': 3500,  'commission': 14.00, 'color': '#8b5cf6',
     'rules': [
         {'type': 'gesamt_eh', 'target': 3500,
          'label': 'Gesamt-EH', 'hint': 'eigen + Team'},
         {'type': 'max_strang_pct', 'pct': 70,
          'label': 'Max. 70% aus einem Strang', 'hint': 'mind. 30% diversifiziert (andere Stränge oder eigen)'}
     ]},
    {'level': 4, 'name': 'Chefrepräsentant',       'short': 'CREP', 'min_eh': 9000,  'commission': 18.00, 'color': '#10b981',
     'rules': [
         {'type': 'gesamt_eh', 'target': 9000,
          'label': 'Gesamt-EH', 'hint': 'eigen + Team'},
         {'type': 'qualified_straenge', 'min_count': 2, 'min_per_strang': 1200, 'max_per_strang': 4500,
          'label': 'Mind. 2 qualifizierte Stränge', 'hint': 'je 1.200 - 4.500 EH (Strang zählt erst ab 1.200)'},
         {'type': 'restbereich_min', 'min_eh': 1200,
          'label': 'Restbereich min. 1.200 EH', 'hint': 'Pflicht: eigene EH + kleine Stränge'}
     ]},
    {'level': 5, 'name': 'Direktionsrepräsentant', 'short': 'DREP', 'min_eh': 25000, 'commission': 20.70, 'color': '#c08a2e',
     'rules': [
         {'type': 'gesamt_eh', 'target': 25000,
          'label': 'Gesamt-EH', 'hint': 'eigen + Team'},
         {'type': 'qualified_straenge', 'min_count': 2, 'min_per_strang': 1200, 'max_per_strang': 99999,
          'label': 'Mind. 2 qualifizierte Stränge', 'hint': 'je mind. 1.200 EH'},
         {'type': 'max_per_strang', 'cap': 12500,
          'label': 'Max. 12.500 EH pro Strang', 'hint': 'Diversifikation'},
         {'type': 'restbereich_min', 'min_eh': 1800,
          'label': 'Restbereich min. 1.800 EH', 'hint': 'Pflicht: eigene EH + kleine Stränge'}
     ]},
    {'level': 6, 'name': 'Generalrepräsentant',    'short': 'GREP', 'min_eh': 60000, 'commission': 23.00, 'color': '#92400e',
     'rules': [
         {'type': 'gesamt_eh', 'target': 60000,
          'label': 'Gesamt-EH', 'hint': 'eigen + Team'},
         {'type': 'qualified_straenge', 'min_count': 2, 'min_per_strang': 1200, 'max_per_strang': 99999,
          'label': 'Mind. 2 qualifizierte Stränge', 'hint': 'je mind. 1.200 EH'},
         {'type': 'max_per_strang', 'cap': 12500,
          'label': 'Max. 12.500 EH pro Strang', 'hint': 'Wie Stufe 5'},
         {'type': 'restbereich_min', 'min_eh': 2400,
          'label': 'Restbereich min. 2.400 EH', 'hint': 'Pflicht: eigene EH + kleine Stränge'}
     ]},
]

TERMINE_PRO_ABSCHLUSS = 3  # Konversionsrate: ca. 3 Termine = 1 Abschluss


def get_career_level(total_eh):
    """Berechnet die Karriere-Stufe basierend auf Gesamt-EH"""
    current = CAREER_LEVELS[0]
    for cl in CAREER_LEVELS:
        if total_eh >= cl['min_eh']:
            current = cl
        else:
            break
    return current


def get_next_level(current_level):
    """Liefert die nächste Stufe"""
    for cl in CAREER_LEVELS:
        if cl['level'] == current_level + 1:
            return cl
    return None


def get_db():
    """Liefert eine SQLite-Connection mit WAL-Mode für hohe Concurrency.
    WAL erlaubt parallele Reads + 1 Writer ohne 'database is locked'-Fehler."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL-Mode: parallele Reads, schnelle Writes, weniger Locks
    conn.execute('PRAGMA journal_mode=WAL')
    # Wenn DB doch mal locked → 30s warten statt sofort fail
    conn.execute('PRAGMA busy_timeout=30000')
    # Schneller writes (sicher genug für Web-App, kein DB-Verlust nur letzte Sekunden)
    conn.execute('PRAGMA synchronous=NORMAL')
    # Foreign-Keys aktiv
    conn.execute('PRAGMA foreign_keys=ON')
    # Cache-Size ~10MB
    conn.execute('PRAGMA cache_size=-10000')
    return conn


def send_eingabeschluss_reminders():
    """Sendet 3 Tage vor Eingabeschluss E-Mails an Partner mit offenen Verträgen.
    Wird beim Login getriggert. Versendet nur 1× pro Eingabeschluss-Periode."""
    if not is_smtp_configured():
        return 0

    deadlines = get_production_deadlines()
    if not deadlines or deadlines['eingabe_passed']:
        return 0

    # Trigger nur bei genau 3 Tagen vor Eingabeschluss
    if deadlines['eingabe_in_days'] != 3:
        return 0

    # Schon gesendet?
    period_key = f'reminder_3d_{deadlines["eingabeschluss"].strftime("%Y-%m")}'
    if get_setting(period_key) == '1':
        return 0

    db = get_db()
    # Alle Partner mit offenen Verträgen oder hängender Recherche
    rows = db.execute('''
        SELECT u.id, u.name, u.email,
               SUM(CASE WHEN c.status = 'offen' THEN 1 ELSE 0 END) as open_count,
               SUM(CASE WHEN c.recherche_status IN ('ausstehend','') AND c.einheiten > 0 THEN 1 ELSE 0 END) as pending_research
        FROM users u
        JOIN contracts c ON c.owner_id = u.id
        WHERE u.active = 1
          AND (c.status = 'offen' OR c.recherche_status IN ('ausstehend',''))
        GROUP BY u.id
        HAVING open_count > 0 OR pending_research > 0
    ''').fetchall()
    db.close()

    eingabe_str = deadlines['eingabeschluss'].strftime('%d.%m.%Y')
    weekday = deadlines['eingabe_weekday']
    sent = 0
    for r in rows:
        total = (r['open_count'] or 0) + (r['pending_research'] or 0)
        first_name = r['name'].split()[0] if r['name'] else ''
        subject = f'⏰ Nur noch 3 Tage bis Eingabeschluss — {total} Vertrag{"" if total == 1 else "äge"} klären!'
        text = f"""Hi {first_name},

in 3 Tagen ist Eingabeschluss ({weekday}, {eingabe_str}).

DU HAST AKTUELL:
• {r['open_count'] or 0} offene Verträge (noch nicht abgeschlossen)
• {r['pending_research'] or 0} Verträge mit hängender Recherche

Diese müssen bis zum Eingabeschluss FERTIG sein, sonst zählen die EH erst im nächsten Monat!

Was du jetzt machst:
1. Geh ins Pro Academy Control Hub
2. Klick auf "Verträge"
3. Setz alle offenen auf "abgeschlossen" + freigegebene Recherche

Wenn du Hilfe brauchst — meld dich bei deinem Upline.

Komm, das schaffst du! 🚀

Pro Academy"""

        html = f"""<!DOCTYPE html><html><body style="font-family:Inter,Arial,sans-serif;background:#f6f7fb;margin:0;padding:24px">
<table cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;background:#fff;border-radius:14px;border:1px solid #ebeef4;overflow:hidden">
<tr><td style="padding:36px 28px;background:linear-gradient(135deg,#dc2626 0%,#7c2d12 100%);text-align:center;color:#fff">
<div style="font-size:42px;margin-bottom:8px">⏰</div>
<div style="font-size:24px;font-weight:900;letter-spacing:-0.5px">Nur noch 3 Tage!</div>
<div style="font-size:14px;color:#fbbf24;margin-top:6px;font-weight:700">Eingabeschluss: {weekday} {eingabe_str}</div>
</td></tr>
<tr><td style="padding:32px 28px;color:#0f172a;line-height:1.6;font-size:15px">
<p>Hi <strong>{first_name}</strong>,</p>
<p>du hast aktuell <strong style="color:#dc2626">{total} Vertrag{"" if total == 1 else "äge"} offen</strong>, die bis zum Eingabeschluss fertig sein müssen — sonst zählen die EH erst nächsten Monat!</p>

<div style="background:#fef2f2;border-left:4px solid #dc2626;padding:18px 20px;margin:20px 0;border-radius:6px">
<div style="font-weight:800;color:#7f1d1d;font-size:13px;text-transform:uppercase;letter-spacing:0.7px;margin-bottom:8px">📋 Dein offenes Pensum</div>
<div style="font-size:14px;color:#0f172a">
• <strong>{r['open_count'] or 0}</strong> offene Verträge<br>
• <strong>{r['pending_research'] or 0}</strong> mit hängender Recherche
</div>
</div>

<p style="font-weight:700;margin-top:24px">Was du jetzt machst:</p>
<ol style="margin-left:20px;line-height:2;color:#0f172a;font-size:14px">
<li>Login: <a href="''' + CANONICAL_URL + '''" style="color:#b8902e;font-weight:600">''' + CANONICAL_HOST + '''</a></li>
<li>Klick auf <strong>„Verträge"</strong></li>
<li>Status auf <strong>„abgeschlossen"</strong> + Recherche auf <strong>„freigegeben"</strong></li>
</ol>

<p style="margin-top:24px">Komm, das schaffst du! 💪</p>
<p style="color:#64748b;font-size:13px">Coach</p>
</td></tr>
<tr><td style="padding:18px 28px;background:#fafbfc;color:#94a3b8;font-size:11px;border-top:1px solid #ebeef4">
Pro Academy · Automatische Erinnerung 3 Tage vor Produktionsschluss
</td></tr></table></body></html>"""

        ok, _ = send_email(r['email'], subject, text, body_html=html, sent_by=None, category='reminder')
        if ok:
            sent += 1

    set_setting(period_key, '1')
    log_activity(None, 'eingabe_reminder', f'⏰ Eingabeschluss-Reminder an {sent} Partner versendet', icon='⏰', color='red')
    return sent


def send_zvg_reminder():
    """Erinnert Admin nach Eingabeschluss an Zielvereinbarungsgespräche (ZVGs)."""
    deadlines = get_production_deadlines()
    if not deadlines:
        return 0
    # Trigger genau am Tag nach Eingabeschluss
    days_since = -deadlines['eingabe_in_days']  # Wenn -2 = vor 2 Tagen
    if days_since != 1:
        return 0
    period_key = f'zvg_reminder_{deadlines["eingabeschluss"].strftime("%Y-%m")}'
    if get_setting(period_key) == '1':
        return 0
    if not is_smtp_configured():
        set_setting(period_key, '1')
        return 0

    db = get_db()
    admin = db.execute("SELECT email, name FROM users WHERE role='admin' AND active=1 LIMIT 1").fetchone()
    db.close()
    if not admin:
        return 0

    text = f"""Hi {admin['name'].split()[0]},

der Eingabeschluss vom {deadlines['eingabeschluss_str']} ist durch.

JETZT IST ZEIT FÜR DIE ZVGs (Zielvereinbarungsgespräche):
• Mit jedem direkten Partner einzeln
• Ergebnis bewerten
• Ziele für nächsten Monat festlegen
• Konkrete nächste Schritte definieren

Dazu: alle 2 Wochen Kontrollgespräche planen.

Plan dir die Zeit ein — das ist DEIN Hebel als Direktionsrepräsentant.

Coach"""

    send_email(admin['email'], 'Zeit für ZVGs nach Produktionsschluss', text, sent_by=None, category='reminder')
    set_setting(period_key, '1')
    log_activity(None, 'zvg_reminder', '🎯 ZVG-Reminder an Admin versendet', icon='🎯', color='gold')
    return 1


def auto_backup_if_needed():
    """Erstellt täglich automatisch ein Backup der DB. Behält die letzten 14 Tage."""
    try:
        backup_dir = os.path.join(DATA_DIR, 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        today_str = date.today().strftime('%Y-%m-%d')
        backup_file = os.path.join(backup_dir, f'vertrieb-{today_str}.db')
        if not os.path.exists(backup_file) and os.path.exists(DB_PATH):
            import shutil
            shutil.copy2(DB_PATH, backup_file)
            # Cleanup: Backups älter als 14 Tage löschen
            cutoff = (date.today() - timedelta(days=14))
            for f in os.listdir(backup_dir):
                if f.startswith('vertrieb-') and f.endswith('.db'):
                    try:
                        f_date = datetime.strptime(f[9:19], '%Y-%m-%d').date()
                        if f_date < cutoff:
                            os.remove(os.path.join(backup_dir, f))
                    except (ValueError, IndexError):
                        pass
    except Exception as e:
        print(f"Auto-backup warning: {e}")


# === In-Memory Cache mit TTL ===
import time
import threading
import pickle
import hashlib
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_REFRESH_INFLIGHT = set()  # verhindert Mehrfach-Refreshes für gleichen Key

# Filesystem-Cache (L2) — cross-worker shared auf PA's Multi-Worker-Setup
# In-Memory ist worker-local → ohne FS hätten wir sporadische Cold-Hits
_FS_CACHE_DIR = '/tmp/proacademy-cache'
try:
    os.makedirs(_FS_CACHE_DIR, exist_ok=True)
except Exception:
    pass


def _fs_cache_path(key):
    h = hashlib.md5(key.encode()).hexdigest()
    return os.path.join(_FS_CACHE_DIR, h + '.pkl')


def _fs_cache_read(key):
    try:
        with open(_fs_cache_path(key), 'rb') as f:
            return pickle.load(f)
    except Exception:
        return None


def _fs_cache_write(key, entry):
    try:
        # Atomic write via temp + rename
        path = _fs_cache_path(key)
        tmp = path + f'.tmp.{os.getpid()}'
        with open(tmp, 'wb') as f:
            pickle.dump(entry, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except Exception:
        pass

def cache_get(key, allow_stale=False):
    """L1 (RAM) → L2 (FS, cross-worker) → None.
    allow_stale=True: returnt (value, is_stale) — auch expired bis stale_until."""
    now = time.time()
    # L1: RAM-Lookup
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    # L2-Fallback: Filesystem-Cache (cross-worker)
    if not entry:
        entry = _fs_cache_read(key)
        if entry:
            # Memory-Cache mit-bauen (für nächste Aufrufe in diesem Worker)
            with _CACHE_LOCK:
                _CACHE[key] = entry
    if not entry:
        return (None, False) if allow_stale else None
    is_fresh = now <= entry['expires']
    is_stale_ok = now <= entry.get('stale_until', entry['expires'])
    if not is_fresh and not is_stale_ok:
        with _CACHE_LOCK:
            _CACHE.pop(key, None)
        try: os.remove(_fs_cache_path(key))
        except Exception: pass
        return (None, False) if allow_stale else None
    if not is_fresh and not allow_stale:
        return None
    return (entry['value'], not is_fresh) if allow_stale else entry['value']


def cache_set(key, value, ttl_seconds=60, ttl=None, stale_extra=None):
    """Setzt in L1 (RAM) + L2 (FS, cross-worker).
    SWR: stale_extra = 2× TTL default.
    `_key` wird ins Entry eingebettet damit prefix-Invalidate die L2-Files
    auch findet die *andere* Worker geschrieben haben."""
    if ttl is not None:
        ttl_seconds = ttl
    if stale_extra is None:
        stale_extra = ttl_seconds * 2
    now = time.time()
    entry = {
        '_key': key,
        'value': value,
        'expires': now + ttl_seconds,
        'stale_until': now + ttl_seconds + stale_extra,
    }
    with _CACHE_LOCK:
        _CACHE[key] = entry
    _fs_cache_write(key, entry)


def cache_invalidate(prefix=None):
    """Löscht aus L1 + L2 — auch cross-worker via embedded _key in L2-Files.
    Ohne den FS-Scan würde Worker A Stränge invalidieren aber die L2-Pickle
    von Worker B blieb 30 Min liegen → User sieht stale Daten."""
    with _CACHE_LOCK:
        if prefix is None:
            _CACHE.clear()
            keys = []
        else:
            keys = [k for k in _CACHE.keys() if k.startswith(prefix)]
            for k in keys:
                del _CACHE[k]
    if prefix is None:
        try:
            for f in os.listdir(_FS_CACHE_DIR):
                if f.endswith('.pkl'):
                    try: os.remove(os.path.join(_FS_CACHE_DIR, f))
                    except Exception: pass
        except Exception:
            pass
    else:
        # 1) Bekannte Memory-Keys aus L2 löschen (cheap path)
        for k in keys:
            try: os.remove(_fs_cache_path(k))
            except Exception: pass
        # 2) Cross-worker: alle L2-Files scannen, _key prüfen, bei Match löschen.
        # Wird nur bei Writes aufgerufen — nicht hot path.
        try:
            for f in os.listdir(_FS_CACHE_DIR):
                if not f.endswith('.pkl'):
                    continue
                fp = os.path.join(_FS_CACHE_DIR, f)
                try:
                    with open(fp, 'rb') as fh:
                        e = pickle.load(fh)
                except Exception:
                    continue
                k2 = e.get('_key', '') if isinstance(e, dict) else ''
                if k2 and k2.startswith(prefix):
                    try: os.remove(fp)
                    except Exception: pass
        except Exception:
            pass


def cache_swr(key, fetch_fn, ttl=300):
    """Stale-While-Revalidate Helper: returnt sofort (auch stale), refresht im Background.
    fetch_fn() wird nur einmal gleichzeitig pro Key aufgerufen."""
    cached, is_stale = cache_get(key, allow_stale=True)
    # Cache-Hit (frisch oder stale)
    if cached is not None:
        if is_stale and key not in _CACHE_REFRESH_INFLIGHT:
            # Background-Refresh triggern
            with _CACHE_LOCK:
                _CACHE_REFRESH_INFLIGHT.add(key)
            def _bg():
                try:
                    fresh = fetch_fn()
                    cache_set(key, fresh, ttl=ttl)
                except Exception as e:
                    print(f'[swr-refresh] {key}: {e}')
                finally:
                    with _CACHE_LOCK:
                        _CACHE_REFRESH_INFLIGHT.discard(key)
            threading.Thread(target=_bg, daemon=True, name=f'swr-{key[:30]}').start()
        return cached
    # Cache komplett leer → synchron holen
    fresh = fetch_fn()
    cache_set(key, fresh, ttl=ttl)
    return fresh


def cached(key_fn, ttl=60):
    """Decorator für funktions-level Caching."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            key = key_fn(*args, **kwargs)
            v = cache_get(key)
            if v is not None:
                return v
            v = fn(*args, **kwargs)
            cache_set(key, v, ttl)
            return v
        wrapper._cache_key_fn = key_fn
        return wrapper
    return decorator


def hash_password(pw):
    """Modernes pbkdf2:sha256 Hashing (Industriestandard)."""
    return generate_password_hash(pw, method='pbkdf2:sha256:260000')


def verify_password(stored_hash, attempt):
    """Verifiziert Passwort. Backwards-kompatibel mit alten SHA256-Hashes."""
    if not stored_hash:
        return False
    if stored_hash.startswith(('pbkdf2:', 'scrypt:')):
        return check_password_hash(stored_hash, attempt)
    # Legacy: alter SHA256-Hash
    return stored_hash == hashlib.sha256(attempt.encode()).hexdigest()


def _slugify_name(name):
    """Macht aus 'Najib Tchatikpi' → 'najib-tchatikpi'.
    Behält nur a-z, 0-9, Bindestrich. Umlaute werden ersetzt."""
    import re
    s = (name or '').lower().strip()
    # Umlaute & Sonderzeichen
    s = s.replace('ä', 'ae').replace('ö', 'oe').replace('ü', 'ue').replace('ß', 'ss')
    s = s.replace('é', 'e').replace('è', 'e').replace('ê', 'e').replace('à', 'a').replace('ç', 'c')
    # Whitespace + Punkte zu Bindestrich
    s = re.sub(r'[\s._]+', '-', s)
    # Alles was nicht a-z0-9- ist → weg
    s = re.sub(r'[^a-z0-9-]', '', s)
    # Mehrfach-Bindestriche zusammenfassen
    s = re.sub(r'-+', '-', s)
    return s.strip('-')[:40] or 'partner'


def get_or_create_lead_token(user_id):
    """Liefert (oder erzeugt) einen sprechenden Lead-Token pro User.
    Format: name-slug (najib-tchatikpi). Auto-Upgrade alter Random-Tokens.
    Bei Kollision: -2, -3, etc."""
    db = get_db()
    row = db.execute('SELECT lead_token, name FROM users WHERE id=?', (user_id,)).fetchone()
    if not row:
        db.close()
        return f'u{user_id}'
    current_token = row['lead_token']
    name_slug = _slugify_name(row['name'])
    # Schon ein name-slug-Format (enthält Bindestrich oder mind. 11 Zeichen)? → behalten
    if current_token and ('-' in current_token or len(current_token) >= 11):
        db.close()
        return current_token
    # Kein Token ODER alter Random-Token → Slug-Token versuchen
    candidate = name_slug
    suffix = 1
    for _ in range(20):
        existing = db.execute('SELECT id FROM users WHERE lead_token=? AND id != ?', (candidate, user_id)).fetchone()
        if not existing:
            db.execute('UPDATE users SET lead_token=? WHERE id=?', (candidate, user_id))
            db.commit()
            db.close()
            return candidate
        suffix += 1
        candidate = f'{name_slug}-{suffix}'
    db.close()
    return current_token or f'u{user_id}'


def generate_random_password(length=10):
    """Sicheres Zufallspasswort für CSV-Import & Reset."""
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def log_activity(user_id, event_type, message, icon='•', color='navy', metadata=None):
    """Loggt eine Aktivität in den Activity-Stream."""
    try:
        db = get_db()
        db.execute('INSERT INTO activity_log (user_id, event_type, message, icon, color, metadata) VALUES (?, ?, ?, ?, ?, ?)',
                   (user_id, event_type, message, icon, color, json.dumps(metadata) if metadata else None))
        db.commit()
        db.close()
    except Exception as e:
        print(f"Activity log warning: {e}")


def get_week_start(d=None):
    """Liefert den Montag der aktuellen Woche im Format YYYY-MM-DD."""
    if d is None:
        d = date.today()
    return (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')


def days_until_birthday(birthday_str):
    """Tage bis zum nächsten Geburtstag. None wenn kein Geburtstag."""
    if not birthday_str:
        return None
    try:
        # Akzeptiert YYYY-MM-DD oder MM-DD
        if len(birthday_str) >= 10:
            bd = datetime.strptime(birthday_str[:10], '%Y-%m-%d').date()
        else:
            bd = datetime.strptime(birthday_str[:5], '%m-%d').date()
        today = date.today()
        next_bd = bd.replace(year=today.year)
        if next_bd < today:
            next_bd = next_bd.replace(year=today.year + 1)
        return (next_bd - today).days
    except (ValueError, TypeError):
        return None


def calculate_age(birthday_str):
    """Aktuelles Alter (für nächsten Geburtstag)."""
    if not birthday_str or len(birthday_str) < 10:
        return None
    try:
        bd = datetime.strptime(birthday_str[:10], '%Y-%m-%d').date()
        today = date.today()
        age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
        return age
    except (ValueError, TypeError):
        return None


# === SETTINGS (Key/Value-Store für SMTP & Co.) ===
def get_setting(key, default=''):
    db = get_db()
    row = db.execute('SELECT value FROM app_settings WHERE key = ?', (key,)).fetchone()
    db.close()
    return row['value'] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute('INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
    db.commit()
    db.close()


def is_smtp_configured():
    return all([get_setting('smtp_host'), get_setting('smtp_port'),
                get_setting('smtp_user'), get_setting('smtp_password')])


# === E-MAIL VERSAND ===
MAIL_CATEGORIES_DEFAULT = 'signup,password_reset,admin_test,login_link'
MAIL_CATEGORY_LABELS = {
    'signup': 'Anmeldung',
    'password_init': 'Passwortvergabe',
    'password_reset': 'Passwort vergessen',
    'admin_test': 'Test-Mail (Admin)',
    'reminder': 'Erinnerung (deaktiviert)',
    'termin': 'Termin-Bestätigung (deaktiviert)',
    'admin_broadcast': 'Admin-Broadcast (deaktiviert)',
    'other': 'Sonstige (deaktiviert)',
}

def _mail_category_allowed(category):
    raw = get_setting('mail_categories_allowed', MAIL_CATEGORIES_DEFAULT)
    allowed = {c.strip() for c in (raw or '').split(',') if c.strip()}
    return (category or 'other') in allowed


def send_email(to, subject, body_text, body_html=None, sent_by=None, reply_to=None, bcc=None, category='other'):
    """Sendet E-Mail über konfigurierten SMTP. Returns (ok, error_msg).
    reply_to: optional Reply-To-Adresse · bcc: zusätzlicher BCC-Empfänger.
    category: filterbar via app_settings.mail_categories_allowed.
    Default-Whitelist: signup, password_init, password_reset, admin_test —
    alles andere wird blockiert (in email_log status='blocked' geloggt)."""
    smtp_host = get_setting('smtp_host')
    smtp_port = int(get_setting('smtp_port', '587'))
    smtp_user = get_setting('smtp_user')
    smtp_password = get_setting('smtp_password')
    sender_name = get_setting('smtp_from_name', 'Pro Academy')
    sender_email = get_setting('smtp_from_email', smtp_user)

    # Kategorie-Whitelist: nicht-whitelisted Mails werden blockiert
    if not _mail_category_allowed(category):
        try:
            db = get_db()
            db.execute('INSERT INTO email_log (sent_by, recipient, subject, status, error) VALUES (?, ?, ?, ?, ?)',
                       (sent_by, to, f'[BLOCKED:{category}] {subject}', 'blocked', f'Kategorie "{category}" nicht in mail_categories_allowed'))
            db.commit()
            db.close()
        except Exception:
            pass
        return False, f'Kategorie "{category}" deaktiviert (Whitelist: {get_setting("mail_categories_allowed", MAIL_CATEGORIES_DEFAULT)})'

    if not all([smtp_host, smtp_user, smtp_password]):
        return False, 'SMTP nicht konfiguriert. Geh zu Einstellungen → E-Mail-Versand.'

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = formataddr((sender_name, sender_email))
    msg['To'] = to
    if reply_to:
        msg['Reply-To'] = reply_to
    if bcc:
        msg['Bcc'] = bcc
    msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
    if body_html:
        msg.attach(MIMEText(body_html, 'html', 'utf-8'))

    try:
        if smtp_port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=15) as server:
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)

        # Log success
        db = get_db()
        db.execute('INSERT INTO email_log (sent_by, recipient, subject, status) VALUES (?, ?, ?, ?)',
                   (sent_by, to, subject, 'ok'))
        db.commit()
        db.close()
        return True, None

    except Exception as e:
        err = str(e)[:300]
        db = get_db()
        db.execute('INSERT INTO email_log (sent_by, recipient, subject, status, error) VALUES (?, ?, ?, ?, ?)',
                   (sent_by, to, subject, 'fail', err))
        db.commit()
        db.close()
        return False, err


# === ANTHROPIC CLAUDE API (echte KI) ===
def is_ai_configured():
    return bool(get_setting('anthropic_api_key'))


def claude_chat(prompt, system_prompt=None, max_tokens=1024, model='claude-sonnet-4-5-20250929'):
    """Echter Claude-API-Call. Returns (text, error)."""
    api_key = get_setting('anthropic_api_key')
    if not api_key:
        return None, 'Anthropic API-Key nicht konfiguriert'

    try:
        import urllib.request
        import urllib.error
        body = {
            'model': model,
            'max_tokens': max_tokens,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        if system_prompt:
            body['system'] = system_prompt
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps(body).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01'
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        text = data.get('content', [{}])[0].get('text', '')
        return text.strip(), None
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode('utf-8')
        except Exception:
            err_body = str(e)
        return None, f'HTTP {e.code}: {err_body[:200]}'
    except Exception as e:
        return None, f'API-Fehler: {str(e)[:200]}'


def build_user_context(user_id):
    """Baut einen kompakten Daten-Kontext über den User für KI-Chat."""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        db.close()
        return None

    own_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (user_id,)).fetchone()['s']
    week_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben" AND date(abschluss_date) >= date("now","-7 days")', (user_id,)).fetchone()['s']
    contracts = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (user_id,)).fetchone()['c']
    pending_recherche = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND recherche_status IN ("ausstehend","")', (user_id,)).fetchone()['c']
    open_contracts = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND status="offen"', (user_id,)).fetchone()['c']
    leads = db.execute('SELECT COUNT(*) as c FROM leads WHERE owner_id=?', (user_id,)).fetchone()['c']
    open_appts = db.execute('SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND status="geplant"', (user_id,)).fetchone()['c']
    direct_partners = db.execute('SELECT COUNT(*) as c FROM users WHERE parent_id=? AND active=1', (user_id,)).fetchone()['c']

    descendants = get_all_descendants(user_id)
    team_size = len(descendants)
    if descendants:
        ph = ','.join('?' * len(descendants))
        team_eh = db.execute(f'SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id IN ({ph}) AND status="abgeschlossen" AND recherche_status="freigegeben"', descendants).fetchone()['s']
        inactive_partners = db.execute(f'SELECT COUNT(*) as c FROM users WHERE id IN ({ph}) AND active=1 AND (last_login IS NULL OR last_login < datetime("now","-7 days"))', descendants).fetchone()['c']
    else:
        team_eh = 0
        inactive_partners = 0

    db.close()

    # Wer hat HEUTE nichts gemacht (max 8 Namen für Prompt-Kompaktheit)
    inactive_today = get_inactive_team_members(user_id, days=1, scope='all')
    inactive_today_names = ', '.join([f"{u['name']} ({u['days_inactive']}T)" for u in inactive_today[:8]]) or 'niemand — alle waren aktiv'
    # Wer ist 3+ Tage still
    inactive_3d = [u for u in inactive_today if u['days_inactive'] >= 3]
    inactive_3d_names = ', '.join([f"{u['name']} ({u['days_inactive']}T)" for u in inactive_3d[:8]]) or '–'
    career = career_for_row(user['manual_career_level'], own_eh)
    next_lvl = next((c for c in CAREER_LEVELS if c['level'] == career['level'] + 1), None)
    eh_to_go = max(0, next_lvl['min_eh'] - own_eh) if next_lvl else 0

    deadlines = get_production_deadlines()

    return {
        'name': user['name'],
        'role': user['role'],
        'career': f"{career['short']} ({career['name']})",
        'next_level': f"{next_lvl['short']} (in {int(eh_to_go)} EH)" if next_lvl else 'Höchste Stufe',
        'own_eh': int(own_eh),
        'week_eh': int(week_eh),
        'contracts': contracts,
        'pending_recherche': pending_recherche,
        'open_contracts': open_contracts,
        'leads': leads,
        'open_appointments': open_appts,
        'direct_partners': direct_partners,
        'team_size': team_size,
        'team_eh': int(team_eh),
        'inactive_partners': inactive_partners,
        'inactive_today_count': len(inactive_today),
        'inactive_today_names': inactive_today_names,
        'inactive_3d_count': len(inactive_3d),
        'inactive_3d_names': inactive_3d_names,
        'vision': user['vision'] or '–',
        'eingabeschluss': deadlines['eingabeschluss_str'] + ' (in ' + str(deadlines['eingabe_in_days']) + ' Tagen)' if deadlines else '–',
        'grundseminar': deadlines['grundseminar_str'] if deadlines else '–',
    }


# ═══════════ CLAUDE TOOL-USE: definitionen + executor ═══════════

CLAUDE_TOOLS = [
    {
        'name': 'list_my_leads',
        'description': 'Listet die Leads/Namensliste des aktuellen Users. Filterbar nach typ (vk=Verkauf, rk=Recruiting, all) und status (neu/kontakt/angebot/gewonnen/verloren).',
        'input_schema': {
            'type': 'object',
            'properties': {
                'typ': {'type': 'string', 'enum': ['vk', 'rk', 'all'], 'description': 'Liste-Typ'},
                'status': {'type': 'string', 'description': 'Status-Filter (optional)'},
                'limit': {'type': 'integer', 'default': 20},
            }
        }
    },
    {
        'name': 'create_lead',
        'description': 'Legt einen neuen Lead an für den aktuellen User. Pflicht: name. Optional: phone, email, liste_typ, notizen.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string'},
                'phone': {'type': 'string'},
                'email': {'type': 'string'},
                'liste_typ': {'type': 'string', 'enum': ['vk', 'rk'], 'default': 'vk'},
                'notizen': {'type': 'string'},
            },
            'required': ['name']
        }
    },
    {
        'name': 'update_lead_status',
        'description': 'Ändert den Status eines bestehenden Leads (neu, kontakt, angebot, gewonnen, verloren).',
        'input_schema': {
            'type': 'object',
            'properties': {
                'lead_id': {'type': 'integer'},
                'new_status': {'type': 'string'},
            },
            'required': ['lead_id', 'new_status']
        }
    },
    {
        'name': 'list_my_termine',
        'description': 'Listet die nächsten Termine des aktuellen Users (default nächste 14 Tage).',
        'input_schema': {
            'type': 'object',
            'properties': {
                'days_ahead': {'type': 'integer', 'default': 14},
                'limit': {'type': 'integer', 'default': 20},
            }
        }
    },
    {
        'name': 'create_termin',
        'description': 'Legt einen neuen Termin für den aktuellen User an. Pflicht: title, termin_date (YYYY-MM-DD).',
        'input_schema': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string'},
                'termin_date': {'type': 'string', 'description': 'ISO YYYY-MM-DD'},
                'termin_time': {'type': 'string', 'description': 'HH:MM (optional)'},
                'duration_min': {'type': 'integer', 'default': 60},
                'typ': {'type': 'string', 'default': 'kundentermin'},
                'client_name': {'type': 'string'},
                'notizen': {'type': 'string'},
            },
            'required': ['title', 'termin_date']
        }
    },
    {
        'name': 'get_my_kpis',
        'description': 'Liefert wichtige KPIs des aktuellen Users: own_eh, team_eh, woche_eh, # Leads, # offene Termine, # offene Verträge, fehlend zur nächsten Stufe.',
        'input_schema': {'type': 'object', 'properties': {}}
    },
    {
        'name': 'list_inactive_partners',
        'description': 'Listet Geschäftspartner in der eigenen Downline die >N Tage inaktiv sind.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'min_days_inactive': {'type': 'integer', 'default': 7},
                'limit': {'type': 'integer', 'default': 10},
            }
        }
    },
    {
        'name': 'send_inbox_to_user',
        'description': 'Schickt eine In-App-Notification (Push) an einen User in der eigenen Downline. Verwendet kein E-Mail.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'target_user_id': {'type': 'integer'},
                'title': {'type': 'string'},
                'body': {'type': 'string'},
            },
            'required': ['target_user_id', 'title', 'body']
        }
    },
]


def execute_claude_tool(tool_name, tool_input, user):
    """Führt einen Tool-Call aus. user = Flask current_user. Returns dict (JSON-serialisierbar)."""
    try:
        db = get_db()
        if tool_name == 'list_my_leads':
            typ = tool_input.get('typ', 'all')
            status = tool_input.get('status')
            limit = min(int(tool_input.get('limit', 20)), 50)
            sql = "SELECT id, name, phone, email, status, liste_typ, kontaktiert_at FROM leads WHERE owner_id=?"
            args = [user.id]
            if typ in ('vk', 'rk'):
                sql += " AND COALESCE(liste_typ,'vk')=?"
                args.append(typ)
            if status:
                sql += " AND status=?"
                args.append(status)
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(limit)
            rows = db.execute(sql, args).fetchall()
            db.close()
            return {'leads': [dict(r) for r in rows], 'count': len(rows)}

        if tool_name == 'create_lead':
            name = (tool_input.get('name') or '').strip()
            if not name:
                db.close()
                return {'error': 'Name fehlt'}
            cur = db.execute(
                'INSERT INTO leads (owner_id, name, phone, email, liste_typ, notizen, status) VALUES (?,?,?,?,?,?,?)',
                (user.id, name[:200],
                 (tool_input.get('phone') or '')[:50] or None,
                 (tool_input.get('email') or '')[:200] or None,
                 tool_input.get('liste_typ', 'vk'),
                 (tool_input.get('notizen') or '')[:500] or None,
                 'neu'))
            new_id = cur.lastrowid
            db.commit()
            db.close()
            cache_invalidate(f'ctx:career:{user.id}')
            return {'success': True, 'lead_id': new_id, 'message': f'Lead „{name}" angelegt (id={new_id})'}

        if tool_name == 'update_lead_status':
            lid = int(tool_input.get('lead_id', 0))
            new_status = (tool_input.get('new_status') or '').strip()
            if not lid or not new_status:
                db.close()
                return {'error': 'lead_id + new_status erforderlich'}
            row = db.execute('SELECT id FROM leads WHERE id=? AND owner_id=?', (lid, user.id)).fetchone()
            if not row:
                db.close()
                return {'error': f'Lead {lid} gehört nicht dir oder existiert nicht'}
            db.execute('UPDATE leads SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (new_status, lid))
            db.commit()
            db.close()
            return {'success': True, 'message': f'Lead {lid} → status „{new_status}"'}

        if tool_name == 'list_my_termine':
            days = min(int(tool_input.get('days_ahead', 14)), 90)
            limit = min(int(tool_input.get('limit', 20)), 50)
            today_iso = date.today().isoformat()
            end_iso = (date.today() + timedelta(days=days)).isoformat()
            rows = db.execute('''SELECT id, title, client_name, termin_date, termin_time, status, typ
                                 FROM appointments WHERE owner_id=? AND date(termin_date) BETWEEN date(?) AND date(?)
                                 ORDER BY termin_date, termin_time LIMIT ?''',
                            (user.id, today_iso, end_iso, limit)).fetchall()
            db.close()
            return {'termine': [dict(r) for r in rows], 'count': len(rows)}

        if tool_name == 'create_termin':
            title = (tool_input.get('title') or '').strip()
            tdate = (tool_input.get('termin_date') or '').strip()
            if not title or not tdate:
                db.close()
                return {'error': 'title + termin_date erforderlich'}
            cur = db.execute('''INSERT INTO appointments
                (owner_id, title, client_name, termin_date, termin_time, typ, status, notizen, duration_min)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (user.id, title[:200],
                 (tool_input.get('client_name') or '')[:200] or None,
                 tdate, tool_input.get('termin_time') or None,
                 tool_input.get('typ', 'kundentermin'), 'geplant',
                 (tool_input.get('notizen') or '')[:500] or None,
                 int(tool_input.get('duration_min', 60))))
            tid = cur.lastrowid
            db.commit()
            db.close()
            return {'success': True, 'termin_id': tid, 'message': f'Termin „{title}" am {tdate} angelegt'}

        if tool_name == 'get_my_kpis':
            own_eh = db.execute("SELECT COALESCE(SUM(einheiten),0) s FROM contracts WHERE owner_id=? AND status='abgeschlossen' AND recherche_status='freigegeben'",
                              (user.id,)).fetchone()['s'] or 0
            initial = db.execute('SELECT COALESCE(initial_eh,0) s FROM users WHERE id=?', (user.id,)).fetchone()['s']
            own_eh += initial or 0
            ids = [user.id] + get_all_descendants(user.id)
            ph = ','.join('?' * len(ids))
            team_eh = db.execute(f"SELECT COALESCE(SUM(einheiten),0) s FROM contracts WHERE owner_id IN ({ph}) AND status='abgeschlossen' AND recherche_status='freigegeben'",
                                ids).fetchone()['s'] or 0
            week_eh = db.execute("SELECT COALESCE(SUM(einheiten),0) s FROM contracts WHERE owner_id=? AND status='abgeschlossen' AND date(abschluss_date)>=date('now','-7 days')",
                               (user.id,)).fetchone()['s'] or 0
            n_leads = db.execute('SELECT COUNT(*) c FROM leads WHERE owner_id=?', (user.id,)).fetchone()['c']
            n_open_termine = db.execute("SELECT COUNT(*) c FROM appointments WHERE owner_id=? AND status='geplant' AND date(termin_date)>=date('now')",
                                       (user.id,)).fetchone()['c']
            n_open_contracts = db.execute("SELECT COUNT(*) c FROM contracts WHERE owner_id=? AND status='abgeschlossen' AND COALESCE(recherche_status,'')!='freigegeben'",
                                         (user.id,)).fetchone()['c']
            db.close()
            # Nächste Stufe heuristisch
            targets = [(3000, 'LREP'), (12500, 'HREP'), (25000, 'DREP'), (50000, 'GREP')]
            next_lvl, eh_to_next = None, 0
            for cap, lbl in targets:
                if own_eh < cap:
                    next_lvl, eh_to_next = lbl, cap - own_eh
                    break
            return {
                'own_eh': int(own_eh), 'team_eh': int(team_eh), 'week_eh': int(week_eh),
                'leads_count': n_leads, 'offene_termine': n_open_termine, 'offene_vertraege': n_open_contracts,
                'next_stufe': next_lvl, 'eh_bis_naechste_stufe': int(eh_to_next),
            }

        if tool_name == 'list_inactive_partners':
            min_days = max(1, int(tool_input.get('min_days_inactive', 7)))
            limit = min(int(tool_input.get('limit', 10)), 30)
            ids = get_all_descendants(user.id)
            if not ids:
                db.close()
                return {'inactive': [], 'count': 0}
            ph = ','.join('?' * len(ids))
            rows = db.execute(f'''SELECT id, name, phone, email,
                                  CAST(julianday('now') - julianday(COALESCE(last_login, joined_date)) as INTEGER) as days_inactive
                                  FROM users WHERE id IN ({ph}) AND active=1
                                  HAVING days_inactive >= ?
                                  ORDER BY days_inactive DESC LIMIT ?''',
                            ids + [min_days, limit]).fetchall()
            db.close()
            return {'inactive': [dict(r) for r in rows], 'count': len(rows)}

        if tool_name == 'send_inbox_to_user':
            tid = int(tool_input.get('target_user_id', 0))
            title = (tool_input.get('title') or '').strip()
            body = (tool_input.get('body') or '').strip()
            if not tid or not title:
                db.close()
                return {'error': 'target_user_id + title erforderlich'}
            # Berechtigung: nur eigene Downline
            if tid != user.id and tid not in get_all_descendants(user.id):
                db.close()
                return {'error': f'User {tid} ist nicht in deiner Downline'}
            db.close()
            ok = False
            try:
                ok = send_push_to_user(tid, title=title[:120], body=body[:280],
                                       url='/inbox', push_type='admin_alert',
                                       tag=f'coach-{int(time.time())}')
            except Exception as e:
                return {'error': f'Push-Fehler: {e}'}
            return {'success': bool(ok), 'message': f'Push an User {tid} versendet' if ok else 'Push fail (keine Subscription?)'}

        db.close()
        return {'error': f'Unbekanntes Tool: {tool_name}'}
    except Exception as e:
        try: db.close()
        except: pass
        return {'error': f'Tool-Exception: {str(e)[:200]}'}


def claude_chat_with_tools(messages, system_prompt, user, max_iter=4):
    """Tool-Use-Loop. Returns (final_text, tool_log, error)."""
    api_key = get_setting('anthropic_api_key')
    if not api_key:
        return None, [], 'Anthropic API-Key fehlt'
    tool_log = []
    msgs = list(messages)
    for _ in range(max_iter):
        body = {
            'model': 'claude-sonnet-4-5-20250929',
            'max_tokens': 1500,
            'system': system_prompt,
            'tools': CLAUDE_TOOLS,
            'messages': msgs,
        }
        try:
            import urllib.request, urllib.error
            req = urllib.request.Request(
                'https://api.anthropic.com/v1/messages',
                data=json.dumps(body).encode('utf-8'),
                headers={'Content-Type': 'application/json', 'x-api-key': api_key,
                         'anthropic-version': '2023-06-01'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try: err_body = e.read().decode('utf-8')
            except: err_body = str(e)
            return None, tool_log, f'HTTP {e.code}: {err_body[:200]}'
        except Exception as e:
            return None, tool_log, f'API-Fehler: {str(e)[:200]}'

        stop_reason = data.get('stop_reason', '')
        content_blocks = data.get('content', [])

        if stop_reason == 'tool_use':
            # Sammle Tool-Use-Blocks + execute
            tool_results = []
            for b in content_blocks:
                if b.get('type') == 'tool_use':
                    tname = b.get('name')
                    tinput = b.get('input', {})
                    result = execute_claude_tool(tname, tinput, user)
                    tool_log.append({'name': tname, 'input': tinput, 'result': result})
                    tool_results.append({
                        'type': 'tool_result',
                        'tool_use_id': b.get('id'),
                        'content': json.dumps(result, ensure_ascii=False)[:3000],
                    })
            msgs.append({'role': 'assistant', 'content': content_blocks})
            msgs.append({'role': 'user', 'content': tool_results})
            continue
        # End: Final text
        final_text = ''
        for b in content_blocks:
            if b.get('type') == 'text':
                final_text += b.get('text', '')
        return final_text.strip(), tool_log, None
    return None, tool_log, 'Tool-Loop max iterations erreicht'


def chat_with_assistant(user_id, user_message):
    """Sendet Nachricht an den KI-Assistenten und bekommt Antwort.
    Nutzt Claude API + speichert Verlauf."""
    if not is_ai_configured():
        return None, 'KI ist nicht konfiguriert. Admin muss API-Key eintragen.'

    db = get_db()
    # Speichere User-Message
    db.execute('INSERT INTO chat_messages (user_id, role, content) VALUES (?, ?, ?)',
               (user_id, 'user', user_message))
    db.commit()

    # Hole letzte 10 Messages als Verlauf
    history_rows = db.execute(
        'SELECT role, content FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 10',
        (user_id,)
    ).fetchall()
    history = list(reversed([{'role': r['role'], 'content': r['content']} for r in history_rows]))
    db.close()

    # Kontext über den User aufbauen
    ctx = build_user_context(user_id)
    if not ctx:
        return None, 'User-Daten nicht gefunden.'

    system_prompt = f"""Du bist Coach — ein KI-Assistent für Strukturvertrieb-Profis bei Pro Academy.

DEINE PERSÖNLICHKEIT:
- Du sprichst direkt, klar, motivierend — wie ein erfahrener Mentor mit Vertriebs-Erfahrung
- Du bist der "kranke Assistent": ehrlich, smart, manchmal frech, aber immer hilfreich
- Du machst aktiv Vorschläge: wen anrufen, was angehen, wo der Fokus liegt
- KURZ und PRÄZISE — keine langen Vorträge. Max 4-5 Sätze pro Antwort.
- Du sprichst Deutsch und duzt den User

DEIN JOB:
- Hilfst dem User Entscheidungen zu treffen
- Erinnerst an wichtige Sachen (ZVGs, Eingabeschluss, Grundseminar)
- Schlägst konkrete Aktionen vor
- Bist NICHT übervorsichtig — sag direkt was zu tun ist

AKTUELLER STAND VON {ctx['name']} ({ctx['role']}):
- Karriere-Stufe: {ctx['career']}
- Nächste Stufe: {ctx['next_level']}
- Eigene EH: {ctx['own_eh']} | Team-EH: {ctx['team_eh']} | Diese Woche: {ctx['week_eh']} EH
- Verträge gesamt: {ctx['contracts']} | Hängende Recherchen: {ctx['pending_recherche']} | Offene: {ctx['open_contracts']}
- Leads/Namensliste: {ctx['leads']} | Geplante Termine: {ctx['open_appointments']}
- Direkte Partner: {ctx['direct_partners']} | Team-Größe gesamt: {ctx['team_size']}
- Inaktive Partner (>7 Tage): {ctx['inactive_partners']}
- Heute NICHT aktiv ({ctx['inactive_today_count']}): {ctx['inactive_today_names']}
- 3+ Tage still ({ctx['inactive_3d_count']}): {ctx['inactive_3d_names']}
- Vision: {ctx['vision']}

WENN DER USER FRAGT WER NICHTS GEMACHT HAT:
- Nutze die Liste oben ("Heute NICHT aktiv" oder "3+ Tage still")
- Nenne 3-5 Namen konkret, mit Tagen
- Schlag vor: "Ruf {{Name}} an — frag woran's hakt"
- Verlinke wenn passend: "Details unter /partner/<id> oder /team/inaktiv"

WENN DER USER NACH WACHSTUM FRAGT (mehr Geschäft, mehr Partner, mehr EH):
- Schlag konkrete Aktionen vor:
  → Namensliste ausbauen (Ziel: 100+ Kontakte, sonst keine stabile Pipeline)
  → 1h Cold-Calling-Block einplanen (Sonntag-Abend reicht für Wochen-Pipeline)
  → Eigene Struktur ausbauen (Empfehl-Geschäft hebelt EH × 5)
  → Closing-Script verbessern wenn viele Termine ohne Abschluss
  → Kontrollgespräche alle 2 Wochen mit jedem direkten Partner
  → Veranstaltungen besuchen / organisieren (Akquise + Network)
- Frag nach: "Was lief letzte Woche schlecht?" / "Wo siehst du dich in 6 Monaten?"
- Wenn der User Vorschläge hat (Veranstaltungen, Tools, Schulungen) — sag ihm:
  "Trag das in den Vorschlags-Bereich ein → /vorschlag — der Admin sieht's sofort"

PROAKTIV: Wenn EH-Forecast schwach aussieht oder Closing-Quote schlecht ist —
sag's direkt aus der Kontext-Daten oben.

WICHTIGE TERMINE:
- Eingabeschluss/Produktionsschluss: {ctx['eingabeschluss']}
- Grundseminar: {ctx['grundseminar']}

BESONDERS WICHTIG IM VERTRIEB:
- ZVG = Zielvereinbarungsgespräche (Admin macht die mit jedem Partner nach Produktionsschluss)
- Kontrollgespräche alle 2 Wochen mit jedem direkten Partner
- 3 Termine = 1 Abschluss (Faustregel)
- Differenzprovision: REP 5€/EH, LREP 9,50€, HREP 14€, CREP 18€, DREP 20,70€, GREP 23€

Antworte jetzt direkt auf die Frage des Users mit konkreten Tipps oder Aktionen.
"""

    # System-Prompt um Tool-Hinweis erweitern
    system_prompt += """\n\nDU HAST WERKZEUGE (Tools) zur Verfügung:
- list_my_leads — zeig die Namensliste
- create_lead — leg einen neuen Lead an
- update_lead_status — Status ändern
- list_my_termine — zeig die nächsten Termine
- create_termin — Termin anlegen
- get_my_kpis — eigene Zahlen abrufen
- list_inactive_partners — inaktive Partner finden
- send_inbox_to_user — Push an einen Downline-User schicken

Nutze die Tools wenn der User konkrete Aktionen will („leg Lead X an", „zeig meine Termine", „push an Niesa: …"). Bei reinen Fragen ohne Aktion antworte direkt ohne Tool.
Bestätige nach jedem Tool-Use kurz was du getan hast."""

    # Claude-Call mit Tool-Use-Loop (Mehrfach-Roundtrip wenn Claude Tools nutzt)
    text, tool_log, err = claude_chat_with_tools(history, system_prompt, current_user)
    if err:
        return None, err
    if not text:
        text = '(KI hat ohne Text-Antwort geantwortet)'
    if tool_log:
        # Optional: Tool-Calls als Suffix anhängen (sichtbar im Chat)
        actions = [f'• {t["name"]}({", ".join(f"{k}={v}" for k,v in (t["input"] or {}).items() if k in ("name","title","new_status","target_user_id"))})' for t in tool_log]
        text = text + '\n\n_Aktionen:_\n' + '\n'.join(actions)

    # Speichere Assistant-Antwort
    db = get_db()
    db.execute('INSERT INTO chat_messages (user_id, role, content) VALUES (?, ?, ?)',
               (user_id, 'assistant', text))
    db.commit()
    db.close()

    return text, None


def heuristic_weekly_briefing(user_id):
    """Generiert ein dynamisches Wochen-Briefing OHNE externe API.
    Datengetriebene Templates, fühlt sich an wie KI."""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        db.close()
        return None

    own_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (user_id,)).fetchone()['s']
    week_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben" AND date(abschluss_date) >= date("now", "-7 days")', (user_id,)).fetchone()['s']
    prev_week_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben" AND date(abschluss_date) BETWEEN date("now", "-14 days") AND date("now", "-8 days")', (user_id,)).fetchone()['s']
    week_termine = db.execute('SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND date(termin_date) >= date("now", "-7 days")', (user_id,)).fetchone()['c']
    week_leads = db.execute('SELECT COUNT(*) as c FROM leads WHERE owner_id=? AND date(created_at) >= date("now", "-7 days")', (user_id,)).fetchone()['c']
    new_partners = db.execute('SELECT COUNT(*) as c FROM users WHERE parent_id=? AND date(joined_date) >= date("now", "-7 days") AND active=1', (user_id,)).fetchone()['c']
    pending = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND recherche_status IN ("ausstehend","")', (user_id,)).fetchone()['c']
    db.close()

    career = career_for_row(user['manual_career_level'], own_eh)
    next_lvl = next((c for c in CAREER_LEVELS if c['level'] == career['level'] + 1), None)
    eh_to_go = max(0, next_lvl['min_eh'] - own_eh) if next_lvl else 0
    first_name = user['name'].split()[0]

    # === Dynamische Bausteine ===
    # 1. Begrüßung basierend auf Tageszeit + Wochenfortschritt
    h = datetime.now().hour
    weekday = datetime.now().weekday()  # 0 = Mo, 6 = So
    if weekday == 0 or weekday == 6:
        opener = f"Hi {first_name}, neue Woche, neue Chancen."
    elif weekday <= 2:
        opener = f"Hi {first_name}, du startest stark in die Woche."
    elif weekday == 3:
        opener = f"Hi {first_name}, Bergfest — wie läuft's bisher?"
    else:
        opener = f"Hi {first_name}, lass uns die Woche stark beenden."

    # 2. Performance-Analyse
    parts = [opener]
    if week_eh > 0:
        delta = week_eh - prev_week_eh
        if prev_week_eh > 0:
            pct = int((delta / prev_week_eh) * 100)
            if pct > 20:
                parts.append(f"Diese Woche {int(week_eh)} EH — das sind {pct}% mehr als letzte Woche, richtig stark! 🔥")
            elif pct < -20:
                parts.append(f"Diese Woche {int(week_eh)} EH — letzte Woche waren's {int(prev_week_eh)}, also {abs(pct)}% weniger. Was ist los?")
            else:
                parts.append(f"Diese Woche {int(week_eh)} EH — stabil zur Vorwoche ({int(prev_week_eh)}).")
        else:
            parts.append(f"Diese Woche {int(week_eh)} EH — sauber.")
    elif prev_week_eh > 0:
        parts.append(f"Diese Woche noch keine EH — letzte Woche waren's {int(prev_week_eh)}. Lass uns das ändern.")
    else:
        parts.append("Noch keine neuen EH diese Woche — Zeit für Action.")

    # 3. Aktivitäts-Check
    activity_msgs = []
    if week_termine == 0 and weekday >= 2:
        activity_msgs.append("Achtung: 0 Termine diese Woche — bei 3 Termine = 1 Abschluss kommt da nichts mehr.")
    elif week_termine >= 5:
        activity_msgs.append(f"{week_termine} Termine diese Woche — Top-Aktivität.")
    elif week_termine >= 3:
        activity_msgs.append(f"{week_termine} Termine — solide, aber mehr geht.")
    if week_leads >= 5:
        activity_msgs.append(f"{week_leads} neue Personen in der Namensliste — perfekte Pipeline.")
    elif week_leads == 0 and weekday >= 3:
        activity_msgs.append("Keine neuen Namensliste-Einträge diese Woche — pflege deine Pipeline.")
    if new_partners > 0:
        activity_msgs.append(f"🎉 {new_partners} neuer{'e' if new_partners > 1 else ''} Partner gewonnen — Strukturaufbau läuft!")
    if activity_msgs:
        parts.append(' '.join(activity_msgs))

    # 4. Karriere-Push
    if next_lvl:
        if eh_to_go <= 200:
            parts.append(f"⚡ {next_lvl['short']} ist GREIFBAR — nur noch {int(eh_to_go)} EH. Jetzt Vollgas, das machst du diese Woche!")
        elif eh_to_go <= 1000:
            parts.append(f"Auf zu {next_lvl['short']} — noch {int(eh_to_go)} EH. Bei {int(week_eh) if week_eh > 0 else 'Vollgas'} EH/Woche eine Frage von Wochen.")
        elif eh_to_go <= 5000:
            parts.append(f"Dein nächstes Ziel: {next_lvl['short']} ({int(eh_to_go)} EH übrig) — Schritt für Schritt.")
    else:
        parts.append("Du bist auf der höchsten Stufe — jetzt gilt: Vorbild sein, Team aufbauen.")

    # 5. Pending-Hinweis
    if pending >= 3:
        parts.append(f"⏳ Du hast {pending} Verträge mit hängender Recherche — bitte nachfassen, das sind verlorene EH.")
    elif pending > 0:
        parts.append(f"Tipp: {pending} Recherche{'n' if pending > 1 else ''} noch offen — kurz nachfragen.")

    # 6. Inaktive Partner — wichtig für Führungskräfte!
    try:
        inact = get_inactive_team_members(user_id, days=1, scope='all')
        if inact:
            top = inact[:3]
            names = ', '.join([f"{u['name']} ({u['days_inactive']}T)" for u in top])
            parts.append(f"⚠ Inaktiv: {names}{' und mehr' if len(inact) > 3 else ''}. Ruf sie an — woran hakt's?")
    except Exception:
        pass

    # 7. Vision-Reminder
    if user['vision'] and user['vision'].strip() and weekday in [0, 4]:
        vision_short = user['vision'][:80] + ('…' if len(user['vision']) > 80 else '')
        parts.append(f'Erinnere dich: „{vision_short}" — dafür machst du das hier.')

    # 7. Closing
    if week_eh > prev_week_eh and week_eh > 0:
        parts.append("Du bist in Form — bleib dran! 💪")
    elif weekday == 4:
        parts.append("Noch ein Tag — was schaffst du heute? 🎯")
    elif weekday == 5 or weekday == 6:
        parts.append("Plane die nächste Woche — wer wird angerufen?")
    else:
        parts.append("Ein Schritt nach dem anderen. Du machst das.")

    return ' '.join(parts)


def heuristic_coaching_diagnosis(target_user_id):
    """Datenbasierte Coaching-Diagnose pro Partner — ohne externe API."""
    db = get_db()
    target = db.execute('SELECT * FROM users WHERE id = ?', (target_user_id,)).fetchone()
    if not target:
        db.close()
        return None

    own_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (target_user_id,)).fetchone()['s']
    contracts = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (target_user_id,)).fetchone()['c']
    termine = db.execute('SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND status="erledigt"', (target_user_id,)).fetchone()['c']
    pending = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND recherche_status IN ("ausstehend","")', (target_user_id,)).fetchone()['c']
    leads = db.execute('SELECT COUNT(*) as c FROM leads WHERE owner_id=?', (target_user_id,)).fetchone()['c']
    last_login_days = db.execute('SELECT CAST(julianday("now") - julianday(COALESCE(last_login, joined_date)) as INTEGER) as d FROM users WHERE id=?', (target_user_id,)).fetchone()['d']
    db.close()

    avg_t = (termine / contracts) if contracts > 0 else 0
    career = career_for_row(target['manual_career_level'], own_eh)
    first_name = target['name'].split()[0]

    # Stärken identifizieren
    staerken = []
    if avg_t > 0 and avg_t <= 2.5:
        staerken.append(f"Termin-Conversion exzellent ({avg_t:.1f} Termine/Abschluss — Top!)")
    if leads >= 30:
        staerken.append(f"Sehr volle Namensliste ({leads} Kontakte)")
    if last_login_days <= 2:
        staerken.append("hohe Aktivität (täglich im System)")
    if career['level'] >= 3 and own_eh > 5000:
        staerken.append("solide EH-Basis")

    # Schwächen identifizieren
    schwaechen = []
    if avg_t > 4 and contracts > 1:
        schwaechen.append(f"schwache Termin-Conversion ({avg_t:.1f}/Abschluss — Ziel ist 3)")
    if pending >= 3:
        schwaechen.append(f"{pending} hängende Recherchen")
    if leads < 10:
        schwaechen.append(f"dünne Namensliste (nur {leads} Kontakte)")
    if last_login_days > 7:
        schwaechen.append(f"seit {last_login_days} Tagen nicht im System")
    if termine < 5 and contracts == 0:
        schwaechen.append("noch keine Aktivität in Termin-Pipeline")

    # Diagnose-Text
    if staerken and schwaechen:
        diagnose = f"{first_name} hat klare Stärken: {staerken[0]}. Aber: {schwaechen[0]}."
    elif staerken:
        diagnose = f"{first_name} läuft gut: {' und '.join(staerken[:2])}. Auf diesem Niveau halten."
    elif schwaechen:
        diagnose = f"{first_name} braucht Fokus: {' und '.join(schwaechen[:2])}."
    else:
        diagnose = f"{first_name} ist noch in Anlaufphase — Basics aufbauen."

    # Konkrete Aktion
    if pending >= 3:
        fokus = f"Diese Woche: alle {pending} hängenden Recherchen durchgehen und auf Stand bringen."
    elif avg_t > 4 and contracts > 0:
        fokus = "Vor dem nächsten Termin: 3-Fragen-Vorqualifikation üben (Bedarf, Budget, Entscheidung)."
    elif leads < 10:
        fokus = "Diese Woche: 20 Personen aus dem Umfeld in Namensliste eintragen."
    elif termine == 0:
        fokus = "Diese Woche mind. 3 Termine vereinbaren — alles andere kommt von dort."
    elif last_login_days > 7:
        fokus = "Tägliche 10-Minuten-Routine etablieren: Login, Pipeline pflegen, 1 Anruf."
    else:
        fokus = "Klare Wochenziele definieren und im System tracken."

    # Coaching-Frage
    if avg_t > 4:
        frage = f"Was glaubst du — was unterscheidet einen Termin der zum Abschluss führt von einem der nicht führt?"
    elif pending >= 3:
        frage = f"Was hält dich aktuell davon ab, die hängenden Themen abzuschließen?"
    elif leads < 10:
        frage = f"Wer aus deinem Umfeld kann konkret von Ergo Rente Chance profitieren — und warum?"
    elif career['level'] < 3 and own_eh < 1000:
        frage = f"Was ist dein realistisches Ziel für die nächsten 30 Tage und was brauchst du dafür?"
    else:
        frage = f"Wenn du in 6 Monaten zurückblickst — was muss passiert sein, damit du stolz bist?"

    return {
        'diagnose': diagnose,
        'fokus': fokus,
        'frage': frage,
        'staerken': staerken[:3],
        'schwaechen': schwaechen[:3]
    }


def ai_generate_weekly_briefing(user_id):
    """Generiert ein persönliches Wochen-Briefing.
    Nutzt Claude API wenn konfiguriert, sonst dynamische Heuristik (Top-Niveau)."""
    if not is_ai_configured():
        # Fallback: Heuristisches Briefing (fühlt sich wie KI an)
        return heuristic_weekly_briefing(user_id)
    cache_key = f'ai:briefing:{user_id}:{date.today().strftime("%Y-W%U")}'
    cached_val = cache_get(cache_key)
    if cached_val:
        return cached_val

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        db.close()
        return None

    own_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (user_id,)).fetchone()['s']
    week_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben" AND date(abschluss_date) >= date("now", "-7 days")', (user_id,)).fetchone()['s']
    week_termine = db.execute('SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND date(termin_date) >= date("now", "-7 days")', (user_id,)).fetchone()['c']
    new_partners = db.execute('SELECT COUNT(*) as c FROM users WHERE parent_id=? AND date(joined_date) >= date("now", "-7 days") AND active=1', (user_id,)).fetchone()['c']
    db.close()

    career = career_for_row(user['manual_career_level'], own_eh)
    next_lvl = next((c for c in CAREER_LEVELS if c['level'] == career['level'] + 1), None)
    eh_to_go = max(0, next_lvl['min_eh'] - own_eh) if next_lvl else 0

    system_prompt = """Du bist ein Top-Vertriebs-Mentor für Strukturvertrieb.
Du sprichst direkt, motivierend, ehrlich. Auf Deutsch.
Schreibe KURZ (max 5 Sätze), persönlich, wie ein echter Coach.
Keine Floskeln, keine Aufzählungen. Direkte Ansprache mit "du"."""

    prompt = f"""Generiere ein persönliches Wochen-Briefing für:

Name: {user['name']}
Stufe: {career['short']} ({career['name']})
Eigene EH gesamt: {int(own_eh)}
{'Nächste Stufe: ' + next_lvl['short'] + ' (noch ' + str(int(eh_to_go)) + ' EH)' if next_lvl else 'Höchste Stufe erreicht'}
Vision: {user['vision'] or 'noch nicht gesetzt'}

Diese Woche:
- Neue EH: {int(week_eh)}
- Termine: {week_termine}
- Neue direkte Partner: {new_partners}

Schreibe ein motivierendes 4-5 Sätze-Briefing, das auf seinen Fortschritt eingeht und konkret sagt, was er diese Woche fokussieren sollte."""

    text, err = claude_chat(prompt, system_prompt=system_prompt, max_tokens=400)
    if text:
        cache_set(cache_key, text, ttl=86400)  # 24h
    return text


def ai_coaching_advice(user_id, target_user_id):
    """Generiert spezifische Coaching-Empfehlung für 1-on-1 Gespräch mit Partner."""
    if not is_ai_configured():
        return None

    db = get_db()
    target = db.execute('SELECT * FROM users WHERE id = ?', (target_user_id,)).fetchone()
    if not target:
        db.close()
        return None

    own_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (target_user_id,)).fetchone()['s']
    contracts = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (target_user_id,)).fetchone()['c']
    termine = db.execute('SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND status="erledigt"', (target_user_id,)).fetchone()['c']
    pending = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND recherche_status IN ("ausstehend","")', (target_user_id,)).fetchone()['c']
    last_contracts = db.execute('SELECT client_name, einheiten FROM contracts WHERE owner_id=? ORDER BY created_at DESC LIMIT 3', (target_user_id,)).fetchall()
    db.close()

    career = career_for_row(target['manual_career_level'], own_eh)
    avg_t = (termine / contracts) if contracts > 0 else 0

    system_prompt = """Du bist ein Top-Strukturvertriebs-Coach.
Antworte im JSON-Format:
{"diagnose": "kurze Stärken/Schwächen-Analyse (max 2 Sätze)",
 "fokus": "EINE konkrete Action für diese Woche (max 1 Satz)",
 "frage": "EINE smarte Coaching-Frage für das Gespräch (max 1 Satz)"}"""

    prompt = f"""Analysiere {target['name']} ({career['short']}):
- Eigene EH: {int(own_eh)}, Verträge: {contracts}, erledigte Termine: {termine}
- Termine pro Abschluss: {avg_t:.1f} (Ziel: 3)
- Hängende Recherchen: {pending}
- Vision: {target['vision'] or 'keine'}
- Letzte Verträge: {', '.join([f'{r["client_name"]} ({int(r["einheiten"])} EH)' for r in last_contracts]) or 'keine'}

Liefere JSON mit diagnose, fokus, frage."""

    text, err = claude_chat(prompt, system_prompt=system_prompt, max_tokens=500)
    if not text:
        return None
    try:
        # Versuche JSON aus Antwort zu extrahieren
        start = text.find('{')
        end = text.rfind('}')
        if start >= 0 and end > start:
            return json.loads(text[start:end+1])
    except Exception:
        pass
    return {'diagnose': text, 'fokus': '', 'frage': ''}


def send_lead_confirmation_email(lead_email, lead_name):
    """Bestätigungsmail an Bewerber direkt nach Anmeldung über öffentliche Form."""
    if not is_smtp_configured():
        return False, 'SMTP nicht konfiguriert'

    first_name = lead_name.split()[0] if lead_name else ''
    subject = f'✅ Anmeldung erhalten — wir melden uns bei dir!'

    text = f"""Hi {first_name},

vielen Dank für deine Anmeldung bei Pro Academy! 🎉

Wir haben deine Daten erhalten und melden uns innerhalb von 24-48 Stunden bei dir.

In der Zwischenzeit:
• Schau dich auf unseren Social-Media-Kanälen um
• Bereite dir 2-3 Fragen vor, die du beim Erstgespräch stellen willst
• Lass dir Zeit, deine Ziele zu sortieren

Was dich bei uns erwartet:
• Klare Karrierewege (REP → GREP)
• Faire Differenz-Provisionen (5,00 - 23,00 €/EH)
• KI-Coach + Mentor-System
• Wachstum mit echtem Team

Bis ganz bald! 🚀

Team Pro Academy

---
Diese E-Mail wurde automatisch versendet, weil du dich bei uns angemeldet hast.
Wenn du das nicht warst, ignoriere bitte diese Nachricht."""

    html = f"""<!DOCTYPE html><html><body style="font-family:Inter,Arial,sans-serif;background:#f6f7fb;margin:0;padding:24px">
<table cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;background:#fff;border-radius:14px;border:1px solid #ebeef4;overflow:hidden">
<tr><td style="padding:40px 28px;background:linear-gradient(135deg,#0f1c3f 0%,#1a2c5b 100%);text-align:center">
<div style="font-size:48px;margin-bottom:12px">✅</div>
<div style="font-size:22px;font-weight:800;color:#fff;letter-spacing:-0.4px">Anmeldung erhalten!</div>
<div style="font-size:13px;color:#d4a843;letter-spacing:1.5px;text-transform:uppercase;margin-top:8px;font-weight:700">Pro Academy</div>
</td></tr>
<tr><td style="padding:32px 28px;color:#0f172a;line-height:1.6;font-size:15px">
<p>Hi <strong>{first_name}</strong>,</p>
<p>vielen Dank für deine Anmeldung! 🎉<br>
Wir haben deine Daten erhalten und melden uns <strong style="color:#b8902e">innerhalb von 24-48 Stunden</strong> bei dir.</p>

<div style="background:#faf6ec;border:1px solid #e8d59a;border-radius:12px;padding:18px 20px;margin:20px 0">
<div style="font-size:11px;color:#7a5c1a;text-transform:uppercase;letter-spacing:1px;font-weight:800;margin-bottom:12px">⏳ Was du in der Zwischenzeit tun kannst</div>
<ul style="margin-left:18px;color:#0f172a;font-size:14px;line-height:2">
<li>Schau dich auf unseren Social-Media-Kanälen um</li>
<li>Bereite 2-3 eigene Fragen für das Erstgespräch vor</li>
<li>Sortier deine Ziele für die nächsten 3-12 Monate</li>
</ul>
</div>

<div style="background:#fff;border:1px solid #ebeef4;border-radius:12px;padding:18px 20px;margin:20px 0">
<div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;font-weight:800;margin-bottom:12px">🚀 Was dich bei uns erwartet</div>
<table style="width:100%;font-size:13px;color:#0f172a;line-height:1.7">
<tr><td style="padding:4px 0">⬡ <strong>Klare Karriere-Wege</strong></td><td style="color:#64748b">REP → LREP → HREP → CREP → DREP → GREP</td></tr>
<tr><td style="padding:4px 0">💰 <strong>Faire Provisionen</strong></td><td style="color:#64748b">5,00 - 23,00 €/EH</td></tr>
<tr><td style="padding:4px 0">🧠 <strong>KI-Coach</strong></td><td style="color:#64748b">tägliche Empfehlungen</td></tr>
<tr><td style="padding:4px 0">👥 <strong>Echtes Team</strong></td><td style="color:#64748b">Mentor-System</td></tr>
</table>
</div>

<p>Bis ganz bald!</p>
<p style="color:#64748b;font-size:13px;margin-top:24px">Team Pro Academy 🚀</p>
</td></tr>
<tr><td style="padding:18px 28px;background:#fafbfc;color:#94a3b8;font-size:11px;border-top:1px solid #ebeef4;border-radius:0 0 14px 14px">
Diese E-Mail wurde automatisch versendet. Wenn du dich nicht angemeldet hast, ignoriere bitte diese Nachricht.<br>
Bei Fragen: einfach auf diese Mail antworten.
</td></tr></table></body></html>"""

    return send_email(lead_email, subject, text, body_html=html, sent_by=None, category='signup')


def send_welcome_email(user_email, user_name, password, sender_name='dein Upline'):
    """Sendet Welcome-E-Mail mit Login-Daten an neuen Partner."""
    if not is_smtp_configured():
        return False, 'SMTP nicht konfiguriert'

    subject = f'🎉 Willkommen bei Pro Academy, {user_name.split()[0]}!'
    base_url = get_setting('app_base_url', 'http://localhost:5001')

    text = f"""Hi {user_name.split()[0]},

willkommen im Team! 🚀

Du hast ab sofort Zugang zum Pro Academy Control Hub.

Deine Login-Daten:
🔑 E-Mail: {user_email}
🔐 Passwort: {password}

→ Login: {base_url}/login

WICHTIG: Beim ersten Login wirst du gebeten, ein neues Passwort zu setzen.

Was dich erwartet:
• KI-Coach mit personalisiertem Wochen-Briefing
• Karriere-Engine: tracking deiner Stufen-Reise (REP → GREP)
• Provisions-Übersicht (Differenz-System automatisch)
• Trophäen für deine Erfolge
• Onboarding-Wizard mit Sprachausgabe

Lass uns starten! 💪

{sender_name}"""

    html = f"""<!DOCTYPE html><html><body style="font-family:Inter,Arial,sans-serif;background:#f6f7fb;margin:0;padding:24px">
<table cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;background:#fff;border-radius:14px;border:1px solid #ebeef4;overflow:hidden">
<tr><td style="padding:36px 28px;background:linear-gradient(135deg,#0f1c3f 0%,#1a2c5b 100%);text-align:center">
<div style="font-size:36px;margin-bottom:8px">⚡</div>
<div style="font-size:22px;font-weight:800;color:#fff;letter-spacing:-0.4px">Willkommen, {user_name.split()[0]}!</div>
<div style="font-size:13px;color:#d4a843;letter-spacing:1.5px;text-transform:uppercase;margin-top:8px;font-weight:700">Pro Academy · Control Hub</div>
</td></tr>
<tr><td style="padding:32px 28px;color:#0f172a;line-height:1.6;font-size:15px">
<p>Hi <strong>{user_name.split()[0]}</strong>,</p>
<p>willkommen im Team! 🚀 Du hast ab sofort Zugang zum Pro Academy Control Hub.</p>

<div style="background:#faf6ec;border:1px solid #e8d59a;border-radius:12px;padding:20px;margin:20px 0">
<div style="font-size:11px;color:#7a5c1a;text-transform:uppercase;letter-spacing:1px;font-weight:800;margin-bottom:10px">🔑 Deine Login-Daten</div>
<div style="font-size:13px;color:#64748b">E-Mail:</div>
<div style="font-size:15px;font-weight:700;color:#0f172a;margin-bottom:10px">{user_email}</div>
<div style="font-size:13px;color:#64748b">Passwort:</div>
<div style="font-family:Menlo,Monaco,monospace;font-size:15px;font-weight:700;color:#b8902e;background:#fff;padding:8px 12px;border-radius:6px;display:inline-block">{password}</div>
</div>

<div style="text-align:center;margin:28px 0">
<a href="{base_url}/login" style="display:inline-block;background:#0f1c3f;color:#fff;text-decoration:none;padding:14px 32px;border-radius:10px;font-weight:700;font-size:14px">→ Jetzt einloggen</a>
</div>

<div style="background:#fef3e7;border-left:3px solid #b8590a;padding:12px 16px;border-radius:6px;font-size:13px;color:#7a3d0a;margin:20px 0">
<strong>⚠ WICHTIG:</strong> Beim ersten Login wirst du gebeten, ein neues sicheres Passwort zu setzen.
</div>

<p style="margin-top:24px;font-weight:700">Was dich erwartet:</p>
<ul style="margin-left:20px;line-height:2;color:#0f172a;font-size:14px">
<li>🧠 KI-Coach mit personalisiertem Wochen-Briefing</li>
<li>⬡ Karriere-Engine — REP → GREP tracking</li>
<li>💰 Provisions-Übersicht automatisch</li>
<li>🏆 Trophäen für deine Erfolge</li>
<li>🎤 Onboarding-Wizard mit Sprachausgabe</li>
</ul>
<p>Lass uns starten! 💪</p>
<p style="color:#64748b;font-size:13px">{sender_name}</p>
</td></tr>
<tr><td style="padding:18px 28px;background:#fafbfc;color:#94a3b8;font-size:11px;border-top:1px solid #ebeef4">
Pro Academy · Control Hub · Diese E-Mail wurde automatisch beim Anlegen deines Accounts versendet.
</td></tr></table></body></html>"""

    return send_email(user_email, subject, text, body_html=html, sent_by=None, category='password_init')


def send_bulk_emails(recipients, subject, body_text, body_html=None, sent_by=None, category='admin_broadcast'):
    """Sendet E-Mail an mehrere Empfänger. Returns (success_count, fail_list)."""
    success = 0
    fails = []
    for r in recipients:
        ok, err = send_email(r, subject, body_text, body_html, sent_by, category=category)
        if ok:
            success += 1
        else:
            fails.append({'email': r, 'error': err})
    return success, fails


def _easter_sunday(year):
    """Berechnet Ostersonntag (Gauß-Algorithmus)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def german_holidays(year):
    """Liefert deutsche bundesweite Feiertage für ein Jahr."""
    easter = _easter_sunday(year)
    return {
        date(year, 1, 1),                          # Neujahr
        easter - timedelta(days=2),                # Karfreitag
        easter + timedelta(days=1),                # Ostermontag
        date(year, 5, 1),                          # Tag der Arbeit
        easter + timedelta(days=39),               # Christi Himmelfahrt
        easter + timedelta(days=50),               # Pfingstmontag
        date(year, 10, 3),                         # Tag der Deutschen Einheit
        date(year, 12, 25),                        # 1. Weihnachtstag
        date(year, 12, 26),                        # 2. Weihnachtstag
    }


def get_third_workday(year, month):
    """Liefert den 3. Werktag (Mo-Fr ohne deutsche Feiertage) des Monats."""
    holidays = german_holidays(year)
    count = 0
    day = 1
    while day <= 31:
        try:
            d = date(year, month, day)
        except ValueError:
            break
        if d.weekday() < 5 and d not in holidays:  # Mo-Fr und kein Feiertag
            count += 1
            if count == 3:
                return d
        day += 1
    return None


def get_grundseminar_date(eingabeschluss):
    """Liefert das Grundseminar-Datum: 2. Samstag NACH dem Eingabeschluss."""
    if not eingabeschluss:
        return None
    # 1. Samstag nach Eingabeschluss
    days_until_sat = (5 - eingabeschluss.weekday()) % 7
    if days_until_sat == 0:
        days_until_sat = 7
    first_sat = eingabeschluss + timedelta(days=days_until_sat)
    # 2. Samstag = + 7 Tage
    return first_sat + timedelta(days=7)


def get_production_deadlines():
    """Cached für 1h — Deadlines ändern sich nur tageweise."""
    ckey = 'deadlines:global'
    cached = cache_get(ckey)
    if cached is not None: return cached
    result = _get_production_deadlines_uncached()
    cache_set(ckey, result, ttl=3600)
    return result


def _get_production_deadlines_uncached():
    """Liefert Eingabeschluss + Grundseminar — TAG-GENAU + UNABHÄNGIG voneinander."""
    today = date.today()
    monate = ['Januar','Februar','März','April','Mai','Juni','Juli','August','September','Oktober','November','Dezember']
    weekdays = ['Mo','Di','Mi','Do','Fr','Sa','So']

    # === EINGABESCHLUSS — wenn vorbei: nächster Monat ===
    eingabe = get_third_workday(today.year, today.month)
    if not eingabe or today > eingabe:
        # nächster Monat
        nm_y = today.year + 1 if today.month == 12 else today.year
        nm_m = 1 if today.month == 12 else today.month + 1
        eingabe = get_third_workday(nm_y, nm_m)

    # === GRUNDSEMINAR — wenn vorbei: nächster Monat ===
    seminar_this_month = get_grundseminar_date(get_third_workday(today.year, today.month))
    if seminar_this_month and today <= seminar_this_month:
        seminar = seminar_this_month
    else:
        nm_y = today.year + 1 if today.month == 12 else today.year
        nm_m = 1 if today.month == 12 else today.month + 1
        seminar = get_grundseminar_date(get_third_workday(nm_y, nm_m))

    if not eingabe or not seminar:
        return None

    eingabe_in_days = (eingabe - today).days
    seminar_in_days = (seminar - today).days

    return {
        'eingabeschluss': eingabe,
        'eingabeschluss_str': eingabe.strftime('%d.%m.%Y'),
        'eingabe_weekday': weekdays[eingabe.weekday()],
        'eingabe_in_days': eingabe_in_days,
        'eingabe_passed': eingabe_in_days < 0,
        'eingabe_today': eingabe_in_days == 0,
        'eingabe_urgent': 0 <= eingabe_in_days <= 3,
        'eingabe_month_label': monate[eingabe.month - 1],
        'grundseminar': seminar,
        'grundseminar_str': seminar.strftime('%d.%m.%Y'),
        'seminar_weekday': weekdays[seminar.weekday()],
        'seminar_in_days': seminar_in_days,
        'seminar_passed': seminar_in_days < 0,
        'seminar_today': seminar_in_days == 0,
        'seminar_urgent': 0 <= seminar_in_days <= 7,
        'seminar_month_label': monate[seminar.month - 1],
        'month_label': monate[eingabe.month - 1],  # backward-compat
    }


def get_period_stats(scope_user_id=None):
    """Cached für 30 Min."""
    ckey = f'period_stats:{scope_user_id or "global"}'
    cached = cache_get(ckey)
    if cached is not None: return cached
    result = _get_period_stats_uncached(scope_user_id)
    cache_set(ckey, result, ttl=1800)
    return result


def _get_period_stats_uncached(scope_user_id=None):
    """Liefert Monats- und Halbjahres-Statistiken für Header."""
    db = get_db()
    today = date.today()

    if scope_user_id:
        ids = [scope_user_id] + get_all_descendants(scope_user_id)
    else:
        ids = [r['id'] for r in db.execute('SELECT id FROM users WHERE active = 1').fetchall()]

    if not ids:
        db.close()
        return None
    ph = ','.join('?' * len(ids))

    # Monat
    cur_month = today.strftime('%Y-%m')
    monat_label = ['Januar','Februar','März','April','Mai','Juni','Juli','August','September','Oktober','November','Dezember'][today.month - 1]
    days_in_month = (date(today.year + (1 if today.month == 12 else 0),
                          1 if today.month == 12 else today.month + 1, 1) - timedelta(days=1)).day
    days_passed = today.day
    month_pct = round(days_passed / days_in_month * 100)

    monat_eh = db.execute(f'''SELECT COALESCE(SUM(einheiten),0) as s FROM contracts
                             WHERE owner_id IN ({ph}) AND status="abgeschlossen" AND recherche_status="freigegeben"
                             AND strftime("%Y-%m", abschluss_date)=?''', ids + [cur_month]).fetchone()['s']
    monat_vtr = db.execute(f'''SELECT COUNT(*) as c FROM contracts
                              WHERE owner_id IN ({ph}) AND status="abgeschlossen" AND recherche_status="freigegeben"
                              AND strftime("%Y-%m", abschluss_date)=?''', ids + [cur_month]).fetchone()['c']
    monat_partner = db.execute(f'''SELECT COUNT(*) as c FROM users
                                  WHERE id IN ({ph}) AND active=1
                                  AND strftime("%Y-%m", joined_date)=?''', ids + [cur_month]).fetchone()['c']

    # Halbjahr
    if today.month <= 6:
        h_num, h_start, h_end = 1, date(today.year, 1, 1), date(today.year, 6, 30)
    else:
        h_num, h_start, h_end = 2, date(today.year, 7, 1), date(today.year, 12, 31)
    h_label = f'H{h_num}/{today.year}'
    h_name = f'{h_num}. Halbjahr {today.year}'
    h_total_days = (h_end - h_start).days + 1
    h_passed_days = (today - h_start).days + 1
    h_pct = round(h_passed_days / h_total_days * 100)
    h_remaining = h_total_days - h_passed_days

    h_eh = db.execute(f'''SELECT COALESCE(SUM(einheiten),0) as s FROM contracts
                         WHERE owner_id IN ({ph}) AND status="abgeschlossen" AND recherche_status="freigegeben"
                         AND date(abschluss_date) BETWEEN ? AND ?''',
                      ids + [h_start.strftime('%Y-%m-%d'), h_end.strftime('%Y-%m-%d')]).fetchone()['s']
    h_vtr = db.execute(f'''SELECT COUNT(*) as c FROM contracts
                          WHERE owner_id IN ({ph}) AND status="abgeschlossen" AND recherche_status="freigegeben"
                          AND date(abschluss_date) BETWEEN ? AND ?''',
                       ids + [h_start.strftime('%Y-%m-%d'), h_end.strftime('%Y-%m-%d')]).fetchone()['c']
    h_partner = db.execute(f'''SELECT COUNT(*) as c FROM users
                              WHERE id IN ({ph}) AND active=1
                              AND date(joined_date) BETWEEN ? AND ?''',
                           ids + [h_start.strftime('%Y-%m-%d'), h_end.strftime('%Y-%m-%d')]).fetchone()['c']

    db.close()
    return {
        'monat_label': monat_label, 'monat_year': today.year, 'monat_pct': month_pct,
        'monat_days_passed': days_passed, 'monat_days_total': days_in_month,
        'monat_eh': monat_eh, 'monat_vtr': monat_vtr, 'monat_partner': monat_partner,
        'h_label': h_label, 'h_name': h_name, 'h_pct': h_pct,
        'h_remaining': h_remaining, 'h_total': h_total_days,
        'h_eh': h_eh, 'h_vtr': h_vtr, 'h_partner': h_partner,
        'today': today.strftime('%d.%m.%Y'),
    }


def get_straenge_for_user(user_id, db=None):
    """Berechnet die Stränge eines Users.
    Ein Strang = direkter Downline-Partner + dessen ganze Downline (rekursiv)."""
    own_db = db is None
    if own_db:
        db = get_db()
    direct = db.execute(
        'SELECT id, name FROM users WHERE parent_id = ? AND active = 1', (user_id,)
    ).fetchall()
    straenge = []
    for d in direct:
        chain_ids = [d['id']] + get_all_descendants(d['id'])
        ph = ','.join('?' * len(chain_ids))
        eh = db.execute(
            f'SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id IN ({ph}) AND status="abgeschlossen" AND recherche_status="freigegeben"',
            chain_ids
        ).fetchone()['s']
        straenge.append({'id': d['id'], 'name': d['name'], 'eh': float(eh)})
    straenge.sort(key=lambda s: -s['eh'])
    if own_db:
        db.close()
    return straenge


def evaluate_career_rule(rule, ctx):
    """Wertet eine einzelne Karriere-Regel gegen den Kontext aus."""
    own_eh = ctx['own_eh']
    total_eh = ctx['total_eh']  # eigen + alle Stränge ungekappt
    straenge = ctx['straenge']  # sortiert absteigend

    if rule['type'] == 'gesamt_eh':
        target = rule['target']
        cur = total_eh
        done = cur >= target
        pct = min(100, int(cur / target * 100)) if target > 0 else 100
        return {
            'icon': '⚡', 'type': rule['type'],
            'label': rule['label'], 'hint': rule.get('hint', ''),
            'current_label': f'{int(cur):,}'.replace(',', '.'),
            'target_label': f'{int(target):,} EH'.replace(',', '.'),
            'pct': pct, 'done': done,
        }

    if rule['type'] == 'max_strang_pct':
        # Kein Strang darf > X% des Total ausmachen
        pct_limit = rule['pct']
        if total_eh == 0 or not straenge:
            done = True
            top_pct = 0
            top_name = '–'
            top_eh = 0
        else:
            top_eh = straenge[0]['eh']
            top_pct = (top_eh / total_eh * 100) if total_eh > 0 else 0
            top_name = straenge[0]['name']
            done = top_pct <= pct_limit
        return {
            'icon': '⚖️', 'type': rule['type'],
            'label': rule['label'], 'hint': rule.get('hint', ''),
            'current_label': f"{top_name}: {int(top_pct)}%",
            'target_label': f'max {pct_limit}%',
            'pct': min(100, int(top_pct / pct_limit * 100)) if pct_limit > 0 else 0,
            'done': done,
            'detail': f"Größter Strang ({top_name}): {int(top_eh):,} EH von {int(total_eh):,} = {top_pct:.0f}%".replace(',', '.'),
        }

    if rule['type'] == 'qualified_straenge':
        # Mind. X Stränge mit min_per_strang ≤ EH ≤ max_per_strang (cap zählt für Eligibility)
        min_count = rule['min_count']
        min_per = rule['min_per_strang']
        max_per = rule['max_per_strang']
        qualified = [s for s in straenge if s['eh'] >= min_per]
        # Stränge die genau im Range sind
        in_range = [s for s in straenge if min_per <= s['eh']]
        done = len(in_range) >= min_count
        return {
            'icon': '⬡', 'type': rule['type'],
            'label': rule['label'], 'hint': rule.get('hint', ''),
            'current_label': f'{len(qualified)} qualifiziert',
            'target_label': f'≥ {min_count}',
            'pct': min(100, int(len(qualified) / min_count * 100)) if min_count > 0 else 100,
            'done': done,
            'detail': ', '.join([f"{s['name']}: {int(s['eh']):,}".replace(',', '.') + " EH" for s in straenge[:5]]) or 'Keine Stränge vorhanden',
        }

    if rule['type'] == 'max_per_strang':
        # Pro Strang werden max X EH gezählt — Rest muss aus anderen Quellen kommen
        cap = rule['cap']
        # Top-Strang vs. Cap
        top = straenge[0] if straenge else {'name': '–', 'eh': 0}
        # Anrechenbares Total: own_eh + min(cap, eh) für jeden Strang
        capped_total = own_eh + sum(min(cap, s['eh']) for s in straenge)
        # OK wenn top <= cap (oder durch capping eh okay)
        done = (not straenge) or top['eh'] <= cap
        return {
            'icon': '🎯', 'type': rule['type'],
            'label': rule['label'], 'hint': rule.get('hint', ''),
            'current_label': f"Top: {top['name']} {int(top['eh']):,}".replace(',', '.') + ' EH',
            'target_label': f'max {cap:,}'.replace(',', '.') + ' EH/Strang',
            'pct': min(100, int(top['eh'] / cap * 100)) if cap > 0 else 0,
            'done': done,
            'detail': f"Anrechenbar mit Cap: {int(capped_total):,} EH".replace(',', '.'),
        }

    if rule['type'] == 'restbereich_min':
        # Restbereich = own_eh + EH aus Strängen die nicht qualifiziert sind / unter Mindestschwelle
        # Pragmatisch: Restbereich = own_eh (eigene Aktivität)
        min_rest = rule['min_eh']
        cur = own_eh
        done = cur >= min_rest
        return {
            'icon': '🏠', 'type': rule['type'],
            'label': rule['label'], 'hint': rule.get('hint', ''),
            'current_label': f'{int(cur):,}'.replace(',', '.') + ' EH',
            'target_label': f'min {min_rest:,}'.replace(',', '.') + ' EH',
            'pct': min(100, int(cur / min_rest * 100)) if min_rest > 0 else 100,
            'done': done,
            'detail': f"Restbereich = eigene EH (eigene Verträge)",
        }

    return None


def get_career_criteria_status(user_id):
    """Cached für 30 Min."""
    ckey = f'career_crit:{user_id}'
    cached = cache_get(ckey)
    if cached is not None: return cached
    result = _get_career_criteria_status_uncached(user_id)
    cache_set(ckey, result, ttl=1800)
    return result


def _get_career_criteria_status_uncached(user_id):
    """Detaillierter Status zu Kriterien für die nächste Stufe (mit echten Regeln)."""
    db = get_db()
    user = db.execute('SELECT manual_career_level FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        db.close()
        return None

    own_eh = db.execute(
        'SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"',
        (user_id,)
    ).fetchone()['s']
    contracts = db.execute(
        'SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"',
        (user_id,)
    ).fetchone()['c']
    direct_partners = db.execute(
        'SELECT COUNT(*) as c FROM users WHERE parent_id=? AND active=1', (user_id,)
    ).fetchone()['c']
    straenge = get_straenge_for_user(user_id, db=db)
    team_eh = sum(s['eh'] for s in straenge)
    total_eh = own_eh + team_eh
    db.close()

    current = career_for_row(user['manual_career_level'], own_eh)
    next_level = next((cl for cl in CAREER_LEVELS if cl['level'] == current['level'] + 1), None)
    if not next_level:
        return {
            'current': current, 'next_level': None,
            'criteria': [], 'completed_count': 0, 'total_count': 0,
            'straenge': straenge, 'own_eh': int(own_eh), 'team_eh': int(team_eh), 'total_eh': int(total_eh),
        }

    ctx = {'own_eh': float(own_eh), 'total_eh': float(total_eh),
           'straenge': straenge, 'team_eh': float(team_eh)}

    criteria = []
    for rule in next_level['rules']:
        evaluated = evaluate_career_rule(rule, ctx)
        if evaluated:
            criteria.append(evaluated)

    bonus = [
        {'icon': '📄', 'label': 'Verträge (gesamt)', 'current': contracts, 'unit': '', 'hint': 'Karriere gesamt'},
        {'icon': '👥', 'label': 'Direkte Partner', 'current': direct_partners, 'unit': '', 'hint': 'Aktive Partner unter dir'},
        {'icon': '⬢', 'label': 'Team-EH (Downline)', 'current': int(team_eh), 'unit': 'EH', 'hint': 'Summe aller Stränge'},
    ]
    completed = sum(1 for c in criteria if c.get('done'))
    return {
        'current': current, 'next_level': next_level,
        'criteria': criteria, 'bonus': bonus,
        'completed_count': completed, 'total_count': len(criteria),
        'all_done': completed == len(criteria) if criteria else False,
        'straenge': straenge,
        'own_eh': int(own_eh), 'team_eh': int(team_eh), 'total_eh': int(total_eh),
    }


# === ACHIEVEMENTS / BADGES ===
ACHIEVEMENTS = [
    # Erste Schritte
    {'code': 'profile_complete',  'icon': '✨', 'name': 'Profil komplett',          'desc': 'Telefon + Vision + Geburtstag gesetzt',                  'tier': 'bronze'},
    {'code': 'vision_set',        'icon': '★',  'name': 'Vision gesetzt',           'desc': 'Eigenes „Warum" formuliert',                              'tier': 'bronze'},
    {'code': 'first_lead',        'icon': '◇',  'name': 'Erste Person',             'desc': 'Erster Eintrag in Namensliste',                           'tier': 'bronze'},
    {'code': 'first_appointment', 'icon': '◷',  'name': 'Erster Termin',            'desc': 'Ersten Kunden-Termin angelegt',                           'tier': 'bronze'},
    {'code': 'first_contract',    'icon': '📄', 'name': 'Erster Vertrag',           'desc': 'Ersten Vertrag im System angelegt',                       'tier': 'bronze'},
    {'code': 'first_freigegeben', 'icon': '✅', 'name': 'Freizeichnung erhalten',  'desc': 'Erster freigegebener Vertrag',                            'tier': 'silver'},
    # EH-Meilensteine
    {'code': 'eh_500',            'icon': '⚡', 'name': '500 EH',                   'desc': 'Erste 500 Einheiten produziert',                          'tier': 'bronze'},
    {'code': 'eh_1000',           'icon': '⚡', 'name': '1.000 EH',                 'desc': '1.000 Einheiten — Stufe LREP erreichbar',                'tier': 'silver'},
    {'code': 'eh_3500',           'icon': '⚡', 'name': '3.500 EH',                 'desc': '3.500 Einheiten — Stufe HREP erreichbar',                'tier': 'silver'},
    {'code': 'eh_9000',           'icon': '⚡', 'name': '9.000 EH',                 'desc': '9.000 Einheiten — Stufe CREP erreichbar',                'tier': 'gold'},
    {'code': 'eh_25000',          'icon': '⚡', 'name': '25.000 EH',                'desc': '25.000 Einheiten — Stufe DREP erreichbar',               'tier': 'gold'},
    {'code': 'eh_60000',          'icon': '⚡', 'name': '60.000 EH',                'desc': '60.000 Einheiten — Stufe GREP erreichbar',               'tier': 'platinum'},
    # Stufen-Beförderungen
    {'code': 'level_2',           'icon': '⬆',  'name': 'LREP erreicht',            'desc': 'Beförderung zu Leitendem Repräsentant',                    'tier': 'silver'},
    {'code': 'level_3',           'icon': '⬆',  'name': 'HREP erreicht',            'desc': 'Beförderung zu Hauptrepräsentant',                         'tier': 'silver'},
    {'code': 'level_4',           'icon': '⬆',  'name': 'CREP erreicht',            'desc': 'Beförderung zu Chefrepräsentant',                          'tier': 'gold'},
    {'code': 'level_5',           'icon': '⬆',  'name': 'DREP erreicht',            'desc': 'Beförderung zu Direktionsrepräsentant',                    'tier': 'gold'},
    {'code': 'level_6',           'icon': '👑', 'name': 'GREP erreicht',            'desc': 'Beförderung zu Generalrepräsentant — Top!',                'tier': 'platinum'},
    # Team-Aufbau
    {'code': 'first_partner',     'icon': '👥', 'name': 'Erster Partner',           'desc': 'Ersten direkten Partner gewonnen',                         'tier': 'silver'},
    {'code': 'partners_5',        'icon': '👥', 'name': '5 Partner',                'desc': '5 direkte Partner unter dir',                              'tier': 'gold'},
    {'code': 'partners_10',       'icon': '👥', 'name': '10 Partner',               'desc': '10 direkte Partner — wahres Team',                         'tier': 'gold'},
    {'code': 'partners_25',       'icon': '👥', 'name': '25 Partner',               'desc': '25 direkte Partner — beeindruckend',                       'tier': 'platinum'},
    # Wettbewerb
    {'code': 'week_top3',         'icon': '🥉', 'name': 'Top 3 der Woche',          'desc': 'Top 3 im Wochen-Ranking',                                  'tier': 'silver'},
    {'code': 'week_top1',         'icon': '🥇', 'name': 'Wochen-Champion',          'desc': 'Platz 1 im Wochen-Ranking',                                'tier': 'gold'},
    # Aktivität
    {'code': 'streak_7',          'icon': '🔥', 'name': '7-Tage-Streak',            'desc': '7 Tage am Stück eingeloggt',                               'tier': 'silver'},
    {'code': 'login_30',          'icon': '🔥', 'name': '30 Logins',                'desc': '30 Mal eingeloggt — du bist dabei!',                       'tier': 'silver'},
    {'code': 'contracts_10',      'icon': '📊', 'name': '10 Verträge',              'desc': '10 abgeschlossene Verträge',                               'tier': 'silver'},
    {'code': 'contracts_50',      'icon': '📊', 'name': '50 Verträge',              'desc': '50 abgeschlossene Verträge',                               'tier': 'gold'},
    # === ECHTE BELOHNUNGEN (Reward-Tier) ===
    {'code': 'reward_ferrari',         'icon': '🏎️', 'name': 'Ferrari-Abzeichen',         'desc': '99 EH erreicht — exklusives Ferrari-Pin als Belohnung',                'tier': 'reward'},
    {'code': 'reward_goldene_nadel',   'icon': '📎', 'name': 'Goldene Nadel',            'desc': '500 EH + 11 Anträge innerhalb von 30 Tagen — goldene Nadel',          'tier': 'reward'},
    {'code': 'reward_platin_nadel',    'icon': '📎', 'name': 'Platin Nadel',             'desc': '6 direkte Partner in 6 Monaten mit je 66 EH — Platin-Nadel',          'tier': 'reward'},
    {'code': 'reward_montblanc_pen',   'icon': '🖋️', 'name': 'Montblanc-Kugelschreiber', 'desc': 'Stufe 3 (HREP) erreicht — hochwertiger Montblanc-Kuli',               'tier': 'reward'},
    {'code': 'reward_montblanc_bag',   'icon': '👜', 'name': 'Montblanc-Tasche',         'desc': '666 EH in 2 Monaten produziert — Montblanc-Tasche als Belohnung',     'tier': 'reward'},
    {'code': 'reward_breitling',       'icon': '⌚', 'name': 'Breitling-Uhr',            'desc': 'Stufe 4 (CREP) erreicht — Breitling-Uhr im Wert von 7.000 €',         'tier': 'reward'},
]

ACHIEVEMENT_TIER_COLORS = {
    'bronze': '#cd7f32', 'silver': '#94a3b8', 'gold': '#d4a843',
    'platinum': '#a78bfa', 'reward': '#dc2626'
}


def get_achievement_by_code(code):
    return next((a for a in ACHIEVEMENTS if a['code'] == code), None)


def unlock_achievement(user_id, code, db=None):
    """Schaltet ein Achievement frei (idempotent — schon vorhanden = no-op)."""
    own_db = db is None
    if own_db:
        db = get_db()
    existing = db.execute('SELECT id FROM user_achievements WHERE user_id=? AND achievement_code=?', (user_id, code)).fetchone()
    if not existing:
        db.execute('INSERT INTO user_achievements (user_id, achievement_code) VALUES (?, ?)', (user_id, code))
        db.commit()
        # Für Notification beim nächsten Page-Load
        ach = get_achievement_by_code(code)
        if ach:
            log_activity(user_id, 'achievement', f'🏆 {get_user_name(user_id, db)} hat Achievement „{ach["name"]}" freigeschaltet', icon=ach['icon'], color='gold')
    if own_db:
        db.close()


def get_user_name(user_id, db):
    row = db.execute('SELECT name FROM users WHERE id=?', (user_id,)).fetchone()
    return row['name'] if row else 'Unbekannt'


def check_achievements_for_user(user_id):
    """Prüft alle Achievements für einen User und schaltet neue frei."""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        db.close()
        return []

    new_unlocks = []
    existing_codes = {r['achievement_code'] for r in db.execute('SELECT achievement_code FROM user_achievements WHERE user_id=?', (user_id,)).fetchall()}

    def maybe_unlock(code):
        if code not in existing_codes:
            unlock_achievement(user_id, code, db=db)
            existing_codes.add(code)
            ach = get_achievement_by_code(code)
            if ach:
                new_unlocks.append(ach)

    # Profil
    if user['vision'] and user['vision'].strip():
        maybe_unlock('vision_set')
    if user['vision'] and user['vision'].strip() and user['phone'] and user['birthday']:
        maybe_unlock('profile_complete')

    # Leads
    lead_count = db.execute('SELECT COUNT(*) as c FROM leads WHERE owner_id=?', (user_id,)).fetchone()['c']
    if lead_count >= 1:
        maybe_unlock('first_lead')

    # Termine
    a_count = db.execute('SELECT COUNT(*) as c FROM appointments WHERE owner_id=?', (user_id,)).fetchone()['c']
    if a_count >= 1:
        maybe_unlock('first_appointment')

    # Verträge
    c_count = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=?', (user_id,)).fetchone()['c']
    if c_count >= 1:
        maybe_unlock('first_contract')
    if c_count >= 10:
        maybe_unlock('contracts_10')
    if c_count >= 50:
        maybe_unlock('contracts_50')

    # Freigegeben
    free_count = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND recherche_status="freigegeben" AND status="abgeschlossen"', (user_id,)).fetchone()['c']
    if free_count >= 1:
        maybe_unlock('first_freigegeben')

    # EH
    own_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (user_id,)).fetchone()['s']
    if own_eh >= 500:    maybe_unlock('eh_500')
    if own_eh >= 1000:   maybe_unlock('eh_1000')
    if own_eh >= 3500:   maybe_unlock('eh_3500')
    if own_eh >= 9000:   maybe_unlock('eh_9000')
    if own_eh >= 25000:  maybe_unlock('eh_25000')
    if own_eh >= 60000:  maybe_unlock('eh_60000')

    # Stufen
    level = max(user['manual_career_level'] or 1, 1)
    if level >= 2: maybe_unlock('level_2')
    if level >= 3: maybe_unlock('level_3')
    if level >= 4: maybe_unlock('level_4')
    if level >= 5: maybe_unlock('level_5')
    if level >= 6: maybe_unlock('level_6')

    # Direkte Partner
    p_count = db.execute('SELECT COUNT(*) as c FROM users WHERE parent_id=? AND active=1', (user_id,)).fetchone()['c']
    if p_count >= 1:  maybe_unlock('first_partner')
    if p_count >= 5:  maybe_unlock('partners_5')
    if p_count >= 10: maybe_unlock('partners_10')
    if p_count >= 25: maybe_unlock('partners_25')

    # Login-Count
    if (user['login_count'] or 0) >= 30:
        maybe_unlock('login_30')

    # === REWARDS / ECHTE BELOHNUNGEN ===
    # Ferrari-Abzeichen: 99 EH
    if own_eh >= 99:
        maybe_unlock('reward_ferrari')
    # Montblanc-Kuli: Stufe 3 erreicht
    if level >= 3:
        maybe_unlock('reward_montblanc_pen')
    # Breitling-Uhr: Stufe 4 erreicht
    if level >= 4:
        maybe_unlock('reward_breitling')
    # Montblanc-Tasche: 666 EH in den letzten 60 Tagen (2 Monate)
    eh_60d = db.execute(
        'SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben" AND date(abschluss_date) >= date("now","-60 days")',
        (user_id,)
    ).fetchone()['s']
    if eh_60d >= 666:
        maybe_unlock('reward_montblanc_bag')

    # Goldene Nadel: 500 EH + 11 Anträge in 30 Tagen (rolling)
    # Pragmatisch: irgendein 30-Tage-Fenster mit beiden Bedingungen
    contracts_history = db.execute('''
        SELECT date(abschluss_date) as ad, einheiten FROM contracts
        WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"
        AND abschluss_date IS NOT NULL
        ORDER BY abschluss_date ASC
    ''', (user_id,)).fetchall()
    gold_nadel = False
    for i, c1 in enumerate(contracts_history):
        try:
            d1 = datetime.strptime(c1['ad'], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            continue
        cnt, eh_sum = 0, 0.0
        for c2 in contracts_history[i:]:
            try:
                d2 = datetime.strptime(c2['ad'], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                continue
            if (d2 - d1).days > 30:
                break
            cnt += 1
            eh_sum += c2['einheiten'] or 0
            if cnt >= 11 and eh_sum >= 500:
                gold_nadel = True
                break
        if gold_nadel:
            break
    if gold_nadel:
        maybe_unlock('reward_goldene_nadel')

    # Platin Nadel: 6 direkte Partner in 6 Monaten mit je 66 EH
    direct_180d = db.execute('''
        SELECT id FROM users
        WHERE parent_id=? AND active=1
        AND date(joined_date) >= date("now","-180 days")
    ''', (user_id,)).fetchall()
    qualified = 0
    for p in direct_180d:
        p_eh = db.execute('''
            SELECT COALESCE(SUM(einheiten),0) as s FROM contracts
            WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"
            AND date(abschluss_date) >= date("now","-180 days")
        ''', (p['id'],)).fetchone()['s']
        if p_eh >= 66:
            qualified += 1
    if qualified >= 6:
        maybe_unlock('reward_platin_nadel')

    db.close()
    return new_unlocks


def get_unseen_achievements(user_id):
    """Liefert noch nicht gesehene Achievements (für Modal-Popup)."""
    db = get_db()
    rows = db.execute('SELECT achievement_code, unlocked_at FROM user_achievements WHERE user_id=? AND seen=0 ORDER BY unlocked_at', (user_id,)).fetchall()
    result = []
    for r in rows:
        ach = get_achievement_by_code(r['achievement_code'])
        if ach:
            result.append({**ach, 'unlocked_at': r['unlocked_at']})
    db.close()
    return result


def mark_achievements_seen(user_id):
    db = get_db()
    db.execute('UPDATE user_achievements SET seen=1 WHERE user_id=?', (user_id,))
    db.commit()
    db.close()


def career_for_row(manual_level, eh):
    """Korrekte Karriere-Stufe = MAX(manual_career_level, EH-erreichte Stufe)."""
    earned = 1
    for cl in CAREER_LEVELS:
        if (eh or 0) >= cl['min_eh']:
            earned = cl['level']
        else:
            break
    final = max(manual_level or 1, earned)
    return next((c for c in CAREER_LEVELS if c['level'] == final), CAREER_LEVELS[0])


def get_greeting_for_user(name, career, next_level, own_eh, eh_to_next):
    """Personalisierte Begrüßung — motivierend + datengestützt."""
    h = datetime.now().hour
    if h < 11:
        time_greeting = 'Guten Morgen'
    elif h < 14:
        time_greeting = 'Hallo'
    elif h < 18:
        time_greeting = 'Hi'
    else:
        time_greeting = 'Guten Abend'
    first_name = name.split()[0] if name else 'Champion'

    if next_level and eh_to_next > 0:
        if eh_to_next <= 200:
            sub = f"Nur noch {int(eh_to_next)} EH bis {next_level['short']} — let's GO! 🚀"
        elif eh_to_next <= 1000:
            sub = f"Noch {int(eh_to_next)} EH bis {next_level['short']} — du hast das! 💪"
        else:
            sub = f"Auf zu {next_level['short']} ({int(eh_to_next)} EH übrig) — Schritt für Schritt 🎯"
    elif not next_level:
        sub = "Höchste Stufe erreicht — Vorbild für alle! 👑"
    else:
        sub = "Lass uns heute Großes erreichen 💪"

    return {'greeting': f'{time_greeting}, {first_name}!', 'sub': sub, 'first_name': first_name}


def get_upcoming_birthdays(scope_user_id=None, days_ahead=30):
    """Liefert kommende Geburtstage in den nächsten X Tagen.
    scope_user_id=None = alle, sonst nur User-Downline + deren Kunden."""
    db = get_db()
    if scope_user_id:
        ids = [scope_user_id] + get_all_descendants(scope_user_id)
        ph = ','.join('?' * len(ids))
        partner_rows = db.execute(f'SELECT id, name, birthday, phone, email FROM users WHERE id IN ({ph}) AND birthday IS NOT NULL AND active = 1', ids).fetchall()
        kunden_rows = db.execute(f'SELECT id, name, birthday, phone, email, owner_id FROM leads WHERE owner_id IN ({ph}) AND birthday IS NOT NULL', ids).fetchall()
    else:
        partner_rows = db.execute('SELECT id, name, birthday, phone, email FROM users WHERE birthday IS NOT NULL AND active = 1').fetchall()
        kunden_rows = db.execute('SELECT id, name, birthday, phone, email, owner_id FROM leads WHERE birthday IS NOT NULL').fetchall()

    # Owner-Namen für Kunden
    owner_names = {}
    if kunden_rows:
        owner_ids = list(set(r['owner_id'] for r in kunden_rows if r['owner_id']))
        if owner_ids:
            ph = ','.join('?' * len(owner_ids))
            for r in db.execute(f'SELECT id, name FROM users WHERE id IN ({ph})', owner_ids).fetchall():
                owner_names[r['id']] = r['name']
    db.close()

    result = []
    for r in partner_rows:
        d_until = days_until_birthday(r['birthday'])
        if d_until is not None and d_until <= days_ahead:
            result.append({
                'type': 'partner', 'id': r['id'], 'name': r['name'],
                'phone': r['phone'], 'email': r['email'],
                'birthday': r['birthday'], 'days_until': d_until,
                'age': calculate_age(r['birthday']),
                'owner_name': None
            })
    for r in kunden_rows:
        d_until = days_until_birthday(r['birthday'])
        if d_until is not None and d_until <= days_ahead:
            result.append({
                'type': 'kunde', 'id': r['id'], 'name': r['name'],
                'phone': r['phone'], 'email': r['email'],
                'birthday': r['birthday'], 'days_until': d_until,
                'age': calculate_age(r['birthday']),
                'owner_name': owner_names.get(r['owner_id'], '–')
            })
    result.sort(key=lambda x: x['days_until'])
    return result


class User(UserMixin):
    def __init__(self, row):
        self.id = row['id']
        self.name = row['name']
        self.email = row['email']
        self.role = row['role']
        self.parent_id = row['parent_id']
        self.level = row['level']
        self.phone = row['phone']
        self.joined_date = row['joined_date']
        self.manual_career_level = row['manual_career_level'] if 'manual_career_level' in row.keys() else 1
        try:
            self.is_co_admin = bool(row['is_co_admin']) if 'is_co_admin' in row.keys() else False
        except Exception:
            self.is_co_admin = False
        try:
            self.must_change_password = bool(row['must_change_password']) if 'must_change_password' in row.keys() else False
        except Exception:
            self.must_change_password = False

    @property
    def has_admin_access(self):
        """Admin oder Co-Admin haben Admin-Rechte."""
        return self.role == 'admin' or self.is_co_admin


def auto_promote_user(user_id):
    """Befördert User automatisch wenn EH eine Stufe erlauben (nur hoch, nie runter)."""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        db.close()
        return
    own_eh = db.execute('SELECT COALESCE(SUM(einheiten), 0) as s FROM contracts WHERE owner_id = ? AND status = "abgeschlossen" AND recherche_status = "freigegeben"', (user_id,)).fetchone()['s']
    earned = 1
    for cl in CAREER_LEVELS:
        if own_eh >= cl['min_eh']:
            earned = cl['level']
        else:
            break
    current_manual = user['manual_career_level'] or 1
    if earned > current_manual:
        db.execute('UPDATE users SET manual_career_level = ? WHERE id = ?', (earned, user_id))
        db.commit()
        # Stufen-Aufstieg loggen!
        new_career = next((cl for cl in CAREER_LEVELS if cl['level'] == earned), None)
        if new_career:
            log_activity(user_id, 'befoerderung',
                f'{user["name"]} wurde automatisch zu {new_career["short"]} befördert! 🚀',
                icon='⬆️', color='gold')
            # Push: Beförderung an User selbst + Upline
            try:
                send_push_to_user(user_id,
                    title=f'🎯 Beförderung erreicht!',
                    body=f'Glückwunsch — du bist jetzt {new_career["short"]} ({new_career["name"]})!',
                    url='/dashboard', urgent=True, tag='goal',
                    push_type='goal_achieved')
                if user['parent_id']:
                    send_push_to_user(user['parent_id'],
                        title=f'🎉 {user["name"]} wurde befördert!',
                        body=f'Neue Stufe: {new_career["short"]} — kurz gratulieren!',
                        url=f'/partner/{user_id}/profil', urgent=True, tag='abschluss',
                        push_type='contract_done')
            except Exception:
                pass
    db.close()


# === KI-COACH: SMART INSIGHTS ===
def get_smart_insights(scope_user_id=None):
    """Analysiert Daten und liefert Action-Items für den Admin/Upline.
    scope_user_id=None = ganzer Vertrieb (Admin), sonst nur Downline dieses Users."""
    db = get_db()

    if scope_user_id:
        ids = [scope_user_id] + get_all_descendants(scope_user_id)
    else:
        rows = db.execute('SELECT id FROM users WHERE active = 1').fetchall()
        ids = [r['id'] for r in rows]

    if not ids:
        db.close()
        return {'urgent_calls': [], 'congrats': [], 'inactive': [], 'pending_research': [],
                'onboarding_stuck': [], 'wins_today': [], 'silence_alert': [], 'team_score': 0,
                'urgent_count': 0, 'total_calls_needed': 0}

    ph = ','.join('?' * len(ids))

    # 1) INAKTIV — lange nicht eingeloggt
    inactive_rows = db.execute(f'''
        SELECT u.id, u.name, u.email, u.phone, u.manual_career_level, u.last_login, u.joined_date,
               COALESCE(SUM(c.einheiten), 0) as eh
        FROM users u
        LEFT JOIN contracts c ON c.owner_id = u.id AND c.status = "abgeschlossen" AND c.recherche_status = "freigegeben"
        WHERE u.id IN ({ph}) AND u.active = 1
          AND (u.last_login IS NULL OR u.last_login < datetime('now', '-7 days'))
        GROUP BY u.id
        ORDER BY u.last_login ASC NULLS FIRST
        LIMIT 10
    ''', ids).fetchall()

    # 2) KURZ VOR BEFÖRDERUNG — über 80% zur nächsten Stufe
    eh_rows = db.execute(f'''
        SELECT u.id, u.name, u.email, u.phone, u.manual_career_level,
               COALESCE(SUM(c.einheiten), 0) as eh
        FROM users u
        LEFT JOIN contracts c ON c.owner_id = u.id AND c.status = "abgeschlossen" AND c.recherche_status = "freigegeben"
        WHERE u.id IN ({ph}) AND u.active = 1
        GROUP BY u.id
    ''', ids).fetchall()
    congrats = []
    for r in eh_rows:
        career = next((cl for cl in CAREER_LEVELS if cl['level'] == (r['manual_career_level'] or 1)), CAREER_LEVELS[0])
        next_level = next((cl for cl in CAREER_LEVELS if cl['level'] == career['level'] + 1), None)
        if not next_level:
            continue
        progress = (r['eh'] / next_level['min_eh'] * 100) if next_level['min_eh'] > 0 else 0
        if progress >= 80 and progress < 100:
            congrats.append({
                'id': r['id'], 'name': r['name'], 'phone': r['phone'], 'email': r['email'],
                'current': career, 'next_level': next_level,
                'eh': r['eh'], 'progress': round(progress),
                'eh_to_go': max(0, next_level['min_eh'] - r['eh'])
            })
    congrats.sort(key=lambda x: -x['progress'])

    # 3) HÄNGENDE RECHERCHEN — Verträge ausstehend > 14 Tage
    pending_research = db.execute(f'''
        SELECT c.id, c.client_name, c.produkt, c.einheiten, c.created_at,
               u.name as berater_name, u.phone as berater_phone, u.id as berater_id,
               julianday('now') - julianday(c.created_at) as tage_offen
        FROM contracts c
        JOIN users u ON c.owner_id = u.id
        WHERE c.recherche_status IN ('ausstehend', '') AND c.einheiten > 0
          AND c.owner_id IN ({ph})
          AND julianday('now') - julianday(c.created_at) > 14
        ORDER BY tage_offen DESC LIMIT 10
    ''', ids).fetchall()

    # 4) ONBOARDING HÄNGT — > 30 Tage dabei aber <3 Onboarding-Schritte
    onboarding_stuck = db.execute(f'''
        SELECT u.id, u.name, u.email, u.phone, u.joined_date,
               (u.onboarding_endgespraech + u.onboarding_einarbeitung_1 +
                u.onboarding_einarbeitung_2 + u.onboarding_einarbeitung_3 +
                u.onboarding_seminar_bezahlt) as ob_done,
               julianday('now') - julianday(u.joined_date) as tage_dabei
        FROM users u
        WHERE u.id IN ({ph}) AND u.active = 1
          AND julianday('now') - julianday(u.joined_date) > 30
          AND (u.onboarding_endgespraech + u.onboarding_einarbeitung_1 +
               u.onboarding_einarbeitung_2 + u.onboarding_einarbeitung_3 +
               u.onboarding_seminar_bezahlt) < 3
        ORDER BY tage_dabei DESC LIMIT 10
    ''', ids).fetchall()

    # 5) HEUTIGE GEWINNE — Verträge heute mit freigegebenem Status
    today_str = date.today().strftime('%Y-%m-%d')
    wins_today = db.execute(f'''
        SELECT c.client_name, c.einheiten, c.volumen, c.produkt, u.name as berater_name
        FROM contracts c
        JOIN users u ON c.owner_id = u.id
        WHERE c.status = "abgeschlossen" AND c.recherche_status = "freigegeben"
          AND c.owner_id IN ({ph})
          AND date(c.abschluss_date) = ?
        ORDER BY c.einheiten DESC
    ''', ids + [today_str]).fetchall()

    # 6) SCHWEIGEN — Partner > 30 Tage keine Aktivität (kein Vertrag, kein Login)
    silence = db.execute(f'''
        SELECT u.id, u.name, u.email, u.phone, u.last_login, u.joined_date,
               julianday('now') - julianday(COALESCE(u.last_login, u.joined_date)) as silence_days
        FROM users u
        WHERE u.id IN ({ph}) AND u.active = 1
          AND julianday('now') - julianday(COALESCE(u.last_login, u.joined_date)) > 30
          AND NOT EXISTS (
              SELECT 1 FROM contracts c WHERE c.owner_id = u.id
              AND julianday('now') - julianday(c.created_at) <= 30
          )
        ORDER BY silence_days DESC LIMIT 10
    ''', ids).fetchall()

    # 7) URGENT CALLS — Top-Liste zum SOFORT anrufen (priorisiert)
    urgent_calls = []
    seen_ids = set()
    # Priorität 1: Schweigen (höchste Dringlichkeit)
    for r in silence[:3]:
        if r['id'] not in seen_ids:
            urgent_calls.append({
                'id': r['id'], 'name': r['name'], 'phone': r['phone'], 'email': r['email'],
                'reason': f"Seit {int(r['silence_days'])} Tagen keine Aktivität",
                'priority': 'hoch', 'icon': '🔴'
            })
            seen_ids.add(r['id'])
    # Priorität 2: Hängende Recherchen
    for r in pending_research[:3]:
        if r['berater_id'] not in seen_ids:
            urgent_calls.append({
                'id': r['berater_id'], 'name': r['berater_name'], 'phone': r['berater_phone'], 'email': '',
                'reason': "Recherche bei „" + str(r['client_name']) + "“ seit " + str(int(r['tage_offen'])) + " Tagen offen",
                'priority': 'hoch', 'icon': '🟠'
            })
            seen_ids.add(r['berater_id'])
    # Priorität 3: Kurz vor Beförderung — anrufen, motivieren!
    for c in congrats[:3]:
        if c['id'] not in seen_ids:
            urgent_calls.append({
                'id': c['id'], 'name': c['name'], 'phone': c['phone'], 'email': c['email'],
                'reason': f"Nur noch {int(c['eh_to_go'])} EH bis {c['next_level']['short']} — JETZT motivieren!",
                'priority': 'mittel', 'icon': '🟡'
            })
            seen_ids.add(c['id'])
    # Priorität 4: Inaktive
    for r in inactive_rows[:3]:
        if r['id'] not in seen_ids:
            urgent_calls.append({
                'id': r['id'], 'name': r['name'], 'phone': r['phone'], 'email': r['email'],
                'reason': "Mehrere Tage nicht im System aktiv",
                'priority': 'niedrig', 'icon': '🟢'
            })
            seen_ids.add(r['id'])
    urgent_calls = urgent_calls[:8]

    # 8) TEAM-SCORE (0-100): Mix aus Aktivität + Vertragsabschluss + Wachstum
    total_active = len(ids)
    active_logins_7d = db.execute(f'SELECT COUNT(*) as c FROM users WHERE id IN ({ph}) AND last_login > datetime("now", "-7 days")', ids).fetchone()['c']
    contracts_30d = db.execute(f'SELECT COUNT(*) as c FROM contracts WHERE owner_id IN ({ph}) AND status="abgeschlossen" AND recherche_status="freigegeben" AND abschluss_date > date("now", "-30 days")', ids).fetchone()['c']
    new_partners_30d = db.execute(f'SELECT COUNT(*) as c FROM users WHERE id IN ({ph}) AND joined_date > date("now", "-30 days")', ids).fetchone()['c']
    activity_score = min(100, (active_logins_7d / max(1, total_active)) * 100)
    contract_score = min(100, contracts_30d * 10)
    growth_score = min(100, new_partners_30d * 20)
    team_score = int((activity_score * 0.4 + contract_score * 0.3 + growth_score * 0.3))

    db.close()

    # Geburtstage (heute + kommende 14 Tage)
    upcoming_bdays = get_upcoming_birthdays(scope_user_id=scope_user_id, days_ahead=14)
    today_bdays = [b for b in upcoming_bdays if b['days_until'] == 0]

    # Geburtstage zu Urgent Calls hinzufügen
    for b in today_bdays:
        prefix = '🎂 ' + ('Kunden-Geburtstag' if b['type'] == 'kunde' else 'Partner-Geburtstag')
        suffix = f' (wird {b["age"]+1})' if b['age'] is not None else ''
        urgent_calls.insert(0, {
            'id': b['id'], 'name': b['name'],
            'phone': b['phone'], 'email': b['email'],
            'reason': f'{prefix} HEUTE!{suffix} — Glückwunsch anrufen 📞',
            'priority': 'hoch', 'icon': '🎂'
        })

    return {
        'urgent_calls': urgent_calls[:10],
        'congrats': congrats,
        'inactive': [dict(r) for r in inactive_rows],
        'pending_research': [dict(r) for r in pending_research],
        'onboarding_stuck': [dict(r) for r in onboarding_stuck],
        'wins_today': [dict(r) for r in wins_today],
        'silence_alert': [dict(r) for r in silence],
        'team_score': team_score,
        'urgent_count': len(urgent_calls),
        'active_logins_7d': active_logins_7d,
        'contracts_30d': contracts_30d,
        'new_partners_30d': new_partners_30d,
        'total_active': total_active,
        'upcoming_birthdays': upcoming_bdays,
        'today_birthdays': today_bdays,
    }


def get_career_level_for_user(user_id):
    """Stufe eines Users: max(manual_career_level, calculated_from_eh)."""
    db = get_db()
    user = db.execute('SELECT manual_career_level FROM users WHERE id = ?', (user_id,)).fetchone()
    own_eh = db.execute('SELECT COALESCE(SUM(einheiten), 0) as s FROM contracts WHERE owner_id = ? AND status = "abgeschlossen" AND recherche_status = "freigegeben"', (user_id,)).fetchone()['s']
    db.close()
    earned_level = 1
    for cl in CAREER_LEVELS:
        if own_eh >= cl['min_eh']:
            earned_level = cl['level']
        else:
            break
    final = max(user['manual_career_level'] or 1, earned_level) if user else earned_level
    for cl in CAREER_LEVELS:
        if cl['level'] == final:
            return cl
    return CAREER_LEVELS[0]


# === 6-Monats-Zyklus ===
def get_current_period():
    """Gibt aktuelle Halbjahresperiode zurück."""
    today = date.today()
    if today.month <= 6:
        return {'start': f'{today.year}-01-01', 'end': f'{today.year}-06-30',
                'label': f'H1/{today.year}', 'name': f'1. Halbjahr {today.year}'}
    return {'start': f'{today.year}-07-01', 'end': f'{today.year}-12-31',
            'label': f'H2/{today.year}', 'name': f'2. Halbjahr {today.year}'}


@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    row = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    db.close()
    if row:
        return User(row)
    return None


def record_partner_view(visitor_id, viewed_user_id):
    """Trackt Partner-Profil-Aufrufe für die "Zuletzt geöffnet"-Pin-Bar.
    UPSERT: gleicher Partner → bumpt nur viewed_at."""
    if not visitor_id or not viewed_user_id or visitor_id == viewed_user_id:
        return
    try:
        db = get_db()
        db.execute('''INSERT INTO recent_partner_views (visitor_id, viewed_user_id, viewed_at)
                      VALUES (?, ?, CURRENT_TIMESTAMP)
                      ON CONFLICT(visitor_id, viewed_user_id)
                      DO UPDATE SET viewed_at = CURRENT_TIMESTAMP''',
                   (visitor_id, viewed_user_id))
        db.commit()
        db.close()
        # Cache invalidieren damit nächster Page-Load die neue Pin-Bar zeigt
        cache_invalidate(f'recent:{visitor_id}')
    except Exception as e:
        print(f'[record_partner_view] {e}')


def get_recent_partner_views(visitor_id, limit=3):
    """Gibt die top N zuletzt besuchten Partner zurück (für Pin-Bar)."""
    ckey = f'recent:{visitor_id}:{limit}'
    cached = cache_get(ckey)
    if cached is not None:
        return cached
    try:
        db = get_db()
        rows = db.execute('''SELECT u.id, u.name, u.photo_path, r.viewed_at
                             FROM recent_partner_views r
                             JOIN users u ON u.id = r.viewed_user_id
                             WHERE r.visitor_id = ? AND u.active = 1
                             ORDER BY r.viewed_at DESC
                             LIMIT ?''', (visitor_id, limit)).fetchall()
        db.close()
        result = [{'id': r['id'], 'name': r['name'],
                   'photo_path': r['photo_path'], 'viewed_at': r['viewed_at']} for r in rows]
        cache_set(ckey, result, ttl=600)
        return result
    except Exception as e:
        print(f'[get_recent_partner_views] {e}')
        return []


@app.context_processor
def inject_career():
    """Stellt aktuelle Karriere-Stufe + Pending-Anzahl + Coach-Anzahl bereit (CACHED)."""
    if current_user.is_authenticated:
        # Career: cached für 60s pro User
        cache_key = f'ctx:career:{current_user.id}'
        ctx = cache_get(cache_key)
        if ctx is None:
            ctx = {'my_career': get_career_level_for_user(current_user.id)}
            if current_user.role == 'admin':
                db = get_db()
                cnt = db.execute('SELECT COUNT(*) as c FROM users WHERE pending_career_level IS NOT NULL AND active = 1').fetchone()['c']
                db.close()
                ctx['pending_count'] = cnt
            # Coach-Insights für Bell-Badge: cached für 5 min
            ai_key = f'ctx:coach_alerts:{current_user.id}'
            alerts = cache_get(ai_key)
            if alerts is None:
                try:
                    scope = None if current_user.role == 'admin' else current_user.id
                    insights = get_smart_insights(scope_user_id=scope)
                    alerts = insights['urgent_count']
                except Exception:
                    alerts = 0
                cache_set(ai_key, alerts, ttl=1800)
            ctx['coach_alerts'] = alerts
            # Team-Kalender verfügbar? (ab HREP+ in der Kette)
            try:
                ctx['team_calendar_available'] = bool(get_team_calendar_root(current_user.id))
            except Exception:
                ctx['team_calendar_available'] = False
            # Inactive-Alert: 3+ Tage stille direkte Partner (für Führungskräfte)
            try:
                inact = get_inactive_team_members(current_user.id, days=3, scope='direct')
                ctx['inactive_alert'] = [
                    {'id': u['id'], 'name': u['name'], 'days': u['days_inactive']}
                    for u in inact[:3]
                ] if inact else []
            except Exception:
                ctx['inactive_alert'] = []
            cache_set(cache_key, ctx, ttl=60)
        # Sprachpräferenz + Foto + feature_tier aus DB
        try:
            db = get_db()
            row = db.execute('SELECT language, photo_path, advanced_mode, manual_career_level FROM users WHERE id=?', (current_user.id,)).fetchone()
            db.close()
            ctx['user_lang'] = (row['language'] if row and row['language'] else 'de')
            ctx['user_photo'] = (row['photo_path'] if row and row['photo_path'] else None)
            # Feature-Tier: 1 = Starter, 2 = LREP, 3 = HREP+, oder advanced_mode = full
            lvl = (row['manual_career_level'] or 1) if row else 1
            adv = (row['advanced_mode'] or 0) if row else 0
            if current_user.has_admin_access or adv:
                ctx['feature_tier'] = 3
            elif lvl >= 3:
                ctx['feature_tier'] = 3
            elif lvl >= 2:
                ctx['feature_tier'] = 2
            else:
                ctx['feature_tier'] = 1
        except Exception:
            ctx['user_lang'] = 'de'
            ctx['user_photo'] = None
            ctx['feature_tier'] = 1
        # Pin-Bar: zuletzt geöffnete Partner-Profile (nur für Stufe 2+, sonst Lärm)
        try:
            if ctx.get('feature_tier', 1) >= 2:
                ctx['recent_partners'] = get_recent_partner_views(current_user.id, limit=3)
            else:
                ctx['recent_partners'] = []
        except Exception:
            ctx['recent_partners'] = []
        # Patch-Notes: Anzahl ungelesener (5-Min-Cache pro User)
        try:
            pkey = f'ctx:patch_unread:{current_user.id}'
            cached_pn = cache_get(pkey)
            if cached_pn is None:
                db_pn = get_db()
                pn_row = db_pn.execute('''SELECT COUNT(*) c FROM patch_notes p
                                          WHERE NOT EXISTS (SELECT 1 FROM patch_notes_seen
                                                            WHERE user_id=? AND patch_id=p.id)''',
                                     (current_user.id,)).fetchone()
                db_pn.close()
                cached_pn = pn_row['c'] if pn_row else 0
                cache_set(pkey, cached_pn, ttl=300)
            ctx['patch_unread'] = cached_pn
        except Exception:
            ctx['patch_unread'] = 0
        # Newsletter: ungelesene Items seit last_seen
        try:
            nkey = f'ctx:newsletter_unread:{current_user.id}'
            cached_nl = cache_get(nkey)
            if cached_nl is None:
                db_nl = get_db()
                last_seen_row = db_nl.execute('SELECT last_seen_at FROM newsletter_last_seen WHERE user_id=?',
                                            (current_user.id,)).fetchone()
                last_seen = last_seen_row['last_seen_at'] if last_seen_row else '2000-01-01 00:00:00'
                nl_row = db_nl.execute('''SELECT COUNT(*) c FROM newsletter_items
                                          WHERE COALESCE(published_at, fetched_at) > ?''',
                                     (last_seen,)).fetchone()
                db_nl.close()
                cached_nl = nl_row['c'] if nl_row else 0
                cache_set(nkey, cached_nl, ttl=300)
            ctx['newsletter_unread'] = cached_nl
        except Exception:
            ctx['newsletter_unread'] = 0
        return ctx
    # Nicht-authentifizierte User (Login, Register, Public Pages) — leeres aber valides dict
    return {
        'user_lang': 'de',
        'user_photo': None,
        'feature_tier': 1,
        'team_calendar_available': False,
        'inactive_alert': [],
        'coach_alerts': 0,
        'pending_count': 0,
        'streak_days': 0,
        'vision_needed': False,
        'patch_unread': 0,
        'newsletter_unread': 0,
    }


@app.route('/admin/team/<int:uid>/toggle-advanced', methods=['POST'])
@login_required
def admin_toggle_advanced(uid):
    """Admin schaltet einem Partner Advanced-Mode (volle Sidebar) frei oder ab."""
    if not current_user.has_admin_access:
        return redirect(url_for('dashboard'))
    db = get_db()
    cur = db.execute('SELECT advanced_mode FROM users WHERE id=?', (uid,)).fetchone()
    if cur is not None:
        new_val = 0 if (cur['advanced_mode'] or 0) else 1
        db.execute('UPDATE users SET advanced_mode=? WHERE id=?', (new_val, uid))
        db.commit()
        cache_invalidate('ctx:'); cache_invalidate('news:'); cache_invalidate('coach_acts:'); cache_invalidate('forecast:'); cache_invalidate('strang:'); cache_invalidate('adm_pers:'); cache_invalidate('admin_dash:')
        flash(f'Advanced-Mode {"aktiviert" if new_val else "deaktiviert"}.', 'success')
    db.close()
    return redirect(request.referrer or url_for('team'))
    return {'user_lang': 'de'}


@app.route('/api/set-language', methods=['POST'])
@login_required
def api_set_language():
    """Speichert Sprachpräferenz (DE/EN/FR) für aktuellen User."""
    lang = (request.form.get('lang') or request.json.get('lang') if request.is_json else request.form.get('lang') or '').strip().lower()
    if lang not in ('de', 'en', 'fr'):
        return jsonify({'ok': False, 'error': 'invalid_language'}), 400
    db = get_db()
    db.execute('UPDATE users SET language=? WHERE id=?', (lang, current_user.id))
    db.commit()
    db.close()
    cache_invalidate(f'ctx:career:{current_user.id}')
    return jsonify({'ok': True, 'lang': lang})


# === ADMIN: AUDIT-LOG ===
@app.route('/admin/audit')
@login_required
def admin_audit():
    """Audit-Log: alle sicherheitsrelevanten Aktionen."""
    if current_user.role != 'admin':
        flash('Nur Hauptadmin', 'error')
        return redirect(url_for('dashboard'))
    db = get_db()
    rows = db.execute('''
        SELECT a.*, u.name as user_name FROM activity_log a
        LEFT JOIN users u ON a.user_id = u.id
        WHERE a.event_type IN ('login', 'co_admin_change', 'partner_neu', 'achievement', 'public_lead')
        OR a.event_type LIKE '%password%'
        OR a.event_type LIKE '%delete%'
        OR a.event_type LIKE '%admin%'
        ORDER BY a.created_at DESC LIMIT 200
    ''').fetchall()
    db.close()
    return render_template('admin_audit.html', entries=rows)


# === DATENSCHUTZ ===
@app.route('/mockups')
def design_mockups():
    """Design-Vorschau-Seite — 4 Stilrichtungen zur Auswahl."""
    return render_template('mockups.html')


@app.route('/datenschutz')
def datenschutz():
    """Öffentliche Datenschutz-Seite."""
    return render_template('datenschutz.html')


# === ADMIN: CO-ADMIN-TOGGLE ===
@app.route('/admin/team/<int:uid>/toggle-co-admin', methods=['POST'])
@login_required
def admin_toggle_co_admin(uid):
    """Nur der echte Admin (nicht Co-Admin) darf Co-Admin-Status verteilen."""
    if current_user.role != 'admin':
        flash('Nur der Hauptadmin kann Co-Admins ernennen', 'error')
        return redirect(url_for('team'))
    db = get_db()
    user = db.execute('SELECT id, name, is_co_admin FROM users WHERE id = ?', (uid,)).fetchone()
    if not user:
        db.close()
        return redirect(url_for('team'))
    new_state = 0 if user['is_co_admin'] else 1
    db.execute('UPDATE users SET is_co_admin = ? WHERE id = ?', (new_state, uid))
    db.commit()
    db.close()
    cache_invalidate('ctx:'); cache_invalidate('news:'); cache_invalidate('coach_acts:'); cache_invalidate('forecast:'); cache_invalidate('strang:'); cache_invalidate('adm_pers:'); cache_invalidate('admin_dash:')
    log_activity(current_user.id, 'co_admin_change',
                 f'{user["name"]} ist jetzt {"Co-Admin" if new_state else "kein Co-Admin mehr"}',
                 icon='⚡', color='gold')
    flash(f'{user["name"]} ist jetzt {"Co-Admin (kann Admin-Aktionen)" if new_state else "kein Co-Admin mehr"}.', 'success')
    return redirect(url_for('team'))


# === ADMIN: WELCOME-MAIL erneut senden ===
@app.route('/admin/team/<int:uid>/resend-welcome', methods=['POST'])
@login_required
def admin_resend_welcome(uid):
    """Welcome-Mail erneut senden + neues Initial-Passwort generieren."""
    if not current_user.has_admin_access:
        return redirect(url_for('team'))
    if not is_smtp_configured():
        flash('SMTP nicht konfiguriert — bitte erst E-Mail-Server einrichten', 'error')
        return redirect(url_for('team'))

    new_pw = generate_random_password()
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
    if not user:
        db.close()
        return redirect(url_for('team'))
    db.execute('UPDATE users SET password = ?, must_change_password = 1 WHERE id = ?',
               (hash_password(new_pw), uid))
    db.commit()
    db.close()

    ok, err = send_welcome_email(user['email'], user['name'], new_pw,
                                  sender_name=current_user.name)
    if ok:
        flash(f'✅ Welcome-E-Mail verschickt an {user["email"]}. Neues Passwort: {new_pw}', 'success')
    else:
        flash(f'❌ E-Mail-Versand fehlgeschlagen: {(err or "")[:100]}. Neues Passwort: {new_pw}', 'error')
    return redirect(url_for('team'))


# === ADMIN: PASSWORT-RESET ===
@app.route('/admin/team/<int:uid>/reset-password', methods=['POST'])
@login_required
def admin_reset_password(uid):
    if not current_user.has_admin_access:
        return redirect(url_for('dashboard'))
    new_pw = generate_random_password()
    db = get_db()
    db.execute('UPDATE users SET password = ? WHERE id = ?', (hash_password(new_pw), uid))
    user = db.execute('SELECT name, email FROM users WHERE id = ?', (uid,)).fetchone()
    db.commit()
    db.close()
    flash(f'Passwort zurückgesetzt für {user["name"]} ({user["email"]}). Neues Passwort: {new_pw}', 'success')
    return redirect(url_for('team'))


# === ADMIN: KI-EINSTELLUNGEN (Anthropic API) ===
@app.route('/admin/ki-settings', methods=['GET', 'POST'])
@login_required
def admin_ki_settings():
    if current_user.role != 'admin':
        flash('Nur Hauptadmin', 'error')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        new_key = (request.form.get('anthropic_api_key') or '').strip()
        if new_key:
            set_setting('anthropic_api_key', new_key)
            cache_invalidate('ai:')
            flash('KI-API-Key gespeichert! Du kannst jetzt Claude-Calls machen.', 'success')
        return redirect(url_for('admin_ki_settings'))

    has_key = bool(get_setting('anthropic_api_key'))
    test_result = None
    if request.args.get('test'):
        text, err = claude_chat('Antworte mit genau einem deutschen Satz: "KI-Verbindung funktioniert!" und nichts anderes.', max_tokens=50)
        test_result = {'ok': bool(text and not err), 'text': text or err or 'Keine Antwort'}

    return render_template('admin_ki_settings.html', has_key=has_key, test_result=test_result)


# === ADMIN: SMTP + E-MAIL ===
@app.route('/weiterbildung')
@login_required
def weiterbildung():
    """Leitet zur konfigurierten Weiterbildungs-URL (Learning Suite) weiter."""
    url = (get_setting('learning_suite_url') or '').strip()
    name = (get_setting('learning_suite_name') or 'Learning Suite').strip()
    return render_template('weiterbildung.html', url=url, name=name,
                          is_admin=current_user.role == 'admin')


@app.route('/admin/weiterbildung', methods=['GET', 'POST'])
@login_required
def admin_weiterbildung():
    """Admin: Weiterbildungs-URL konfigurieren."""
    if current_user.role != 'admin':
        flash('Nur Hauptadmin', 'error')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        url = (request.form.get('url') or '').strip()
        name = (request.form.get('name') or 'Learning Suite').strip()
        # URL absichern: muss mit http:// oder https:// beginnen
        if url and not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        set_setting('learning_suite_url', url)
        set_setting('learning_suite_name', name)
        flash(f'✅ Weiterbildungs-Link gespeichert: {url or "(leer)"}', 'success')
        return redirect(url_for('admin_weiterbildung'))

    return render_template('admin_weiterbildung.html',
        url=get_setting('learning_suite_url'),
        name=get_setting('learning_suite_name', 'Learning Suite'))


@app.route('/admin/email-settings', methods=['GET', 'POST'])
@login_required
def admin_email_settings():
    if current_user.role != 'admin':
        flash('Nur Hauptadmin kann E-Mail-Server konfigurieren', 'error')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        # SMTP-Settings speichern
        for key in ['smtp_host', 'smtp_port', 'smtp_user', 'smtp_from_name', 'smtp_from_email']:
            if key in request.form:
                set_setting(key, request.form.get(key, '').strip())
        # Passwort nur überschreiben wenn nicht leer
        new_pw = (request.form.get('smtp_password') or '').strip()
        if new_pw:
            set_setting('smtp_password', new_pw)
        flash('SMTP-Einstellungen gespeichert!', 'success')
        return redirect(url_for('admin_email_settings'))

    settings = {
        'smtp_host': get_setting('smtp_host'),
        'smtp_port': get_setting('smtp_port', '587'),
        'smtp_user': get_setting('smtp_user'),
        'smtp_from_name': get_setting('smtp_from_name', 'Pro Academy'),
        'smtp_from_email': get_setting('smtp_from_email'),
        'has_password': bool(get_setting('smtp_password')),
    }

    db = get_db()
    log = db.execute('''
        SELECT el.*, u.name as sent_by_name
        FROM email_log el
        LEFT JOIN users u ON el.sent_by = u.id
        ORDER BY el.sent_at DESC LIMIT 30
    ''').fetchall()
    db.close()

    return render_template('admin_email_settings.html', settings=settings, log=log,
                          configured=is_smtp_configured())


@app.route('/admin/email-test', methods=['POST'])
@login_required
def admin_email_test():
    if current_user.role != 'admin':
        return redirect(url_for('dashboard'))
    to_email = (request.form.get('to') or '').strip()
    if not to_email:
        flash('Bitte E-Mail-Adresse angeben', 'error')
        return redirect(url_for('admin_email_settings'))
    ok, err = send_email(to_email,
                         'Test-E-Mail von Pro Academy',
                         f'Hallo!\n\nDies ist eine Test-E-Mail von deinem Control Hub.\nWenn du das siehst, ist alles richtig konfiguriert.\n\nGesendet: {datetime.now().strftime("%d.%m.%Y %H:%M")}',
                         body_html=f'<h2 style="color:#0f1c3f">Test erfolgreich</h2><p>Dies ist eine Test-E-Mail von deinem <strong>Pro Academy Control Hub</strong>.</p><p>Wenn du das siehst, ist alles richtig konfiguriert.</p><p style="color:#94a3b8;font-size:12px">Gesendet: {datetime.now().strftime("%d.%m.%Y %H:%M")}</p>',
                         sent_by=current_user.id, category='admin_test')
    if ok:
        flash(f'✅ Test-E-Mail erfolgreich an {to_email} gesendet!', 'success')
    else:
        flash(f'❌ Versand fehlgeschlagen: {err}', 'error')
    return redirect(url_for('admin_email_settings'))


@app.route('/admin/eingabe-reminder-now', methods=['POST'])
@login_required
def admin_eingabe_reminder_now():
    """Manuelle Trigger für den Eingabeschluss-Reminder (auch außerhalb 3-Tage-Fenster)."""
    if not current_user.has_admin_access:
        return redirect(url_for('dashboard'))
    if not is_smtp_configured():
        flash('SMTP nicht konfiguriert', 'error')
        return redirect(url_for('admin_mail'))

    deadlines = get_production_deadlines()
    if not deadlines:
        flash('Keine Eingabeschluss-Daten', 'error')
        return redirect(url_for('admin_mail'))

    db = get_db()
    rows = db.execute('''
        SELECT u.id, u.name, u.email,
               SUM(CASE WHEN c.status = 'offen' THEN 1 ELSE 0 END) as open_count,
               SUM(CASE WHEN c.recherche_status IN ('ausstehend','') AND c.einheiten > 0 THEN 1 ELSE 0 END) as pending_research
        FROM users u JOIN contracts c ON c.owner_id = u.id
        WHERE u.active = 1 AND (c.status = 'offen' OR c.recherche_status IN ('ausstehend',''))
        GROUP BY u.id
        HAVING open_count > 0 OR pending_research > 0
    ''').fetchall()
    db.close()

    eingabe_str = deadlines['eingabeschluss_str']
    sent = 0
    for r in rows:
        total = (r['open_count'] or 0) + (r['pending_research'] or 0)
        first_name = r['name'].split()[0] if r['name'] else ''
        ok, _ = send_email(r['email'],
                          f'Eingabeschluss {eingabe_str} — {total} Vertrag{"" if total==1 else "äge"} klären',
                          f'Hi {first_name},\n\nbis Eingabeschluss am {eingabe_str} hast du noch {r["open_count"] or 0} offene Verträge und {r["pending_research"] or 0} hängende Recherchen.\n\nLogin: {CANONICAL_URL}\n\nCoach',
                          sent_by=current_user.id, category='reminder')
        if ok: sent += 1
    flash(f'✅ {sent} Reminder verschickt', 'success')
    return redirect(url_for('admin_mail'))


@app.route('/admin/mail', methods=['GET', 'POST'])
@login_required
def admin_mail():
    """Bulk-Mailer für Admin."""
    if not current_user.has_admin_access:
        flash('Keine Berechtigung', 'error')
        return redirect(url_for('dashboard'))

    if not is_smtp_configured():
        flash('Bitte zuerst SMTP konfigurieren', 'error')
        return redirect(url_for('admin_email_settings'))

    db = get_db()
    if request.method == 'POST':
        target = request.form.get('target', 'all')
        subject = (request.form.get('subject') or '').strip()
        body = (request.form.get('body') or '').strip()
        if not subject or not body:
            flash('Betreff und Nachricht erforderlich', 'error')
            db.close()
            return redirect(url_for('admin_mail'))

        # Empfänger ermitteln
        if target == 'all':
            recipients = [r['email'] for r in db.execute('SELECT email FROM users WHERE active = 1').fetchall()]
        elif target == 'inactive':
            recipients = [r['email'] for r in db.execute('SELECT email FROM users WHERE active = 1 AND (last_login IS NULL OR last_login < datetime("now", "-7 days"))').fetchall()]
        elif target == 'silent':
            recipients = [r['email'] for r in db.execute('SELECT email FROM users WHERE active = 1 AND (last_login IS NULL OR last_login < datetime("now", "-30 days"))').fetchall()]
        elif target.startswith('level_'):
            lvl = int(target.split('_')[1])
            recipients = [r['email'] for r in db.execute('SELECT email FROM users WHERE active = 1 AND manual_career_level = ?', (lvl,)).fetchall()]
        else:
            recipients = []

        # HTML-Body bauen
        html_body = f'''<!DOCTYPE html><html><body style="font-family:Inter,sans-serif;background:#f6f7fb;margin:0;padding:24px">
<table cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;background:#fff;border-radius:14px;border:1px solid #ebeef4">
<tr><td style="padding:24px 28px;background:#0f1c3f;border-radius:14px 14px 0 0;color:#fff">
<div style="font-size:20px;font-weight:800">Pro Academy</div>
<div style="font-size:11px;color:#d4a843;letter-spacing:1.5px;text-transform:uppercase;margin-top:2px">Control Hub</div>
</td></tr>
<tr><td style="padding:28px;color:#0f172a;line-height:1.6;font-size:15px">{body.replace(chr(10), '<br>')}</td></tr>
<tr><td style="padding:18px 28px;background:#fafbfc;color:#94a3b8;font-size:11px;border-top:1px solid #ebeef4;border-radius:0 0 14px 14px">
Diese Nachricht wurde von {current_user.name} versendet.
</td></tr></table></body></html>'''

        success, fails = send_bulk_emails(recipients, subject, body, html_body, sent_by=current_user.id)
        db.close()
        if fails:
            flash(f'✅ {success} gesendet, ❌ {len(fails)} fehlgeschlagen', 'info')
        else:
            flash(f'✅ {success} E-Mail(s) erfolgreich gesendet!', 'success')
        return redirect(url_for('admin_mail'))

    # GET: Empfänger-Counts
    counts = {
        'all': db.execute('SELECT COUNT(*) as c FROM users WHERE active = 1').fetchone()['c'],
        'inactive': db.execute('SELECT COUNT(*) as c FROM users WHERE active = 1 AND (last_login IS NULL OR last_login < datetime("now", "-7 days"))').fetchone()['c'],
        'silent': db.execute('SELECT COUNT(*) as c FROM users WHERE active = 1 AND (last_login IS NULL OR last_login < datetime("now", "-30 days"))').fetchone()['c'],
    }
    for cl in CAREER_LEVELS:
        counts[f'level_{cl["level"]}'] = db.execute('SELECT COUNT(*) as c FROM users WHERE active = 1 AND manual_career_level = ?', (cl['level'],)).fetchone()['c']
    db.close()
    return render_template('admin_mail.html', counts=counts, all_levels=CAREER_LEVELS)


# === ADMIN: BACKUP DOWNLOAD ===
@app.route('/admin/backup')
@login_required
def admin_backup():
    if not current_user.has_admin_access:
        return redirect(url_for('dashboard'))
    if not os.path.exists(DB_PATH):
        flash('Datenbank-Datei nicht gefunden!', 'error')
        return redirect(url_for('dashboard'))
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    return send_file(DB_PATH, as_attachment=True,
                     download_name=f'vertrieb-backup-{timestamp}.db',
                     mimetype='application/octet-stream')


# === ADMIN: AKTIVITÄT ===
@app.route('/admin/aktivitaet')
@login_required
def admin_aktivitaet():
    if not current_user.has_admin_access:
        flash('Keine Berechtigung', 'error')
        return redirect(url_for('dashboard'))
    db = get_db()
    rows = db.execute('''
        SELECT u.id, u.name, u.email, u.last_login, u.login_count, u.joined_date,
               u.manual_career_level, p.name as upline_name,
               COALESCE(SUM(c.einheiten), 0) as einheiten
        FROM users u
        LEFT JOIN users p ON u.parent_id = p.id
        LEFT JOIN contracts c ON c.owner_id = u.id AND c.status = "abgeschlossen" AND c.recherche_status = "freigegeben"
        WHERE u.active = 1
        GROUP BY u.id
        ORDER BY u.last_login DESC NULLS LAST, u.name
    ''').fetchall()
    members = []
    today_str = date.today().strftime('%Y-%m-%d')
    for r in rows:
        d = dict(r)
        d['career'] = career_for_row(r['manual_career_level'], r['einheiten'])
        # Tage seit letztem Login
        if r['last_login']:
            try:
                last_date = datetime.strptime(r['last_login'][:10], '%Y-%m-%d').date()
                d['days_ago'] = (date.today() - last_date).days
            except Exception:
                d['days_ago'] = None
        else:
            d['days_ago'] = None
        members.append(d)
    db.close()
    return render_template('admin_aktivitaet.html', members=members)


# === ADMIN: CSV-IMPORT ===
@app.route('/admin/import', methods=['GET', 'POST'])
@login_required
def admin_import():
    if not current_user.has_admin_access:
        flash('Keine Berechtigung', 'error')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        file = request.files.get('csv_file')
        if not file or file.filename == '':
            flash('Keine Datei ausgewählt', 'error')
            return redirect(url_for('admin_import'))

        try:
            content = file.read().decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                file.seek(0)
                content = file.read().decode('latin-1')
            except Exception:
                flash('Datei konnte nicht gelesen werden (Zeichensatz-Fehler)', 'error')
                return redirect(url_for('admin_import'))

        # Delimiter automatisch erkennen
        delimiter = ','
        first_line = content.split('\n')[0] if content else ''
        if first_line.count(';') > first_line.count(','):
            delimiter = ';'

        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
        # Spalten-Namen normalisieren
        if reader.fieldnames:
            reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]

        db = get_db()
        results = {'created': [], 'skipped': [], 'errors': []}

        for row_idx, row in enumerate(reader, start=2):
            try:
                name = (row.get('name') or '').strip()
                email = (row.get('email') or row.get('e-mail') or '').strip().lower()
                phone = (row.get('phone') or row.get('telefon') or '').strip()
                parent_email = (row.get('parent_email') or row.get('upline') or row.get('upline_email') or '').strip().lower()
                stufe_raw = (row.get('stufe') or row.get('manual_career_level') or row.get('career_level') or '1').strip()

                if not name or not email:
                    results['errors'].append(f'Zeile {row_idx}: Name oder E-Mail leer')
                    continue
                if '@' not in email:
                    results['errors'].append(f'Zeile {row_idx}: Ungültige E-Mail "{email}"')
                    continue

                existing = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
                if existing:
                    results['skipped'].append(f'{email} (existiert bereits)')
                    continue

                # Upline finden
                if parent_email:
                    parent = db.execute('SELECT id, level FROM users WHERE email = ?', (parent_email,)).fetchone()
                    if not parent:
                        results['errors'].append(f'Zeile {row_idx}: Upline "{parent_email}" nicht gefunden')
                        continue
                    parent_id = parent['id']
                    new_level = parent['level'] + 1
                else:
                    # Default: Najib (Admin) als Upline
                    najib = db.execute('SELECT id, level FROM users WHERE email = ?', ('najib@ntpro.de',)).fetchone()
                    parent_id = najib['id'] if najib else None
                    new_level = (najib['level'] + 1) if najib else 1

                try:
                    stufe = int(stufe_raw)
                    if stufe < 1 or stufe > 6:
                        stufe = 1
                except (ValueError, TypeError):
                    stufe = 1

                generated_pw = generate_random_password()
                db.execute('''INSERT INTO users (name, email, password, role, parent_id, level, phone, manual_career_level)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                           (name, email, hash_password(generated_pw), 'partner',
                            parent_id, new_level, phone, stufe))
                results['created'].append({'name': name, 'email': email, 'password': generated_pw, 'stufe': stufe})
            except Exception as e:
                results['errors'].append(f'Zeile {row_idx}: {str(e)}')

        db.commit()
        db.close()
        return render_template('admin_import_result.html', results=results)

    return render_template('admin_import.html')


@app.route('/admin/import/template')
@login_required
def admin_import_template():
    if not current_user.has_admin_access:
        return redirect(url_for('dashboard'))
    template = (
        'name,email,phone,parent_email,stufe\n'
        'Max Mustermann,max@email.de,+49 170 1234567,najib@ntpro.de,1\n'
        'Anna Schmidt,anna@email.de,+49 170 7654321,najib@ntpro.de,2\n'
        'Tom Weber,tom@email.de,+49 170 1122334,max@email.de,1\n'
    )
    return Response(
        template,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment;filename=partner-import-vorlage.csv'}
    )


# === GENEHMIGUNGEN (Admin) ===
@app.route('/admin/genehmigungen')
@login_required
def admin_genehmigungen():
    if not current_user.has_admin_access:
        flash('Keine Berechtigung', 'error')
        return redirect(url_for('dashboard'))
    db = get_db()
    rows = db.execute('''
        SELECT u.id, u.name, u.email, u.manual_career_level, u.pending_career_level,
               u.pending_at, p.name as proposed_by, up.name as upline_name
        FROM users u
        LEFT JOIN users p ON u.pending_by_user_id = p.id
        LEFT JOIN users up ON u.parent_id = up.id
        WHERE u.pending_career_level IS NOT NULL AND u.active = 1
        ORDER BY u.pending_at DESC
    ''').fetchall()
    pending = []
    for r in rows:
        d = dict(r)
        d['current_career'] = next((c for c in CAREER_LEVELS if c['level'] == r['manual_career_level']), CAREER_LEVELS[0])
        d['proposed_career'] = next((c for c in CAREER_LEVELS if c['level'] == r['pending_career_level']), CAREER_LEVELS[0])
        pending.append(d)
    db.close()
    return render_template('genehmigungen.html', pending=pending)


@app.route('/admin/genehmigungen/<int:uid>/approve', methods=['POST'])
@login_required
def genehmigung_approve(uid):
    if not current_user.has_admin_access:
        return redirect(url_for('dashboard'))
    db = get_db()
    user = db.execute('SELECT pending_career_level FROM users WHERE id = ?', (uid,)).fetchone()
    if user and user['pending_career_level']:
        new_lvl = user['pending_career_level']
        db.execute('''UPDATE users SET manual_career_level = ?,
                      pending_career_level = NULL, pending_by_user_id = NULL, pending_at = NULL
                      WHERE id = ?''', (new_lvl, uid))
        u_info = db.execute('SELECT name FROM users WHERE id = ?', (uid,)).fetchone()
        db.commit()
        new_career = next((cl for cl in CAREER_LEVELS if cl['level'] == new_lvl), None)
        if new_career and u_info:
            log_activity(uid, 'befoerderung',
                f'{u_info["name"]} wurde zu {new_career["short"]} befördert! 🚀',
                icon='⬆️', color='gold')
    db.close()
    recalculate_all_commissions()
    flash('Stufe bestätigt!', 'success')
    return redirect(url_for('admin_genehmigungen'))


@app.route('/admin/genehmigungen/<int:uid>/reject', methods=['POST'])
@login_required
def genehmigung_reject(uid):
    if not current_user.has_admin_access:
        return redirect(url_for('dashboard'))
    db = get_db()
    db.execute('''UPDATE users SET pending_career_level = NULL,
                  pending_by_user_id = NULL, pending_at = NULL WHERE id = ?''', (uid,))
    db.commit()
    db.close()
    flash('Stufen-Antrag abgelehnt', 'info')
    return redirect(url_for('admin_genehmigungen'))


def init_db():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'berater',
            parent_id INTEGER,
            level INTEGER DEFAULT 1,
            phone TEXT,
            joined_date TEXT DEFAULT CURRENT_DATE,
            manual_career_level INTEGER DEFAULT 1,
            pending_career_level INTEGER,
            pending_by_user_id INTEGER,
            pending_at TEXT,
            onboarding_endgespraech INTEGER DEFAULT 0,
            onboarding_einarbeitung_1 INTEGER DEFAULT 0,
            onboarding_einarbeitung_2 INTEGER DEFAULT 0,
            onboarding_einarbeitung_3 INTEGER DEFAULT 0,
            onboarding_seminar_bezahlt INTEGER DEFAULT 0,
            vision TEXT DEFAULT '',
            last_login TEXT,
            login_count INTEGER DEFAULT 0,
            birthday TEXT,
            onboarding_done INTEGER DEFAULT 0,
            is_co_admin INTEGER DEFAULT 0,
            must_change_password INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            FOREIGN KEY (parent_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            birthday TEXT,
            produkt TEXT,
            status TEXT DEFAULT 'neu',
            notizen TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            client_name TEXT NOT NULL,
            produkt TEXT NOT NULL,
            volumen REAL DEFAULT 0,
            einheiten REAL DEFAULT 0,
            provision REAL DEFAULT 0,
            status TEXT DEFAULT 'offen',
            abschluss_date TEXT,
            notizen TEXT,
            recherche_done INTEGER DEFAULT 0,
            telefonat_done INTEGER DEFAULT 0,
            unterlagen_done INTEGER DEFAULT 0,
            nachweise_done INTEGER DEFAULT 0,
            unterschrieben INTEGER DEFAULT 0,
            freizeichnung_done INTEGER DEFAULT 0,
            recherche_status TEXT DEFAULT 'ausstehend',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            client_name TEXT,
            termin_date TEXT NOT NULL,
            termin_time TEXT,
            typ TEXT DEFAULT 'kundentermin',
            status TEXT DEFAULT 'geplant',
            notizen TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS quotas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            monat TEXT NOT NULL,
            ziel_einheiten REAL DEFAULT 0,
            ziel_vertraege INTEGER DEFAULT 0,
            ziel_partner INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS commissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            eh REAL NOT NULL,
            rate_diff REAL NOT NULL,
            amount REAL NOT NULL,
            career_level INTEGER NOT NULL,
            is_own INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (contract_id) REFERENCES contracts(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS daily_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            sort_order INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            datum TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            done_at TEXT,
            UNIQUE(user_id, task_id, datum),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (task_id) REFERENCES daily_tasks(id)
        );

        CREATE TABLE IF NOT EXISTS personal_todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            done_at TEXT,
            datum TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS onboarding_roadmap (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            day_number INTEGER NOT NULL,
            task_code TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(user_id, day_number, task_code),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS webhook_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            label TEXT,
            list_typ TEXT DEFAULT 'vk',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_used TEXT,
            request_count INTEGER DEFAULT 0,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS content_profile (
            user_id INTEGER PRIMARY KEY,
            antworten_json TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS patch_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT,
            title TEXT NOT NULL,
            summary TEXT,
            body_md TEXT,
            kategorie TEXT DEFAULT 'feature',
            published_at TEXT DEFAULT CURRENT_TIMESTAMP,
            pushed INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS patch_notes_seen (
            user_id INTEGER NOT NULL,
            patch_id INTEGER NOT NULL,
            seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, patch_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (patch_id) REFERENCES patch_notes(id)
        );

        CREATE TABLE IF NOT EXISTS daily_checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            datum TEXT NOT NULL,
            anrufe INTEGER DEFAULT 0,
            termine INTEGER DEFAULT 0,
            notiz TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, datum),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS deploy_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha TEXT,
            message TEXT,
            status TEXT NOT NULL DEFAULT 'ok',
            output TEXT,
            triggered_by TEXT DEFAULT 'webhook',
            deployed_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS newsletter_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kategorie TEXT NOT NULL,
            titel TEXT NOT NULL,
            zusammenfassung TEXT,
            quelle TEXT,
            quelle_url TEXT,
            relevanz INTEGER DEFAULT 5,
            published_at TEXT,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
            seen_count INTEGER DEFAULT 0,
            pushed INTEGER DEFAULT 0,
            UNIQUE(quelle_url)
        );

        CREATE TABLE IF NOT EXISTS newsletter_last_seen (
            user_id INTEGER PRIMARY KEY,
            last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS magic_link_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            ip TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            method TEXT NOT NULL DEFAULT 'email',
            sms_code TEXT,
            sms_attempts INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            ip TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS vision_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            datum TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, datum),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS push_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            push_type TEXT NOT NULL,
            ref_key TEXT,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, push_type, ref_key)
        );

        CREATE TABLE IF NOT EXISTS daily_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            datum TEXT NOT NULL,
            kontext TEXT,
            psyche_output TEXT,
            hardcore_output TEXT,
            chairman_output TEXT,
            target_partner_id INTEGER,
            target_partner_name TEXT,
            done_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_actions_user_date ON daily_actions(user_id, datum DESC);

        CREATE TABLE IF NOT EXISTS content_ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            datum TEXT NOT NULL,
            kontext TEXT,
            agent_typ TEXT NOT NULL,    -- 'partner' | 'reichweite'
            content_type TEXT,           -- bei reichweite: hot_take/how_to/bts/trend/mistake/listicle/story
            hook TEXT,
            storyline TEXT,
            cta_or_caption TEXT,
            mechanik TEXT,
            full_output TEXT,
            used_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_content_ideas_user_date ON content_ideas(user_id, datum DESC);

        CREATE TABLE IF NOT EXISTS recent_partner_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visitor_id INTEGER NOT NULL,
            viewed_user_id INTEGER NOT NULL,
            viewed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(visitor_id, viewed_user_id),
            FOREIGN KEY (visitor_id) REFERENCES users(id),
            FOREIGN KEY (viewed_user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            user_agent TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_used TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS placeholder_structures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            est_eh REAL DEFAULT 0,
            partner_count INTEGER DEFAULT 0,
            notes TEXT,
            linked_user_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id),
            FOREIGN KEY (linked_user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS partner_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kategorie TEXT NOT NULL,
            titel TEXT NOT NULL,
            details TEXT,
            status TEXT DEFAULT 'offen',
            admin_response TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            icon TEXT DEFAULT '•',
            color TEXT DEFAULT 'navy',
            metadata TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS weekly_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            week_start TEXT NOT NULL,
            ziel_termine INTEGER DEFAULT 0,
            ziel_vertraege INTEGER DEFAULT 0,
            ziel_einheiten REAL DEFAULT 0,
            ziel_neue_partner INTEGER DEFAULT 0,
            ziel_anrufe INTEGER DEFAULT 0,
            UNIQUE(user_id, week_start),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS coaching_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_user_id INTEGER NOT NULL,
            author_user_id INTEGER NOT NULL,
            note TEXT NOT NULL,
            next_session_date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (target_user_id) REFERENCES users(id),
            FOREIGN KEY (author_user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS user_achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            achievement_code TEXT NOT NULL,
            unlocked_at TEXT DEFAULT CURRENT_TIMESTAMP,
            seen INTEGER DEFAULT 0,
            UNIQUE(user_id, achievement_code),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS email_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
            sent_by INTEGER,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            FOREIGN KEY (sent_by) REFERENCES users(id)
        );
    ''')

    # === Performance: DB-Indexes ===
    try:
        db.executescript('''
            CREATE INDEX IF NOT EXISTS idx_users_parent ON users(parent_id);
            CREATE INDEX IF NOT EXISTS idx_users_active ON users(active);
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_contracts_owner ON contracts(owner_id);
            CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts(status, recherche_status);
            CREATE INDEX IF NOT EXISTS idx_contracts_owner_status ON contracts(owner_id, status, recherche_status);
            CREATE INDEX IF NOT EXISTS idx_contracts_abschluss_date ON contracts(abschluss_date);
            CREATE INDEX IF NOT EXISTS idx_leads_owner ON leads(owner_id);
            CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            CREATE INDEX IF NOT EXISTS idx_appointments_owner ON appointments(owner_id);
            CREATE INDEX IF NOT EXISTS idx_appointments_date ON appointments(termin_date);
            CREATE INDEX IF NOT EXISTS idx_commissions_user ON commissions(user_id);
            CREATE INDEX IF NOT EXISTS idx_commissions_contract ON commissions(contract_id);
            CREATE INDEX IF NOT EXISTS idx_activity_user_date ON activity_log(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_user_achievements ON user_achievements(user_id);
            CREATE INDEX IF NOT EXISTS idx_user_tasks ON user_tasks(user_id, datum);
            CREATE INDEX IF NOT EXISTS idx_contracts_owner ON contracts(owner_id, status, recherche_status);
            CREATE INDEX IF NOT EXISTS idx_contracts_abschluss ON contracts(abschluss_date);
            CREATE INDEX IF NOT EXISTS idx_appointments_owner_date ON appointments(owner_id, termin_date);
            CREATE INDEX IF NOT EXISTS idx_leads_owner ON leads(owner_id, status);
            CREATE INDEX IF NOT EXISTS idx_leads_typ ON leads(owner_id, liste_typ);
            CREATE INDEX IF NOT EXISTS idx_users_parent ON users(parent_id, active);
            CREATE INDEX IF NOT EXISTS idx_commissions_user ON commissions(user_id);
            CREATE INDEX IF NOT EXISTS idx_push_log_lookup ON push_log(user_id, push_type, ref_key);
            CREATE INDEX IF NOT EXISTS idx_vision_user_date ON vision_entries(user_id, datum);
            -- Lead-Token (per-Partner Werbe-URLs) + Self-Service-Reset + Newsletter
            CREATE INDEX IF NOT EXISTS idx_users_lead_token ON users(lead_token) WHERE lead_token IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token);
            CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets(user_id, used_at);
            CREATE INDEX IF NOT EXISTS idx_newsletter_kat_pub ON newsletter_items(kategorie, published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_deploy_log_date ON deploy_log(deployed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_daily_checkins_user_date ON daily_checkins(user_id, datum DESC);
        ''')
    except Exception as e:
        print(f"Index creation warning: {e}")

    # SQLite Performance: WAL mode + Foreign Keys
    try:
        db.execute('PRAGMA journal_mode = WAL')
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('PRAGMA cache_size = -10000')  # 10 MB Cache
    except Exception as e:
        print(f"PRAGMA warning: {e}")

    # Migration: neue Spalten für bestehende DBs nachrüsten
    try:
        # users
        cols = db.execute("PRAGMA table_info(users)").fetchall()
        col_names = [c['name'] for c in cols]
        for new_col, sql_type in [
            ('vision', "TEXT DEFAULT ''"),
            ('last_login', "TEXT"),
            ('login_count', "INTEGER DEFAULT 0"),
            ('birthday', "TEXT"),
            ('onboarding_done', "INTEGER DEFAULT 0"),
            ('is_co_admin', "INTEGER DEFAULT 0"),
            ('must_change_password', "INTEGER DEFAULT 0"),
            ('language', "TEXT DEFAULT 'de'"),
            ('initial_eh', "REAL DEFAULT 0"),
            ('catchup_done', "INTEGER DEFAULT 0"),
            ('photo_path', "TEXT"),
            ('advanced_mode', "INTEGER DEFAULT 0"),
            ('push_prefs', "TEXT DEFAULT '{}'"),
            ('streak_days', "INTEGER DEFAULT 0"),
            ('streak_last_date', "TEXT"),
            ('instagram_handle', "TEXT"),
            ('tiktok_handle', "TEXT"),
            ('lead_token', "TEXT"),  # eindeutiger Lead-Link-Token (?ref=<token>)
        ]:
            if new_col not in col_names:
                db.execute(f"ALTER TABLE users ADD COLUMN {new_col} {sql_type}")

        # appointments: attendee_ids (JSON-Liste mit User-IDs für Multi-Partner-Termine)
        appt_cols = db.execute("PRAGMA table_info(appointments)").fetchall()
        appt_col_names = [c['name'] for c in appt_cols]
        if 'attendee_ids' not in appt_col_names:
            db.execute("ALTER TABLE appointments ADD COLUMN attendee_ids TEXT")
        if 'duration_min' not in appt_col_names:
            db.execute("ALTER TABLE appointments ADD COLUMN duration_min INTEGER DEFAULT 60")

        # contracts: kunde_birthday + lead_id für Koppelung mit Namensliste
        contract_cols = db.execute("PRAGMA table_info(contracts)").fetchall()
        contract_col_names = [c['name'] for c in contract_cols]
        for new_col, sql_type in [
            ('kunde_birthday', "TEXT"),
            ('lead_id', "INTEGER"),
        ]:
            if new_col not in contract_col_names:
                db.execute(f"ALTER TABLE contracts ADD COLUMN {new_col} {sql_type}")

        # leads
        lead_cols = db.execute("PRAGMA table_info(leads)").fetchall()
        lead_col_names = [c['name'] for c in lead_cols]
        if 'liste_typ' not in lead_col_names:
            db.execute("ALTER TABLE leads ADD COLUMN liste_typ TEXT DEFAULT 'vk'")
        if 'kontaktiert_at' not in lead_col_names:
            db.execute("ALTER TABLE leads ADD COLUMN kontaktiert_at TEXT")
        if 'birthday' not in lead_col_names:
            db.execute("ALTER TABLE leads ADD COLUMN birthday TEXT")
        if 'source' not in lead_col_names:
            db.execute("ALTER TABLE leads ADD COLUMN source TEXT DEFAULT 'manual'")
        if 'public_message' not in lead_col_names:
            db.execute("ALTER TABLE leads ADD COLUMN public_message TEXT")
        if 'referred_by' not in lead_col_names:
            db.execute("ALTER TABLE leads ADD COLUMN referred_by TEXT")
        db.commit()
    except Exception as e:
        print(f"Migration warning: {e}")

    admin = db.execute('SELECT id FROM users WHERE email = ?', ('najib@ntpro.de',)).fetchone()
    if not admin:
        db.execute('''
            INSERT INTO users (name, email, password, role, level, manual_career_level)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', ('Najib Tchatikpi', 'najib@ntpro.de', hash_password('admin123'), 'admin', 1, 5))
        db.commit()
    else:
        # Sicherstellen dass Najib mind. Stufe 5 hat
        db.execute('UPDATE users SET manual_career_level = MAX(COALESCE(manual_career_level, 1), 5) WHERE email = ?', ('najib@ntpro.de',))
        db.commit()

    db.close()


def get_all_descendants(user_id):
    db = get_db()
    all_ids = []
    queue = [user_id]
    visited = set()
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        children = db.execute('SELECT id FROM users WHERE parent_id = ? AND active = 1', (current,)).fetchall()
        for child in children:
            all_ids.append(child['id'])
            queue.append(child['id'])
    db.close()
    return all_ids


def calculate_commissions_for_contract(contract_id):
    """Differenz-Provisionssystem: Jeder bekommt die Differenz seiner Stufe
    zur höchsten bisher gesehenen Stufe in der Kette darunter."""
    db = get_db()
    contract = db.execute('SELECT * FROM contracts WHERE id = ?', (contract_id,)).fetchone()
    db.execute('DELETE FROM commissions WHERE contract_id = ?', (contract_id,))

    if (not contract or contract['status'] != 'abgeschlossen'
        or contract['einheiten'] <= 0
        or (contract['recherche_status'] or 'ausstehend') != 'freigegeben'):
        db.commit()
        db.close()
        return

    eh = contract['einheiten']
    chain = []
    current_id = contract['owner_id']
    is_own = True
    while current_id:
        user = db.execute('SELECT * FROM users WHERE id = ? AND active = 1', (current_id,)).fetchone()
        if not user:
            break
        own_eh = db.execute(
            'SELECT COALESCE(SUM(einheiten), 0) as s FROM contracts WHERE owner_id = ? AND status = "abgeschlossen" AND recherche_status = "freigegeben"',
            (user['id'],)
        ).fetchone()['s']
        earned = 1
        for cl in CAREER_LEVELS:
            if own_eh >= cl['min_eh']:
                earned = cl['level']
            else:
                break
        manual = user['manual_career_level'] or 1
        final_level = max(manual, earned)
        career = next((c for c in CAREER_LEVELS if c['level'] == final_level), CAREER_LEVELS[0])
        chain.append({
            'user_id': user['id'],
            'level': career['level'],
            'rate': career['commission'],
            'is_own': is_own
        })
        is_own = False
        current_id = user['parent_id']

    highest_rate_so_far = 0.0
    for link in chain:
        diff = link['rate'] - highest_rate_so_far
        if diff > 0:
            amount = eh * diff
            db.execute('''INSERT INTO commissions
                (contract_id, user_id, eh, rate_diff, amount, career_level, is_own)
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (contract_id, link['user_id'], eh, diff, amount, link['level'], 1 if link['is_own'] else 0))
            highest_rate_so_far = link['rate']

    db.commit()
    db.close()


def recalculate_all_commissions():
    """Berechnet alle Provisionen neu - z.B. wenn sich Karriere-Stufen ändern."""
    db = get_db()
    contracts = db.execute('SELECT id FROM contracts WHERE status = "abgeschlossen" AND recherche_status = "freigegeben"').fetchall()
    db.close()
    for c in contracts:
        calculate_commissions_for_contract(c['id'])


def get_commissions_for_user(user_id, only_own=False):
    """Provisionen eines Users (alle aus seiner gesamten Up-/Downline-Kette)"""
    db = get_db()
    if only_own:
        rows = db.execute('SELECT * FROM commissions WHERE user_id = ? AND is_own = 1', (user_id,)).fetchall()
    else:
        rows = db.execute('SELECT * FROM commissions WHERE user_id = ?', (user_id,)).fetchall()
    db.close()
    total = sum(r['amount'] for r in rows)
    own_total = sum(r['amount'] for r in rows if r['is_own'])
    diff_total = sum(r['amount'] for r in rows if not r['is_own'])
    return {
        'total': total, 'own': own_total, 'differenz': diff_total,
        'count': len(rows), 'rows': rows
    }


def get_struktur_news(user_id, days=7, limit=8):
    """Cached für 5 Min — News ändern sich nicht im Sekundentakt"""
    ckey = f'news:{user_id}:{days}:{limit}'
    cached_v = cache_get(ckey)
    if cached_v is not None:
        return cached_v
    try:
        result = _get_struktur_news_uncached(user_id, days, limit)
    except Exception as ex:
        print(f"[get_struktur_news] failed: {ex}")
        result = None
    cache_set(ckey, result, ttl=1800)
    return result


def _get_struktur_news_uncached(user_id, days=7, limit=8):
    """Highlights aus der Downline der letzten X Tage: Promotions, Trophäen, viele Termine, große Abschlüsse.
    Returns: list of {icon, color, title, detail, who_id, who_name, time, score}
    Score = Wichtigkeit für Sortierung."""
    db = get_db()
    descendants = get_all_descendants(user_id)
    if not descendants:
        db.close()
        return []
    ph = ','.join('?' * len(descendants))
    items = []

    # 1) TROPHÄEN — Achievements der letzten X Tage
    try:
        trophy_rows = db.execute(f'''
            SELECT ua.achievement_code as code, ua.unlocked_at, u.id as uid, u.name, u.photo_path
            FROM user_achievements ua
            JOIN users u ON ua.user_id = u.id
            WHERE u.id IN ({ph}) AND date(ua.unlocked_at) >= date('now', '-{days} days')
            ORDER BY ua.unlocked_at DESC
        ''', descendants).fetchall()
        for t in trophy_rows:
            items.append({
                'icon': '★', 'color': '#d4a843',
                'title': f"{t['name']} hat eine Trophäe erreicht",
                'detail': f"Achievement: {t['code']}",
                'who_id': t['uid'], 'who_name': t['name'], 'who_photo': t['photo_path'],
                'time': t['unlocked_at'], 'score': 80,
                'url': f"/partner/{t['uid']}/profil"
            })
    except Exception:
        pass

    # 2) STUFEN-AUFSTIEG — Beförderungen via activity_log
    try:
        promo_rows = db.execute(f'''
            SELECT a.message, a.created_at, a.metadata, u.id as uid, u.name, u.photo_path
            FROM activity_log a
            JOIN users u ON a.user_id = u.id
            WHERE u.id IN ({ph}) AND a.event_type IN ('career_promo', 'beförderung', 'pending_approved')
              AND date(a.created_at) >= date('now', '-{days} days')
            ORDER BY a.created_at DESC
        ''', descendants).fetchall()
        for p in promo_rows:
            items.append({
                'icon': '⬆', 'color': '#10b981',
                'title': f"{p['name']} wurde befördert",
                'detail': p['message'][:100] if p['message'] else 'Neue Karriere-Stufe erreicht',
                'who_id': p['uid'], 'who_name': p['name'], 'who_photo': p['photo_path'],
                'time': p['created_at'], 'score': 100,
                'url': f"/partner/{p['uid']}/profil"
            })
    except Exception:
        pass

    # 3) GROSSE ABSCHLÜSSE der letzten Tage (>= 50 EH einzeln)
    try:
        big_contracts = db.execute(f'''
            SELECT c.client_name, c.einheiten, c.created_at, u.id as uid, u.name, u.photo_path
            FROM contracts c
            JOIN users u ON c.owner_id = u.id
            WHERE u.id IN ({ph}) AND c.status='abgeschlossen' AND c.recherche_status='freigegeben'
              AND date(c.abschluss_date) >= date('now', '-{days} days')
              AND c.einheiten >= 50
            ORDER BY c.einheiten DESC LIMIT 8
        ''', descendants).fetchall()
        for b in big_contracts:
            items.append({
                'icon': '€', 'color': '#a855f7',
                'title': f"{b['name']}: großer Abschluss!",
                'detail': f"{b['client_name']} · {int(b['einheiten'])} EH",
                'who_id': b['uid'], 'who_name': b['name'], 'who_photo': b['photo_path'],
                'time': b['created_at'], 'score': 90 + min(int(b['einheiten']) // 10, 20),
                'url': f"/partner/{b['uid']}/profil"
            })
    except Exception:
        pass

    # 4) FLEISSIGE TERMIN-MACHER (>= 8 Termine in Woche)
    try:
        active_rows = db.execute(f'''
            SELECT u.id as uid, u.name, u.photo_path, COUNT(a.id) as cnt
            FROM users u
            LEFT JOIN appointments a ON a.owner_id = u.id AND date(a.termin_date) >= date('now', '-{days} days')
            WHERE u.id IN ({ph})
            GROUP BY u.id
            HAVING cnt >= 8
            ORDER BY cnt DESC LIMIT 5
        ''', descendants).fetchall()
        for r in active_rows:
            items.append({
                'icon': '◷', 'color': '#3b82f6',
                'title': f"{r['name']}: starke Termin-Woche",
                'detail': f"{r['cnt']} Termine in {days} Tagen — Pipeline läuft!",
                'who_id': r['uid'], 'who_name': r['name'], 'who_photo': r['photo_path'],
                'time': None, 'score': 60 + min(r['cnt'], 30),
                'url': f"/partner/{r['uid']}/profil"
            })
    except Exception:
        pass

    # 5) NEUE PARTNER (innerhalb des Zeitfensters in der eigenen Struktur)
    try:
        new_partners = db.execute(f'''
            SELECT a.message, a.created_at, u.id as uid, u.name, u.photo_path
            FROM activity_log a
            JOIN users u ON a.user_id = u.id
            WHERE u.id IN ({ph}) AND a.event_type='partner_neu'
              AND date(a.created_at) >= date('now', '-{days} days')
            ORDER BY a.created_at DESC LIMIT 5
        ''', descendants).fetchall()
        for n in new_partners:
            items.append({
                'icon': '◈', 'color': '#10b981',
                'title': f"Neuer Partner: {n['name']}",
                'detail': 'Willkommen in der Familie!',
                'who_id': n['uid'], 'who_name': n['name'], 'who_photo': n['photo_path'],
                'time': n['created_at'], 'score': 70,
                'url': f"/partner/{n['uid']}/profil"
            })
    except Exception:
        pass

    db.close()
    # Nach Score sortieren, dann nach Zeit
    items.sort(key=lambda x: (-x.get('score', 0), -(int(x['time'][:4] + x['time'][5:7] + x['time'][8:10]) if x.get('time') else 0)))
    return items[:limit]


def get_strang_status(user_id):
    """Cached für 2 Min — robust gegen Crashes."""
    ckey = f'strang:{user_id}'
    cached_v = cache_get(ckey)
    if cached_v is not None:
        return cached_v
    try:
        result = _get_strang_status_uncached(user_id)
    except Exception as e:
        print(f'[strang] failed: {e}')
        result = None
    cache_set(ckey, result, ttl=1800)
    return result


def _get_strang_status_uncached(user_id):
    """Strang-Status für Dashboard: bin ich auf Kurs für die nächste Stufe?
    Returns: dict mit straenge, qualifizierte, benötigt, next_level."""
    db = get_db()
    user = db.execute('SELECT manual_career_level FROM users WHERE id=? AND active=1', (user_id,)).fetchone()
    if not user:
        db.close()
        return None
    own_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (user_id,)).fetchone()['s']
    own_eh += db.execute('SELECT COALESCE(initial_eh,0) as s FROM users WHERE id=?', (user_id,)).fetchone()['s']
    current_career = career_for_row(user['manual_career_level'], own_eh)
    next_lvl = next((c for c in CAREER_LEVELS if c['level'] == current_career['level'] + 1), None)
    straenge = get_straenge_for_user(user_id, db=db)

    # Strang-Anforderung der nächsten Stufe finden
    needed_strands = 0
    min_per_strang = 0
    if next_lvl and 'rules' in next_lvl:
        for rule in next_lvl['rules']:
            if rule.get('type') == 'qualified_straenge':
                needed_strands = rule.get('min_count', 0)
                min_per_strang = rule.get('min_per_strang', 0)
                break

    # Welche meiner aktuellen Stränge sind schon qualifiziert?
    qualified = []
    in_progress = []
    for s in straenge:
        if min_per_strang > 0 and s['eh'] >= min_per_strang:
            s['status'] = 'qualified'
            s['pct'] = 100
            qualified.append(s)
        elif min_per_strang > 0:
            s['status'] = 'progress'
            s['pct'] = round(s['eh'] / min_per_strang * 100, 1)
            s['eh_to_qualify'] = max(0, min_per_strang - s['eh'])
            in_progress.append(s)
        else:
            s['status'] = 'na'
            s['pct'] = 100
            qualified.append(s)

    missing_strands = max(0, needed_strands - len(qualified))
    db.close()
    return {
        'next_level': next_lvl,
        'current_level': current_career,
        'needed_strands': needed_strands,
        'min_per_strang': min_per_strang,
        'qualified_count': len(qualified),
        'missing_count': missing_strands,
        'total_strands': len(straenge),
        'qualified': qualified,
        'in_progress': in_progress,
        'all_straenge': straenge,
    }


def get_structure_distribution(user_id, scope='all'):
    """Wie verteilt sich mein Team über die Karriere-Stufen?
    Returns: list of {short, name, color, count, pct, eh_total}"""
    if scope == 'direct':
        ids = [r['id'] for r in get_db().execute('SELECT id FROM users WHERE parent_id=? AND active=1', (user_id,)).fetchall()]
    else:
        ids = get_all_descendants(user_id)
    if not ids:
        return []
    db = get_db()
    placeholders = ','.join('?' * len(ids))
    rows = db.execute(f'''
        SELECT u.id, u.manual_career_level,
               COALESCE(SUM(c.einheiten), 0) as eh
        FROM users u
        LEFT JOIN contracts c ON c.owner_id=u.id AND c.status="abgeschlossen" AND c.recherche_status="freigegeben"
        WHERE u.id IN ({placeholders}) AND u.active=1
        GROUP BY u.id
    ''', ids).fetchall()
    db.close()
    # Stufe pro User berechnen
    counts = {l['short']: {'short': l['short'], 'name': l['name'], 'color': l['color'], 'level': l['level'], 'count': 0, 'eh_total': 0.0} for l in CAREER_LEVELS}
    for r in rows:
        career = career_for_row(r['manual_career_level'], r['eh'])
        counts[career['short']]['count'] += 1
        counts[career['short']]['eh_total'] += float(r['eh'] or 0)
    total = sum(c['count'] for c in counts.values())
    result = []
    for short in ['GREP', 'DREP', 'CREP', 'HREP', 'LREP', 'REP']:
        c = counts[short]
        c['pct'] = round((c['count'] / total * 100), 1) if total > 0 else 0
        result.append(c)
    return result


def get_coach_actions(user_id, max_actions=5):
    """Cached für 2 Min."""
    ckey = f'coach_acts:{user_id}:{max_actions}'
    cached_v = cache_get(ckey)
    if cached_v is not None:
        return cached_v
    try:
        result = _get_coach_actions_uncached(user_id, max_actions)
    except Exception as ex:
        print(f"[get_coach_actions] failed: {ex}")
        result = None
    cache_set(ckey, result, ttl=1800)
    return result


def _get_coach_actions_uncached(user_id, max_actions=5):
    """Was soll ich JETZT tun? Konsolidierte Action-Liste für die Dashboard-Coach-Karte.
    Mischt: KI-Recs, inaktive Partner, hängende Recherchen, Geburtstage, anstehende Termine.
    + HARD-TRIGGER (Najib's Specs): 0 EH 4W, 0 Termine, 0 calls → kritisch.
    Returns: list of {icon, title, detail, action_label, action_url, priority}"""
    actions = []
    db = get_db()
    today = date.today().isoformat()

    # ─── HARD-TRIGGER (Najib's Specs für inaktive Partner) ──────────
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    month_ago = (date.today() - timedelta(days=30)).isoformat()

    # 0 EH in letzten 4 Wochen → KRITISCH (mit Strukturhöher-CTA)
    eh_30d = db.execute('''SELECT COALESCE(SUM(einheiten),0) as s FROM contracts
                           WHERE owner_id=? AND status='abgeschlossen'
                           AND recherche_status='freigegeben' AND date(abschluss_date) >= date(?)''',
                        (user_id, month_ago)).fetchone()['s']
    if eh_30d == 0:
        # Strukturhöher finden
        parent = db.execute('SELECT name FROM users WHERE id=(SELECT parent_id FROM users WHERE id=?)', (user_id,)).fetchone()
        parent_name = parent['name'] if parent else 'deinem Mentor'
        actions.append({
            'icon': '🚨', 'priority': 'critical',
            'title': '4 Wochen 0 EH — KRITISCH',
            'detail': f'Ruf {parent_name} JETZT an. Nicht morgen.',
            'action_label': 'Anrufen', 'action_url': '/team',
        })
    # 0 Termine reingekommen letzte Woche → kritisch
    new_appts = db.execute('''SELECT COUNT(*) as c FROM appointments
                              WHERE owner_id=? AND date(created_at) >= date(?)''',
                           (user_id, week_ago)).fetchone()['c']
    if new_appts == 0:
        actions.append({
            'icon': '📭', 'priority': 'critical',
            'title': '0 Termine diese Woche',
            'detail': 'Namensliste raus. 5 Anrufe heute. Mind. 1 Termin diese Woche.',
            'action_label': 'Namensliste', 'action_url': '/namensliste',
        })
    # 1-Monats-Check: 250 EH + 3 GPs (Onboarding-Ziel)
    user_row = db.execute('SELECT joined_date FROM users WHERE id=?', (user_id,)).fetchone()
    if user_row and user_row['joined_date']:
        try:
            joined = datetime.strptime(user_row['joined_date'][:10], '%Y-%m-%d').date()
            days_in = (date.today() - joined).days
            if 25 <= days_in <= 35:  # ~1 Monat im Geschäft
                # 1-Monats-Ziele checken
                gps_in_month = db.execute('SELECT COUNT(*) as c FROM users WHERE parent_id=? AND active=1', (user_id,)).fetchone()['c']
                if eh_30d < 250:
                    actions.append({
                        'icon': '🎯', 'priority': 'high',
                        'title': f'1-Monats-Ziel: {int(eh_30d)}/250 EH',
                        'detail': f'Noch {int(250-eh_30d)} EH bis safe. {35-days_in}T Zeit.',
                        'action_label': 'Verträge', 'action_url': '/vertraege',
                    })
                if gps_in_month < 3:
                    actions.append({
                        'icon': '🤝', 'priority': 'high',
                        'title': f'1-Monats-Ziel: {gps_in_month}/3 Geschäftspartner',
                        'detail': f'Noch {3-gps_in_month} GPs einbinden. Recruiting-Liste durchgehen.',
                        'action_label': 'Recruiting', 'action_url': '/namensliste?typ=rk',
                    })
        except Exception:
            pass
    # ─── Standard-Trigger ──────────────────────────────────────────

    # 1) Hängende Recherchen
    pending_r = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND recherche_status IN ("ausstehend","")', (user_id,)).fetchone()['c']
    if pending_r >= 3:
        actions.append({'icon': '⏳', 'priority': 'high', 'title': f'{pending_r} Verträge hängen in Recherche',
                        'detail': 'Nachfassen. Sonst war der Vertrag Arbeit für nix.', 'action_label': 'Verträge', 'action_url': '/vertraege'})
    # 2) Inaktive direkte Partner
    inact = get_inactive_team_members(user_id, days=2, scope='direct')
    for u in inact[:2]:
        actions.append({'icon': '📞', 'priority': 'high' if u['days_inactive'] >= 5 else 'medium',
                        'title': f"{u['name']} anrufen",
                        'detail': f"{u['days_inactive']}T inaktiv — frag wo's hakt",
                        'action_label': 'Heute →', 'action_url': f"/partner/{u['id']}"})
    # 3) Heutige Termine
    today_term = db.execute('SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND date(termin_date)=? AND status="geplant"', (user_id, today)).fetchone()['c']
    if today_term > 0:
        actions.append({'icon': '◷', 'priority': 'high', 'title': f'{today_term} Termin{"e" if today_term > 1 else ""} heute',
                        'detail': 'Vorbereiten + bestätigen', 'action_label': 'Termine', 'action_url': '/termine'})
    # 4) Geburtstage diese Woche — STRANG-ISOLIERT (nur eigene Downline + deren Kunden)
    try:
        own_ids = [user_id] + get_all_descendants(user_id)
        ph_b = ','.join('?' * len(own_ids))
        birthday_rows = db.execute(f'''
            SELECT name, phone, birthday FROM users
                WHERE id IN ({ph_b}) AND active=1 AND birthday IS NOT NULL
            UNION ALL
            SELECT name, phone, birthday FROM leads
                WHERE owner_id IN ({ph_b}) AND birthday IS NOT NULL
        ''', own_ids + own_ids).fetchall()
        for b in birthday_rows:
            try:
                d = days_until_birthday(b['birthday'])
                if d is not None and d <= 3:
                    actions.append({'icon': '🎂', 'priority': 'medium',
                                    'title': f"{b['name']} hat {'heute' if d==0 else f'in {d}T'} Geburtstag",
                                    'detail': 'Anrufen oder Nachricht schicken',
                                    'action_label': 'Anrufen' if b['phone'] else 'Ok',
                                    'action_url': f"tel:{b['phone']}" if b['phone'] else '#'})
                    if len([a for a in actions if a['icon'] == '🎂']) >= 2: break
            except Exception:
                continue
    except Exception:
        pass
    # 5) Eingabeschluss Reminder
    deadlines = get_production_deadlines()
    if deadlines and deadlines.get('eingabe_in_days') is not None and 0 < deadlines['eingabe_in_days'] <= 3:
        actions.append({'icon': '⏰', 'priority': 'critical',
                        'title': f"Eingabeschluss in {deadlines['eingabe_in_days']} Tag{'en' if deadlines['eingabe_in_days'] > 1 else ''}",
                        'detail': 'Lieferst du oder schiebst du? Verträge rein.', 'action_label': 'Verträge', 'action_url': '/vertraege'})

    # 6) Wachstums-Tipps (wenn noch Platz ist)
    week_termine = db.execute("SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND date(termin_date) >= date('now','-7 days')", (user_id,)).fetchone()['c']
    week_neue_leads = db.execute("SELECT COUNT(*) as c FROM leads WHERE owner_id=? AND date(created_at) >= date('now','-7 days')", (user_id,)).fetchone()['c']
    namensliste_size = db.execute("SELECT COUNT(*) as c FROM leads WHERE owner_id=?", (user_id,)).fetchone()['c']
    direct_count = db.execute('SELECT COUNT(*) as c FROM users WHERE parent_id=? AND active=1', (user_id,)).fetchone()['c']
    week_abschluss = db.execute("SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND status='abgeschlossen' AND date(abschluss_date) >= date('now','-7 days')", (user_id,)).fetchone()['c']

    # Karriere-Stufe ermitteln für Starter-spezifische Tipps
    user_row = db.execute('SELECT manual_career_level, parent_id FROM users WHERE id=?', (user_id,)).fetchone()
    is_starter = user_row and (user_row['manual_career_level'] or 1) <= 1
    upline_id = user_row['parent_id'] if user_row else None

    growth_tips = []
    # STARTER-ROUTINE (höhere Priorität für Stufe 1)
    if is_starter:
        # 1) Tägliche 5 Anrufe aus Namensliste
        called_today = db.execute(
            "SELECT COUNT(*) as c FROM leads WHERE owner_id=? AND date(kontaktiert_at)=date('now')",
            (user_id,)).fetchone()['c']
        if called_today < 5 and namensliste_size > 0:
            growth_tips.append({'icon': '☎', 'priority': 'high',
                                'title': f'5 Anrufe heute (du hast {called_today})',
                                'detail': 'Pflicht für Stufe 1: 5 Anrufe. Heute. Keine Ausreden.',
                                'action_label': 'Liste öffnen', 'action_url': '/namensliste'})
        # 2) Upline kontaktieren wenn 3+ Tage kein Login mit Upline-Aktivität
        if upline_id:
            up_row = db.execute('SELECT name FROM users WHERE id=?', (upline_id,)).fetchone()
            up_name = up_row['name'] if up_row else 'deine Upline'
            growth_tips.append({'icon': '⬢', 'priority': 'high',
                                'title': f'{up_name} anrufen',
                                'detail': 'Tagesplanung mit Strukturhöher klären. Allein bist du langsamer.',
                                'action_label': 'Upline', 'action_url': f'/partner/{upline_id}'})
        # 3) Mindestens 1 Termin heute
        today_term_planned = db.execute("SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND date(termin_date)=date('now')",
                                         (user_id,)).fetchone()['c']
        if today_term_planned == 0:
            growth_tips.append({'icon': '◷', 'priority': 'high',
                                'title': 'Mindestens 1 Termin heute',
                                'detail': 'Kein Termin = kein Geschäft. Plan jetzt einen ein.',
                                'action_label': '+ Termin', 'action_url': '/termine/neu'})

    if namensliste_size < 50:
        growth_tips.append({'icon': '◎', 'priority': 'medium',
                            'title': 'Namensliste ausbauen',
                            'detail': f'Du hast {namensliste_size} Kontakte. Ziel: 100. Wer fehlt?',
                            'action_label': '+ Hinzufügen', 'action_url': '/namensliste/neu'})
    if week_termine < 5:
        growth_tips.append({'icon': '📞', 'priority': 'medium',
                            'title': 'Mehr Termine planen',
                            'detail': f'{week_termine} Termine diese Woche. Reicht nicht. 3 Termine = 1 Abschluss.',
                            'action_label': '+ Termin', 'action_url': '/termine/neu'})
    if week_neue_leads < 3:
        growth_tips.append({'icon': '🎯', 'priority': 'low',
                            'title': 'Cold-Calling-Block einplanen',
                            'detail': f'Nur {week_neue_leads} neue Kontakte diese Woche — 1h Block reicht für 5-10',
                            'action_label': 'Plan', 'action_url': '/aufgaben'})
    if direct_count < 3:
        growth_tips.append({'icon': '🌐', 'priority': 'medium',
                            'title': 'Eigene Struktur aufbauen',
                            'detail': 'Empfehl-Geschäft hebelt deine EH × 5 — sprich diese Woche 3 Personen drauf an',
                            'action_label': '+ Partner', 'action_url': '/team/neu'})
    if week_abschluss == 0 and week_termine >= 3:
        growth_tips.append({'icon': '🔍', 'priority': 'high',
                            'title': 'Termine konvertieren nicht',
                            'detail': f'{week_termine} Termine, 0 Abschlüsse — schau dir das Closing-Script nochmal an',
                            'action_label': 'Coach', 'action_url': '/assistent'})

    # Mische Wachstumstipps unter (max 2)
    actions.extend(growth_tips[:2])
    db.close()
    # Sort by priority + cap
    pri_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
    actions.sort(key=lambda x: pri_order.get(x.get('priority', 'low'), 9))
    return actions[:max_actions]


def get_team_calendar_root(user_id):
    """Kalender-Bubble-Logik:
    - Admin → eigene Wurzel (sieht alles)
    - HREP+ (Stufe 3+) → eigene Wurzel (sieht eigene Downline)
    - LREP/REP MIT HREP+ Ancestor → nutzt diesen HREP als Wurzel (geteilter Kalender)
    - LREP/REP OHNE HREP+ Ancestor aber MIT eigener Downline → eigene Wurzel (z.B. Abdallah-Case)
    - REP ohne alles → None (nur eigene Termine)
    Returns: dict {id, name, level, short} oder None."""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=? AND active=1', (user_id,)).fetchone()
    if not user:
        db.close()
        return None
    # Admin = sieht eigene volle Struktur
    if user['role'] == 'admin':
        db.close()
        return {'id': user_id, 'name': user['name'], 'level': 6, 'short': 'ADMIN'}
    own_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (user_id,)).fetchone()['s']
    own_eh += user['initial_eh'] or 0
    own_career = career_for_row(user['manual_career_level'], own_eh)
    # Self ist HREP+ → eigene Wurzel
    if own_career['level'] >= 3:
        db.close()
        return {'id': user_id, 'name': user['name'], 'level': own_career['level'], 'short': own_career['short']}
    # Suche HREP+ Ancestor
    current_id = user['parent_id']
    while current_id:
        parent = db.execute('SELECT * FROM users WHERE id=? AND active=1', (current_id,)).fetchone()
        if not parent:
            break
        peh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (current_id,)).fetchone()['s']
        peh += parent['initial_eh'] or 0
        pcareer = career_for_row(parent['manual_career_level'], peh)
        if pcareer['level'] >= 3:
            db.close()
            return {'id': current_id, 'name': parent['name'], 'level': pcareer['level'], 'short': pcareer['short']}
        current_id = parent['parent_id']
    # Kein HREP+ in Kette — fallback: eigene Wurzel wenn eigene Downline (Abdallah-Case)
    has_downline = db.execute('SELECT COUNT(*) as c FROM users WHERE parent_id=? AND active=1', (user_id,)).fetchone()['c'] > 0
    db.close()
    if has_downline:
        return {'id': user_id, 'name': user['name'], 'level': own_career['level'], 'short': own_career['short']}
    return None


# Color-Palette für Partner im Kalender (12 distincte Farben, hochwertig)
_CALENDAR_COLORS = [
    '#d4a843', '#3b82f6', '#10b981', '#a855f7', '#f59e0b', '#ef4444',
    '#06b6d4', '#ec4899', '#84cc16', '#6366f1', '#14b8a6', '#f97316'
]


def strang_color(partner_id):
    """Deterministische Farbe pro Direkt-Partner-Strang (für Upline-Sicht)."""
    if not isinstance(partner_id, int):
        return _CALENDAR_COLORS[0]
    return _CALENDAR_COLORS[partner_id % len(_CALENDAR_COLORS)]


def get_team_calendar_data(root_user_id, year, month, mono_color=None):
    """Alle Termine im Team (root + alle Downlines) für gegebenen Monat.
    Spezialfall root_user_id == '__global__': alle aktiven User systemweit.
    mono_color: wenn gesetzt, alle Termine bekommen DIESE EINE Farbe (Upline-Sicht
    eines Direkt-Strangs — Najib will Niesa's Strang in einer Farbe sehen).
    Returns: dict {appointments, members_with_colors}"""
    db = get_db()
    if root_user_id == '__global__':
        ids = [r['id'] for r in db.execute('SELECT id FROM users WHERE active=1').fetchall()]
    else:
        ids = [root_user_id] + get_all_descendants(root_user_id)
    placeholders = ','.join('?' * len(ids))
    members = db.execute(f'SELECT id, name, photo_path FROM users WHERE id IN ({placeholders}) AND active=1 ORDER BY name', ids).fetchall()
    if mono_color:
        color_map = {m['id']: mono_color for m in members}
    else:
        color_map = {m['id']: _CALENDAR_COLORS[i % len(_CALENDAR_COLORS)] for i, m in enumerate(members)}
    photo_map = {m['id']: m['photo_path'] for m in members}
    members_list = [{'id': m['id'], 'name': m['name'], 'color': color_map[m['id']], 'photo': photo_map.get(m['id'])} for m in members]

    # Termine im Monat (etwas grosszügig für Wochen-Übergreifende Anzeige)
    start_date = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end_date = f"{year + 1:04d}-01-01"
    else:
        end_date = f"{year:04d}-{month + 1:02d}-01"
    appts = db.execute(f'''
        SELECT a.*, u.name as owner_name
        FROM appointments a
        JOIN users u ON a.owner_id = u.id
        WHERE a.owner_id IN ({placeholders})
          AND date(a.termin_date) >= date(?) AND date(a.termin_date) < date(?)
        ORDER BY a.termin_date, a.termin_time
    ''', ids + [start_date, end_date]).fetchall()
    appts_list = []
    for a in appts:
        d = dict(a)
        d['color'] = color_map.get(a['owner_id'], '#94a3b8')
        appts_list.append(d)
    db.close()
    return {'appointments': appts_list, 'members': members_list}


def get_quoten_forecast(user_id, days=30):
    """Cached für 5 Min."""
    ckey = f'forecast:{user_id}:{days}'
    cached_v = cache_get(ckey)
    if cached_v is not None:
        return cached_v
    try:
        result = _get_quoten_forecast_uncached(user_id, days)
    except Exception as ex:
        print(f"[get_quoten_forecast] failed: {ex}")
        result = None
    cache_set(ckey, result, ttl=1800)
    return result


def _get_quoten_forecast_uncached(user_id, days=30):
    """Auto-Prognose: was wird der User dieses Monat schaffen — basierend auf Vergangenheit?
    Returns: dict mit termine/abschluss/eh/abgesagt/abgelehnt — vorhergesagt."""
    db = get_db()
    # Vergangene 60 Tage als Basis
    lookback_days = 60
    past_termine = db.execute(f"SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND date(termin_date) >= date('now','-{lookback_days} days')", (user_id,)).fetchone()['c']
    past_termine_done = db.execute(f"SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND date(termin_date) >= date('now','-{lookback_days} days') AND status='erledigt'", (user_id,)).fetchone()['c']
    past_termine_cancel = db.execute(f"SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND date(termin_date) >= date('now','-{lookback_days} days') AND status='abgesagt'", (user_id,)).fetchone()['c']
    past_abschluss = db.execute(f"SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND status='abgeschlossen' AND date(abschluss_date) >= date('now','-{lookback_days} days')", (user_id,)).fetchone()['c']
    past_storno = db.execute(f"SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND status='storniert' AND date(created_at) >= date('now','-{lookback_days} days')", (user_id,)).fetchone()['c']
    past_eh_total = db.execute(f"SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status='abgeschlossen' AND recherche_status='freigegeben' AND date(abschluss_date) >= date('now','-{lookback_days} days')", (user_id,)).fetchone()['s']
    past_recherche_neg = db.execute(f"SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND recherche_status='negativ' AND date(created_at) >= date('now','-{lookback_days} days')", (user_id,)).fetchone()['c']
    db.close()

    # Hochrechnen auf Zielzeitraum
    factor = days / lookback_days
    return {
        'termine_geplant': round(past_termine * factor),
        'termine_erfolgreich': round(past_termine_done * factor),
        'termine_abgesagt': round(past_termine_cancel * factor),
        'termine_cancel_rate': round((past_termine_cancel / past_termine * 100) if past_termine else 0, 1),
        'abschluss_count': round(past_abschluss * factor),
        'abschluss_storno_rate': round((past_storno / past_abschluss * 100) if past_abschluss else 0, 1),
        'abschluss_negativ_rate': round((past_recherche_neg / past_abschluss * 100) if past_abschluss else 0, 1),
        'eh_forecast': round(past_eh_total * factor),
        'days_target': days,
        'lookback_days': lookback_days,
        'has_data': (past_termine + past_abschluss) > 0,
    }


def get_user_activity_today(user_id):
    """Was hat ein User HEUTE getan? Returns dict mit Counts und last_active_at."""
    db = get_db()
    today = date.today().isoformat()
    cnt_leads = db.execute("SELECT COUNT(*) as c FROM leads WHERE owner_id=? AND date(created_at)=?", (user_id, today)).fetchone()['c']
    cnt_termine = db.execute("SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND date(created_at)=?", (user_id, today)).fetchone()['c']
    cnt_termine_done = db.execute("SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND date(termin_date)=? AND status='erledigt'", (user_id, today)).fetchone()['c']
    cnt_vertraege = db.execute("SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND date(created_at)=?", (user_id, today)).fetchone()['c']
    # Tagesaufgaben done/total
    try:
        tasks_total = db.execute("SELECT COUNT(*) as c FROM user_tasks WHERE user_id=? AND date(datum)=?", (user_id, today)).fetchone()['c']
        tasks_done = db.execute("SELECT COUNT(*) as c FROM user_tasks WHERE user_id=? AND date(datum)=? AND done=1", (user_id, today)).fetchone()['c']
    except Exception:
        tasks_total, tasks_done = 0, 0
    # Letzte Aktivität via activity_log (max date) — falls vorhanden
    last_act = db.execute("SELECT MAX(created_at) as la FROM activity_log WHERE user_id=?", (user_id,)).fetchone()
    last_active_at = last_act['la'] if last_act else None
    db.close()
    total_actions = cnt_leads + cnt_termine + cnt_termine_done + cnt_vertraege + tasks_done
    return {
        'leads_today': cnt_leads, 'termine_today': cnt_termine,
        'termine_done_today': cnt_termine_done, 'vertraege_today': cnt_vertraege,
        'tasks_done': tasks_done, 'tasks_total': tasks_total,
        'last_active_at': last_active_at,
        'active_today': total_actions > 0,
        'total_actions_today': total_actions,
    }


def get_inactive_team_members(user_id, days=1, scope='direct'):
    """Liefert Liste der Partner die seit X Tagen nichts getan haben.
    scope='direct' = nur direkte Downline, 'all' = ganze Struktur."""
    db = get_db()
    if scope == 'direct':
        ids = [r['id'] for r in db.execute('SELECT id FROM users WHERE parent_id=? AND active=1', (user_id,)).fetchall()]
    else:
        ids = get_all_descendants(user_id)
    if not ids:
        db.close()
        return []
    placeholders = ','.join('?' * len(ids))
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = db.execute(f'''
        SELECT u.id, u.name, u.email,
               (SELECT MAX(created_at) FROM activity_log WHERE user_id=u.id) as last_active,
               (SELECT MAX(created_at) FROM leads WHERE owner_id=u.id) as last_lead,
               (SELECT MAX(created_at) FROM appointments WHERE owner_id=u.id) as last_termin
        FROM users u
        WHERE u.id IN ({placeholders}) AND u.active=1
    ''', ids).fetchall()
    db.close()
    inactive = []
    for r in rows:
        latest = max([x for x in [r['last_active'], r['last_lead'], r['last_termin']] if x] or [''])
        if not latest or latest[:10] < cutoff:
            try:
                days_inactive = (date.today() - datetime.strptime(latest[:10], '%Y-%m-%d').date()).days if latest else 999
            except Exception:
                days_inactive = 999
            inactive.append({
                'id': r['id'], 'name': r['name'], 'email': r['email'],
                'last_active': latest or None, 'days_inactive': days_inactive
            })
    return sorted(inactive, key=lambda x: x['days_inactive'], reverse=True)


def get_user_total_eh(user_id, include_team=False):
    """EH eines Users (eigene oder mit Team) — inkl. initial_eh (Pre-System-Bestand)"""
    db = get_db()
    if include_team:
        ids = [user_id] + get_all_descendants(user_id)
    else:
        ids = [user_id]
    placeholders = ','.join('?' * len(ids))
    contract_eh = db.execute(
        f'SELECT COALESCE(SUM(einheiten), 0) as total FROM contracts WHERE owner_id IN ({placeholders}) AND status = "abgeschlossen" AND recherche_status = "freigegeben"',
        ids
    ).fetchone()['total']
    # Initial-EH (was vor System-Start schon produziert wurde) dazurechnen
    initial = db.execute(
        f'SELECT COALESCE(SUM(initial_eh), 0) as total FROM users WHERE id IN ({placeholders})',
        ids
    ).fetchone()['total']
    db.close()
    return contract_eh + initial


def build_tree(user_id, db):
    """Rekursiv Strukturbaum aufbauen mit allen Stats"""
    user = db.execute('SELECT * FROM users WHERE id = ? AND active = 1', (user_id,)).fetchone()
    if not user:
        return None
    children_rows = db.execute('SELECT id FROM users WHERE parent_id = ? AND active = 1 ORDER BY name', (user_id,)).fetchall()
    own_eh = db.execute('SELECT COALESCE(SUM(einheiten), 0) as s FROM contracts WHERE owner_id = ? AND status = "abgeschlossen" AND recherche_status = "freigegeben"', (user_id,)).fetchone()['s']
    contracts = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id = ? AND status = "abgeschlossen" AND recherche_status = "freigegeben"', (user_id,)).fetchone()['c']
    appointments_done = db.execute('SELECT COUNT(*) as c FROM appointments WHERE owner_id = ? AND status = "erledigt"', (user_id,)).fetchone()['c']
    leads = db.execute('SELECT COUNT(*) as c FROM leads WHERE owner_id = ?', (user_id,)).fetchone()['c']

    # FIX: korrekte Stufe = max(manuelle Stufe, durch EH erreichte Stufe)
    earned_level = 1
    for cl in CAREER_LEVELS:
        if own_eh >= cl['min_eh']:
            earned_level = cl['level']
        else:
            break
    final_level = max(user['manual_career_level'] or 1, earned_level)
    career = next((c for c in CAREER_LEVELS if c['level'] == final_level), CAREER_LEVELS[0])

    children = []
    team_eh = own_eh
    team_size = 1
    for ch in children_rows:
        sub = build_tree(ch['id'], db)
        if sub:
            children.append(sub)
            team_eh += sub['team_eh']
            team_size += sub['team_size']

    return {
        'id': user['id'], 'name': user['name'], 'email': user['email'],
        'phone': user['phone'], 'level': user['level'], 'role': user['role'],
        'own_eh': own_eh, 'team_eh': team_eh,
        'contracts': contracts, 'appointments_done': appointments_done, 'leads': leads,
        'team_size': team_size,
        'career': career,
        'children': children
    }


def get_conversion_rate(user_id, include_team=True):
    """Berechnet Termine pro Abschluss"""
    db = get_db()
    if include_team:
        ids = [user_id] + get_all_descendants(user_id)
    else:
        ids = [user_id]
    ph = ','.join('?' * len(ids))
    termine = db.execute(f'SELECT COUNT(*) as c FROM appointments WHERE owner_id IN ({ph}) AND status = "erledigt"', ids).fetchone()['c']
    abschluss = db.execute(f'SELECT COUNT(*) as c FROM contracts WHERE owner_id IN ({ph}) AND status = "abgeschlossen" AND recherche_status = "freigegeben"', ids).fetchone()['c']
    db.close()
    if abschluss == 0:
        return {'termine': termine, 'abschluss': abschluss, 'rate': 0, 'fehlende_termine': 0}
    rate = termine / abschluss
    return {'termine': termine, 'abschluss': abschluss, 'rate': rate, 'fehlende_termine': 0}


def get_team_stats(user_id, include_self=True):
    db = get_db()
    descendants = get_all_descendants(user_id)
    team_ids = ([user_id] if include_self else []) + descendants

    if not team_ids:
        return {'leads': 0, 'contracts': 0, 'volumen': 0, 'einheiten': 0, 'members': 0, 'appointments': 0}

    placeholders = ','.join('?' * len(team_ids))
    leads = db.execute(f'SELECT COUNT(*) as c FROM leads WHERE owner_id IN ({placeholders})', team_ids).fetchone()['c']
    contracts = db.execute(f'SELECT COUNT(*) as c FROM contracts WHERE owner_id IN ({placeholders}) AND status = "abgeschlossen" AND recherche_status = "freigegeben"', team_ids).fetchone()['c']
    volumen = db.execute(f'SELECT COALESCE(SUM(volumen), 0) as s FROM contracts WHERE owner_id IN ({placeholders}) AND status = "abgeschlossen" AND recherche_status = "freigegeben"', team_ids).fetchone()['s']
    einheiten = db.execute(f'SELECT COALESCE(SUM(einheiten), 0) as s FROM contracts WHERE owner_id IN ({placeholders}) AND status = "abgeschlossen" AND recherche_status = "freigegeben"', team_ids).fetchone()['s']
    appointments = db.execute(f'SELECT COUNT(*) as c FROM appointments WHERE owner_id IN ({placeholders}) AND status = "geplant"', team_ids).fetchone()['c']
    members = db.execute(f'SELECT COUNT(*) as c FROM users WHERE id IN ({placeholders}) AND active = 1', team_ids).fetchone()['c']

    db.close()
    return {'leads': leads, 'contracts': contracts, 'volumen': volumen, 'einheiten': einheiten, 'members': members, 'appointments': appointments}


# === ROUTES ===

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login/magic-request', methods=['POST'])
def login_magic_request():
    """Magic-Link-Login: User trägt nur E-Mail ein, bekommt Klick-Link.
    Anti-Enumeration: gibt immer dieselbe Erfolgsmeldung zurück."""
    email = (request.form.get('email') or '').strip().lower()
    if not email or '@' not in email:
        flash('Bitte gültige E-Mail eingeben.', 'error')
        return redirect(url_for('login'))
    db = get_db()
    row = db.execute('SELECT id, name, email FROM users WHERE LOWER(email)=? AND active=1', (email,)).fetchone()
    if row:
        token = secrets.token_urlsafe(32)
        expires = (datetime.now() + timedelta(minutes=20)).strftime('%Y-%m-%d %H:%M:%S')
        db.execute('INSERT INTO magic_link_tokens (user_id, token, expires_at, ip) VALUES (?,?,?,?)',
                   (row['id'], token, expires, request.remote_addr or ''))
        db.commit()
        base = (request.url_root or '').rstrip('/')
        link = f'{base}/login/magic/{token}'
        if is_smtp_configured():
            text = (f"Hallo {row['name']},\n\nklick zum Einloggen (gültig 20 Minuten):\n\n{link}\n\n"
                    f"Wenn du das nicht warst — einfach ignorieren. Niemand kommt ohne diesen Link rein.\n\nProAcademy")
            html = (f'<p>Hallo {row["name"]},</p>'
                    f'<p>Klick zum sofortigen Einloggen — kein Passwort nötig:</p>'
                    f'<p style="margin:24px 0"><a href="{link}" style="background:#d4a843;color:#0f1c3f;'
                    f'padding:14px 26px;border-radius:10px;text-decoration:none;font-weight:800;display:inline-block">'
                    f'→ Login öffnen</a></p>'
                    f'<p style="color:#64748b;font-size:13px">Link gilt 20 Minuten. War das nicht du? Einfach ignorieren.</p>')
            try:
                send_email(row['email'], 'Login-Link für Pro Academy', text,
                           body_html=html, sent_by=None, category='login_link')
            except Exception as e:
                print(f'[magic-link] mail fail: {e}')
    db.close()
    flash('Falls die E-Mail bei uns hinterlegt ist, kommt gleich ein Login-Link an.', 'info')
    return render_template('magic_link_sent.html', email_used=email)


@app.route('/login/magic/<token>')
def login_magic(token):
    """Magic-Link Klick → User einloggen."""
    db = get_db()
    row = db.execute('''SELECT m.*, u.name as user_name, u.email as user_email,
                               u.active, u.must_change_password, u.role
                        FROM magic_link_tokens m JOIN users u ON m.user_id = u.id
                        WHERE m.token=? AND m.used_at IS NULL''', (token,)).fetchone()
    if not row:
        db.close()
        flash('Login-Link ungültig oder schon benutzt. Fordere einen neuen an.', 'error')
        return redirect(url_for('login'))
    try:
        exp = datetime.strptime(row['expires_at'], '%Y-%m-%d %H:%M:%S')
    except Exception:
        exp = datetime.now() - timedelta(seconds=1)
    if datetime.now() > exp:
        db.close()
        flash('Login-Link ist abgelaufen. Fordere einen neuen an.', 'error')
        return redirect(url_for('login'))
    if not row['active']:
        db.close()
        flash('Account ist nicht aktiv. Frag deinen Strukturhöher.', 'error')
        return redirect(url_for('login'))
    # Token verbrauchen
    db.execute('UPDATE magic_link_tokens SET used_at=CURRENT_TIMESTAMP WHERE id=?', (row['id'],))
    # User-Object für flask-login
    user_row = db.execute('SELECT * FROM users WHERE id=?', (row['user_id'],)).fetchone()
    db.commit()
    db.close()
    if not user_row:
        return redirect(url_for('login'))
    user_obj = User(dict(user_row))
    session.permanent = True
    login_user(user_obj, remember=True, duration=timedelta(days=30))
    log_activity(user_obj.id, 'login', f'{user_obj.name} hat sich per Magic-Link eingeloggt', icon='✓', color='green')
    flash(f'Hi {user_obj.name.split()[0]} — eingeloggt.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        # Rate-Limit Check
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
        block_key = f'{client_ip}:{email[:50]}'
        if is_login_blocked(block_key):
            flash('Zu viele fehlgeschlagene Login-Versuche. Bitte in 15 Minuten erneut versuchen.', 'error')
            return render_template('login.html')
        db = get_db()
        row = db.execute('SELECT * FROM users WHERE email = ? AND active = 1', (email,)).fetchone()
        if row and verify_password(row['password'], password):
            record_login_attempt(block_key, success=True)
            # Last-Login + Counter aktualisieren
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            new_count = (row['login_count'] or 0) + 1
            db.execute('UPDATE users SET last_login = ?, login_count = ? WHERE id = ?',
                       (now, new_count, row['id']))
            # Falls alter SHA256-Hash: Migration zu pbkdf2
            if not (row['password'] or '').startswith(('pbkdf2:', 'scrypt:')):
                db.execute('UPDATE users SET password = ? WHERE id = ?',
                           (hash_password(password), row['id']))
            db.commit()
            db.close()
            # Session permanent machen + Remember-Me aktivieren
            # → User bleibt 60 Tage eingeloggt, selbst nach Handy/Browser-Restart
            session.permanent = True
            login_user(User(row), remember=True, duration=timedelta(days=60))
            session['show_vision'] = True
            # Achievements prüfen
            try:
                check_achievements_for_user(row['id'])
            except Exception as e:
                print(f"Achievement check failed: {e}")
            # Onboarding-Check: wenn noch nicht durch + nicht-Admin → /willkommen
            try:
                onboarding_done = row['onboarding_done'] if 'onboarding_done' in row.keys() else 0
            except Exception:
                onboarding_done = 0
            # Activity log nur bei erstem Login des Tages
            today = date.today().strftime('%Y-%m-%d')
            db2 = get_db()
            existing = db2.execute('SELECT id FROM activity_log WHERE user_id = ? AND event_type = ? AND date(created_at) = ?',
                                   (row['id'], 'login', today)).fetchone()
            db2.close()
            if not existing:
                log_activity(row['id'], 'login', f'{row["name"]} ist heute eingeloggt', icon='🔓', color='blue')
                # Auto-Backup + Reminder einmal pro Tag (beim ersten Admin-Login)
                if row['role'] == 'admin':
                    auto_backup_if_needed()
                    try:
                        send_eingabeschluss_reminders()
                        send_zvg_reminder()
                    except Exception as e:
                        print(f"Auto-Reminder warning: {e}")
            # Forced Passwort-Change wenn aktiviert
            try:
                must_change = row['must_change_password']
            except Exception:
                must_change = 0
            if must_change:
                session['must_change_password'] = True
                return redirect(url_for('passwort_aendern'))
            # Erstes Login → Onboarding-Wizard zeigen (nur Nicht-Admins)
            if not onboarding_done and row['role'] != 'admin':
                return redirect(url_for('willkommen'))
            return redirect(url_for('dashboard'))
        db.close()
        record_login_attempt(block_key, success=False)
        flash('Falsche E-Mail oder Passwort', 'error')
    return render_template('login.html')


_FAVICON_SVG = '''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
    <defs>
        <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stop-color="#0f1c3f"/>
            <stop offset="100%" stop-color="#1a2c5b"/>
        </linearGradient>
    </defs>
    <rect width="100" height="100" rx="22" fill="url(#bg)"/>
    <text x="50" y="68" font-family="Inter, Arial, sans-serif" font-weight="900" font-size="42" fill="#d4a843" text-anchor="middle" letter-spacing="-3">NT</text>
</svg>'''


@app.route('/favicon.svg')
def favicon_svg():
    """SVG-Favicon mit NT-Logo (Navy + Gold)."""
    return Response(_FAVICON_SVG, mimetype='image/svg+xml',
                    headers={'Cache-Control': 'public, max-age=86400'})


@app.route('/favicon.ico')
def favicon_ico():
    """Liefert SVG direkt aus statt Redirect (Browser folgen Favicon-Redirects unzuverlässig)."""
    return Response(_FAVICON_SVG, mimetype='image/svg+xml',
                    headers={'Cache-Control': 'public, max-age=86400'})


@app.route('/apple-touch-icon.png')
@app.route('/apple-touch-icon-precomposed.png')
@app.route('/apple-touch-icon-152x152.png')
@app.route('/apple-touch-icon-152x152-precomposed.png')
@app.route('/apple-touch-icon-180x180.png')
def apple_touch_icon():
    """Safari/iOS Home-Screen Icon — echtes PNG (180x180) damit iOS es akzeptiert."""
    return send_from_directory(os.path.join(app.root_path, 'static', 'icons'),
                               'apple-touch-icon.png', mimetype='image/png')


# === PUSH NOTIFICATIONS (Web Push API + VAPID) ===
VAPID_PUBLIC = 'BN0i_1u9X1MaVVqO74v5hXbKK8PyHV7QJtjzvuZpSqQV7PZw69Mg1wKWDGckR_XeTEUvbd4zSZfZCR36H47qMac'
VAPID_PRIVATE = 'xIzBWGWIy96l7zdFSPlJyEKnoOtBTa3zVnGhT1ADp1o'
VAPID_CONTACT = 'mailto:najib@ntpro.de'


def _user_wants_push(user_id, push_type):
    """Checkt ob User diese Push-Kategorie aktiviert hat. Default: alles an."""
    db = get_db()
    row = db.execute('SELECT push_prefs FROM users WHERE id=?', (user_id,)).fetchone()
    db.close()
    if not row or not row['push_prefs']:
        return True
    try:
        prefs = json.loads(row['push_prefs'])
        # Default True wenn nicht gesetzt
        return prefs.get(push_type, True)
    except Exception:
        return True


def _push_send_sync(user_id, title, body, url, urgent, tag, push_type):
    """Echte Push-Logik — wird vom Background-Thread aufgerufen.
    Synchron ggü. FCM/APNS, daher hier 5s timeout pro Subscription."""
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return (0, 0)
    db = get_db()
    subs = db.execute('SELECT * FROM push_subscriptions WHERE user_id=?', (user_id,)).fetchall()
    db.close()
    sent, failed = 0, 0
    payload = json.dumps({
        'title': title, 'body': body, 'url': url, 'urgent': urgent,
        'tag': tag or f'user-{user_id}',
        'icon': '/static/icons/pa-icon-192.png',
        'badge': '/static/icons/pa-favicon-32.png',
    })
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub['endpoint'],
                    'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']},
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE,
                vapid_claims={'sub': VAPID_CONTACT},
                timeout=5,  # FCM/APNS-Call max 5s pro Gerät
            )
            sent += 1
        except WebPushException as e:
            failed += 1
            # 410 Gone = Subscription invalid → remove from DB
            if e.response and e.response.status_code in (404, 410):
                try:
                    db = get_db()
                    db.execute('DELETE FROM push_subscriptions WHERE id=?', (sub['id'],))
                    db.commit()
                    db.close()
                except Exception:
                    pass
        except Exception:
            failed += 1
    return (sent, failed)


def send_push_to_user(user_id, title, body, url='/dashboard', urgent=False, tag=None, push_type=None):
    """Sendet Push-Notification ASYNC (Background-Thread) — Save-Routes blockieren NIE.
    push_type: optional — wenn gesetzt, wird User-Präferenz geprüft.
    Returns: (1, 0) als optimistisches Ack — echtes Ergebnis liegt im Background."""
    if push_type and not _user_wants_push(user_id, push_type):
        return (0, 0)
    # Hintergrund-Thread: Push-Call darf 30s+ dauern, aber Save-Route ist sofort fertig
    import threading as _t
    _t.Thread(
        target=_push_send_sync,
        args=(user_id, title, body, url, urgent, tag, push_type),
        daemon=True,
        name=f'push-u{user_id}'
    ).start()
    return (1, 0)  # optimistisch — UI muss nicht warten


def _push_already_sent(user_id, push_type, ref_key=''):
    """Prüft ob ein bestimmter Push für den User heute schon raus ist (idempotent)."""
    db = get_db()
    today = date.today().isoformat()
    full_key = f'{today}:{ref_key}'
    row = db.execute('SELECT id FROM push_log WHERE user_id=? AND push_type=? AND ref_key=?',
                     (user_id, push_type, full_key)).fetchone()
    db.close()
    return row is not None


def _push_mark_sent(user_id, push_type, ref_key=''):
    db = get_db()
    today = date.today().isoformat()
    full_key = f'{today}:{ref_key}'
    try:
        db.execute('INSERT OR IGNORE INTO push_log (user_id, push_type, ref_key) VALUES (?, ?, ?)',
                   (user_id, push_type, full_key))
        db.commit()
    finally:
        db.close()


def run_daily_pushes(force=False):
    """Schickt alle anstehenden Push-Notifications. Idempotent (1× pro Tag pro Push pro User).
    force=True ignoriert die Tagessperre.
    Returns: dict mit statistiken."""
    stats = {'birthday_customer': 0, 'birthday_partner': 0, 'inactive_alert': 0,
             'eingabeschluss': 0, 'daily_routine': 0}
    db = get_db()
    active_users = db.execute("SELECT id, name, role, manual_career_level FROM users WHERE active=1").fetchall()
    deadlines = get_production_deadlines()
    db.close()

    for u in active_users:
        uid = u['id']

        # 1) GEBURTSTAGS-PUSHES (Kunden + Partner mit relevantem Geburtstag in 0-2 Tagen)
        try:
            db = get_db()
            # Kunden (leads) mit Birthday
            lead_birthdays = db.execute(
                "SELECT id, name, birthday, phone FROM leads WHERE owner_id=? AND birthday IS NOT NULL", (uid,)
            ).fetchall()
            # Direkte Partner mit Birthday (du als Upline kriegst Push)
            partner_birthdays = db.execute(
                "SELECT id, name, birthday, phone FROM users WHERE parent_id=? AND active=1 AND birthday IS NOT NULL", (uid,)
            ).fetchall()
            db.close()
            for b in lead_birthdays:
                d = days_until_birthday(b['birthday'])
                if d is None: continue
                if d == 0:
                    key = f'kunde-{b["id"]}-{b["birthday"]}'
                    if force or not _push_already_sent(uid, 'birthday_customer', key):
                        send_push_to_user(uid,
                            title=f'🎂 {b["name"]} hat heute Geburtstag!',
                            body=f'Kurz anrufen oder Nachricht schicken — kostet nichts, wirkt viel.',
                            url='/namensliste', urgent=True, tag='birthday', push_type='birthday_customer')
                        _push_mark_sent(uid, 'birthday_customer', key)
                        stats['birthday_customer'] += 1
                elif d == 1:
                    key = f'kunde-{b["id"]}-vor-{b["birthday"]}'
                    if force or not _push_already_sent(uid, 'birthday_customer_before', key):
                        send_push_to_user(uid,
                            title=f'🎂 Morgen Geburtstag: {b["name"]}',
                            body='Vergiss nicht — eine kurze Nachricht macht den Tag.',
                            url='/namensliste', tag='birthday-soon')
                        _push_mark_sent(uid, 'birthday_customer_before', key)
                        stats['birthday_customer'] += 1
            for b in partner_birthdays:
                d = days_until_birthday(b['birthday'])
                if d == 0:
                    key = f'partner-{b["id"]}-{b["birthday"]}'
                    if force or not _push_already_sent(uid, 'birthday_partner', key):
                        send_push_to_user(uid,
                            title=f'🎉 {b["name"]} hat heute Geburtstag (Partner)!',
                            body='Dein Partner hat heute Geburtstag — feier ihn kurz.',
                            url=f'/partner/{b["id"]}/profil', urgent=True, tag='birthday', push_type='birthday_partner')
                        _push_mark_sent(uid, 'birthday_partner', key)
                        stats['birthday_partner'] += 1
        except Exception:
            pass

        # 2) INAKTIV-ALERT für Uplines (3+ Tage stille direkte Partner)
        try:
            inact = get_inactive_team_members(uid, days=3, scope='direct')
            if inact:
                key = f'inact-{len(inact)}'
                if force or not _push_already_sent(uid, 'inactive_alert', key):
                    names = ', '.join([f"{x['name']} ({x['days_inactive']}T)" for x in inact[:3]])
                    send_push_to_user(uid,
                        title=f'⚠ {len(inact)} Partner sind inaktiv',
                        body=f'{names}{" und mehr" if len(inact) > 3 else ""}. Kurzer Anruf?',
                        url='/team/inaktiv', tag='inactive', push_type='inactive_alert')
                    _push_mark_sent(uid, 'inactive_alert', key)
                    stats['inactive_alert'] += 1
        except Exception:
            pass

        # 3) EINGABESCHLUSS-REMINDER (wenn ≤ 2 Tage)
        try:
            if deadlines and 0 < deadlines.get('eingabe_in_days', 99) <= 2:
                key = f'eingabe-{deadlines["eingabeschluss"].isoformat()}'
                if force or not _push_already_sent(uid, 'eingabeschluss', key):
                    days_left = deadlines['eingabe_in_days']
                    send_push_to_user(uid,
                        title=f'⏰ Eingabeschluss in {days_left} Tag{"en" if days_left > 1 else ""}!',
                        body='Alle Verträge eintragen — sonst zählen die EH nicht für diesen Monat.',
                        url='/vertraege', urgent=True, tag='deadline', push_type='eingabeschluss')
                    _push_mark_sent(uid, 'eingabeschluss', key)
                    stats['eingabeschluss'] += 1
        except Exception:
            pass

        # 4) STARTER-ROUTINE (Stufe 1) — täglicher Kick-off-Push
        try:
            if (u['manual_career_level'] or 1) <= 1 and u['role'] != 'admin':
                key = f'routine'
                if force or not _push_already_sent(uid, 'daily_routine', key):
                    send_push_to_user(uid,
                        title='☎ Heute 5 Anrufe!',
                        body='Stufe-1-Routine: 5 Personen aus der Namensliste anrufen + Upline kontaktieren + 1 Termin planen.',
                        url='/namensliste', tag='routine', push_type='daily_routine')
                    _push_mark_sent(uid, 'daily_routine', key)
                    stats['daily_routine'] += 1

                # Onboarding-Roadmap-Reminder
                progress = get_onboarding_progress(uid)
                if progress and not progress['completed']:
                    open_today = [t for t in progress['days'].get(progress['active_day'], []) if not t['done']]
                    if open_today:
                        key = f'roadmap-day{progress["active_day"]}'
                        if force or not _push_already_sent(uid, 'onboarding_reminder', key):
                            first = open_today[0]
                            send_push_to_user(uid,
                                title=f'🚀 Tag {progress["active_day"]}: {len(open_today)} Aufgabe{"n" if len(open_today)>1 else ""} offen',
                                body=f'{first["title"]} — {first["detail"]}',
                                url='/onboarding/roadmap', tag='roadmap', push_type='onboarding_reminder')
                            _push_mark_sent(uid, 'onboarding_reminder', key)
        except Exception:
            pass

    return stats


@app.route('/api/push/run-daily', methods=['POST', 'GET'])
@login_required
def push_run_daily():
    """Daily-Push manuell triggern (Admin)."""
    if not current_user.has_admin_access:
        return jsonify({'ok': False, 'error': 'admin only'}), 403
    force = request.args.get('force') == '1'
    stats = run_daily_pushes(force=force)
    return jsonify({'ok': True, 'stats': stats})


@app.route('/api/push/broadcast', methods=['POST'])
@login_required
def push_broadcast():
    """Admin schickt manuelle Push an Team (alle / Stufe / einzelne)."""
    if not current_user.has_admin_access:
        return jsonify({'ok': False, 'error': 'admin only'}), 403
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    body = (data.get('body') or '').strip()
    url_target = data.get('url', '/dashboard')
    scope = data.get('scope', 'all')  # 'all', 'team', 'level:N', 'user:ID'
    if not title:
        return jsonify({'ok': False, 'error': 'no title'}), 400

    db = get_db()
    if scope == 'all':
        rows = db.execute('SELECT id FROM users WHERE active=1').fetchall()
    elif scope == 'team':
        ids = [current_user.id] + get_all_descendants(current_user.id)
        ph = ','.join('?' * len(ids))
        rows = db.execute(f'SELECT id FROM users WHERE id IN ({ph}) AND active=1', ids).fetchall()
    elif scope.startswith('level:'):
        try:
            lvl = int(scope.split(':')[1])
            rows = db.execute('SELECT id FROM users WHERE active=1 AND COALESCE(manual_career_level,1)=?', (lvl,)).fetchall()
        except Exception:
            rows = []
    elif scope.startswith('user:'):
        try:
            target = int(scope.split(':')[1])
            rows = [{'id': target}]
        except Exception:
            rows = []
    else:
        rows = []
    db.close()

    sent_total, failed_total = 0, 0
    for r in rows:
        s, f = send_push_to_user(r['id'], title=title, body=body, url=url_target, tag='broadcast', push_type='broadcast')
        sent_total += s
        failed_total += f
    return jsonify({'ok': True, 'recipients': len(rows), 'sent': sent_total, 'failed': failed_total})


# Lazy-Trigger: beim Dashboard-Load (1× pro Tag) Daily-Pushes laufen lassen
_daily_push_lock = {'date': None}


def _maybe_run_daily_pushes_lazy():
    today = date.today().isoformat()
    if _daily_push_lock['date'] == today:
        return
    _daily_push_lock['date'] = today
    try:
        run_daily_pushes(force=False)
    except Exception:
        pass


@app.route('/sw.js')
def service_worker():
    """Service Worker MUSS unter root-scope ausgeliefert werden, nicht unter /static/.
    Cache: 5 Min mit must-revalidate → spart Requests aber Updates kommen zügig."""
    from flask import send_from_directory as _sfd
    resp = _sfd(os.path.join(app.root_path, 'static'), 'sw.js', mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    # 5 Min Browser-Cache + must-revalidate für Updates
    resp.headers['Cache-Control'] = 'public, max-age=300, must-revalidate'
    return resp


@app.route('/api/push/vapid-key')
def push_vapid_key():
    return jsonify({'key': VAPID_PUBLIC})


@app.route('/api/push/subscribe', methods=['POST'])
@login_required
def push_subscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint')
    keys = data.get('keys', {})
    p256dh = keys.get('p256dh')
    auth = keys.get('auth')
    if not (endpoint and p256dh and auth):
        return jsonify({'ok': False, 'error': 'invalid'}), 400
    db = get_db()
    db.execute('''INSERT OR REPLACE INTO push_subscriptions
                  (user_id, endpoint, p256dh, auth, user_agent, created_at, last_used)
                  VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM push_subscriptions WHERE endpoint=?), datetime('now')), datetime('now'))''',
               (current_user.id, endpoint, p256dh, auth, request.headers.get('User-Agent', '')[:200], endpoint))
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/api/push/unsubscribe', methods=['POST'])
@login_required
def push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint')
    if endpoint:
        db = get_db()
        db.execute('DELETE FROM push_subscriptions WHERE user_id=? AND endpoint=?',
                   (current_user.id, endpoint))
        db.commit()
        db.close()
    return jsonify({'ok': True})


PUSH_CATEGORIES = [
    ('onboarding_reminder', '🚀 Starter-Roadmap-Reminder', 'Tägliche Erinnerung an offene Tag-Aufgaben (nur Stufe 1)'),
    ('birthday_customer', '🎂 Kunden-Geburtstage', 'Wenn ein Kunde aus deiner Namensliste Geburtstag hat'),
    ('birthday_partner', '🎉 Partner-Geburtstage', 'Wenn ein Partner aus deinem Team Geburtstag hat'),
    ('inactive_alert', '⚠ Inaktive Partner', 'Wenn ein direkter Partner 3+ Tage still ist'),
    ('eingabeschluss', '⏰ Eingabeschluss-Reminder', 'Bei ≤ 2 Tagen bis zum Eingabeschluss'),
    ('daily_routine', '☎ Tägliche Routine (Stufe 1)', 'Morgendlicher Kick-off — 5 Anrufe etc.'),
    ('contract_done', '🎉 Abschluss in deiner Struktur', 'Wenn ein Partner einen Vertrag abschließt'),
    ('appointment_made', '📅 Termin angelegt', 'Direkte Bestätigung wenn du einen Termin setzt.'),
    ('lead_won', '✓ Lead gewonnen', 'Wenn dein Lead-Status auf "gewonnen" wechselt'),
    ('partner_recruited', '◇ Partner rekrutiert', 'Wenn ein neuer Partner in deine Struktur kommt'),
    ('goal_achieved', '🎯 Ziel erreicht', 'Wenn du dein Wochenziel/Karriere-Stufe erreichst'),
    ('vorschlag', '💡 Vorschläge (nur Admin)', 'Wenn ein Partner einen Vorschlag einreicht'),
    ('broadcast', '📢 Broadcasts vom Admin', 'Wichtige Mitteilungen an alle'),
    ('admin_alert', '🚨 System-Alerts (nur Admin)', '24/7-Monitor: App down, DB-Korruption, Backup-Failure'),
]


@app.route('/api/push/prefs', methods=['GET', 'POST'])
@login_required
def push_prefs():
    db = get_db()
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        # Nur erlaubte Keys
        allowed_keys = [k for k, _, _ in PUSH_CATEGORIES]
        prefs = {k: bool(data.get(k)) for k in allowed_keys}
        db.execute('UPDATE users SET push_prefs=? WHERE id=?', (json.dumps(prefs), current_user.id))
        db.commit()
        db.close()
        return jsonify({'ok': True, 'prefs': prefs})
    row = db.execute('SELECT push_prefs FROM users WHERE id=?', (current_user.id,)).fetchone()
    db.close()
    try:
        prefs = json.loads(row['push_prefs']) if row and row['push_prefs'] else {}
    except Exception:
        prefs = {}
    # Defaults: alles an
    full = {k: prefs.get(k, True) for k, _, _ in PUSH_CATEGORIES}
    return jsonify({'ok': True, 'prefs': full, 'categories': [{'key': k, 'label': l, 'desc': d} for k, l, d in PUSH_CATEGORIES]})


@app.route('/push-settings')
@login_required
def push_settings():
    return render_template('push_settings.html', categories=PUSH_CATEGORIES)


@app.route('/api/push/test', methods=['POST'])
@login_required
def push_test():
    """Test-Notification an den User selbst."""
    sent, failed = send_push_to_user(
        current_user.id,
        title='🚀 Coach Test',
        body='Push-Notifications funktionieren! Ab jetzt erfährst du alles wichtige direkt.',
        url='/dashboard',
    )
    return jsonify({'ok': True, 'sent': sent, 'failed': failed})


@app.route('/manifest.json')
def web_app_manifest():
    """PWA Manifest — Android Home-Screen + Browser-Hint 'App installieren'.
    Cache: 1 Tag — Manifest ändert sich selten."""
    resp = jsonify({
        'name': 'Pro Academy',
        'short_name': 'Pro Academy',
        'description': 'Lernen. Wachsen. Erfolgreich sein. · Karriere, Provisionen & Coaching',
        'start_url': '/dashboard',
        'scope': '/',
        'display': 'standalone',
        'display_override': ['standalone', 'minimal-ui'],
        'orientation': 'portrait',
        'theme_color': '#000000',
        'background_color': '#000000',
        'lang': 'de',
        'dir': 'ltr',
        'categories': ['business', 'productivity', 'finance'],
        'icons': [
            {'src': '/static/icons/pa-icon-192.png?v=2', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any maskable'},
            {'src': '/static/icons/pa-icon-512.png?v=2', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any maskable'},
            {'src': '/static/icons/pa-apple-touch.png?v=2', 'sizes': '180x180', 'type': 'image/png'},
        ],
        'shortcuts': [
            {'name': 'Dashboard', 'url': '/dashboard', 'icons': [{'src': '/static/icons/pa-icon-192.png?v=2', 'sizes': '192x192'}]},
            {'name': 'Assistent', 'url': '/assistent', 'icons': [{'src': '/static/icons/pa-icon-192.png?v=2', 'sizes': '192x192'}]},
            {'name': 'Tagesaufgaben', 'url': '/aufgaben', 'icons': [{'src': '/static/icons/pa-icon-192.png?v=2', 'sizes': '192x192'}]},
        ],
    })
    resp.headers['Cache-Control'] = 'public, max-age=86400'  # 1 Tag — Manifest ändert sich selten
    return resp


@app.route('/api/health')
def api_health():
    """Health-Check für Monitoring."""
    try:
        db = get_db()
        user_count = db.execute('SELECT COUNT(*) as c FROM users WHERE active = 1').fetchone()['c']
        db.close()
        return jsonify({
            'status': 'ok',
            'time': datetime.now().isoformat(),
            'users_active': user_count,
            'cache_entries': len(_CACHE),
            'smtp_configured': is_smtp_configured(),
            'ai_configured': is_ai_configured(),
        })
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)[:200]}), 500


@app.route('/profil', methods=['GET', 'POST'])
@login_required
def profil():
    """Eigenes Profil — jeder darf Vision, Passwort, Telefon selbst ändern."""
    db = get_db()
    if request.method == 'POST':
        vision = request.form.get('vision', '').strip()
        phone = request.form.get('phone', '').strip()
        birthday = request.form.get('birthday', '').strip() or None
        # Social-Media-Handles (mit @ normalisieren)
        ig = (request.form.get('instagram_handle') or '').strip().lstrip('@')[:80] or None
        tt = (request.form.get('tiktok_handle') or '').strip().lstrip('@')[:80] or None
        new_password = request.form.get('password', '').strip()
        if new_password:
            db.execute('UPDATE users SET vision=?, phone=?, birthday=?, instagram_handle=?, tiktok_handle=?, password=? WHERE id=?',
                       (vision, phone, birthday, ig, tt, hash_password(new_password), current_user.id))
        else:
            db.execute('UPDATE users SET vision=?, phone=?, birthday=?, instagram_handle=?, tiktok_handle=? WHERE id=?',
                       (vision, phone, birthday, ig, tt, current_user.id))
        db.commit()
        db.close()
        flash('Profil aktualisiert!', 'success')
        return redirect(url_for('profil'))
    user = db.execute('SELECT * FROM users WHERE id = ?', (current_user.id,)).fetchone()
    db.close()
    # Token sicherstellen (bei jedem GET — generiert nur wenn None)
    lead_token = get_or_create_lead_token(current_user.id)
    lead_link = f"{CANONICAL_URL.rstrip('/')}/start?ref={lead_token}"
    user_photo = (user['photo_path'] if user and user['photo_path'] else None)
    return render_template('profil.html', user=user, user_photo=user_photo,
                          lead_link=lead_link, lead_token=lead_token)


# === PUBLIC LEAD-CAPTURE (kein Login) ===
@app.route('/start', methods=['GET', 'POST'])
@app.route('/interesse', methods=['GET', 'POST'])
def public_lead_capture():
    """Öffentliche Lead-Capture-Page für Werbung, Social Media, etc."""
    # Rate-Limit pro IP
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()

    if request.method == 'POST':
        # Rate-Limit Check (max 3 Submissions pro 15 Min pro IP)
        block_key = f'public_lead:{client_ip}'
        if is_login_blocked(block_key):
            return render_template('public_lead.html', error='Zu viele Anfragen. Bitte später erneut versuchen.')

        name = (request.form.get('name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        phone = (request.form.get('phone') or '').strip()
        message = (request.form.get('message') or '').strip()
        wunsch_datum = (request.form.get('wunsch_datum') or '').strip()
        wunsch_zeit = (request.form.get('wunsch_zeit') or '').strip()
        # referred_by: Free-Text Quelle (Instagram/Google/...) oder Berater-Name
        # ref_token: persistenter Token aus dem Lead-Link (?ref=<token>) — Token-Match hat Vorrang
        referred_by = (request.form.get('referred_by') or '').strip()
        ref_token = (request.form.get('ref_token') or '').strip()
        interesse = (request.form.get('interesse') or '').strip().lower()  # 'beratung' | 'karriere'
        privacy = request.form.get('privacy')

        if not name or not email or '@' not in email:
            return render_template('public_lead.html', error='Bitte Name und gültige E-Mail eingeben.', referred_by=referred_by, today_iso=date.today().isoformat())
        if not referred_by or len(referred_by) < 2:
            return render_template('public_lead.html', error='Bitte angeben wie du auf uns aufmerksam geworden bist (Pflichtfeld).', referred_by=referred_by, today_iso=date.today().isoformat())
        if not privacy:
            return render_template('public_lead.html', error='Bitte Datenschutzerklärung akzeptieren.', referred_by=referred_by, today_iso=date.today().isoformat())

        record_login_attempt(block_key, success=False)  # Counter für IP

        # Auto-Zuordnung: 1) Token-Lookup hat Vorrang  2) Name-Match  3) Admin-Fallback
        db = get_db()
        owner_id = None
        matched_berater_name = None  # für Notiz-Tag
        # Schritt 1: Token-Match (URL-Param vom Lead-Link, fälschungssicher)
        if ref_token and ref_token.replace(' ', '').isalnum():
            tok_user = db.execute(
                'SELECT id, name FROM users WHERE lead_token = ? AND active = 1', (ref_token,)
            ).fetchone()
            if tok_user:
                owner_id = tok_user['id']
                matched_berater_name = tok_user['name']
        # Schritt 2: Name/Email-Match nur wenn Token nicht gefunden
        if not owner_id and referred_by:
            ref_user = db.execute(
                'SELECT id, name FROM users WHERE (LOWER(email) = ? OR LOWER(name) LIKE ?) AND active = 1 LIMIT 1',
                (referred_by.lower(), f'%{referred_by.lower()}%')
            ).fetchone()
            if ref_user:
                owner_id = ref_user['id']
                matched_berater_name = ref_user['name']
        if not owner_id:
            # Fallback: Admin
            admin = db.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1 LIMIT 1").fetchone()
            owner_id = admin['id'] if admin else 1

        # Existiert E-Mail schon?
        existing = db.execute('SELECT id FROM leads WHERE LOWER(email) = ?', (email,)).fetchone()
        if existing:
            db.close()
            return render_template('public_lead.html', success=True, duplicate=True)

        # Notiz inkl. Interesse + Quelle/Empfehlung-Tag — sofort sichtbar im CRM
        interesse_tag = ''
        if interesse == 'beratung':
            interesse_tag = '🎯 BERATUNG · '
        elif interesse == 'karriere':
            interesse_tag = '🚀 KARRIERE · '
        # Quelle-Tag: wenn Berater-Match → "Empfohlen von <Name>", sonst → "Quelle: <Eingabe>"
        quelle_tag = ''
        if matched_berater_name:
            quelle_tag = f' · 🤝 Empfohlen von {matched_berater_name}'
        elif referred_by:
            quelle_tag = f' · Quelle: {referred_by[:60]}'
        notizen_full = f'{interesse_tag}Über Online-Form{quelle_tag}. {message}' if (interesse_tag or message or quelle_tag) else 'Über Online-Form.'
        # Lead-Liste-Typ: Beratung → vk (Vertrieb), Karriere → rk (Recruiting)
        liste_typ = 'rk' if interesse == 'karriere' else 'vk'
        cur = db.execute('''INSERT INTO leads (owner_id, name, email, phone, status, notizen,
                            source, public_message, referred_by, liste_typ)
                            VALUES (?, ?, ?, ?, 'neu', ?, 'public', ?, ?, ?)''',
                       (owner_id, name, email, phone, notizen_full,
                        message, referred_by, liste_typ))
        new_id = cur.lastrowid

        # ─── PUNKT B: Termin-Wunsch → Auto-Termin im Kalender des Beraters ───
        termin_extra = ''
        if wunsch_datum and len(wunsch_datum) == 10:
            try:
                # Validiere Datum (YYYY-MM-DD)
                _ = datetime.strptime(wunsch_datum, '%Y-%m-%d').date()
                termin_title = f'📞 Jibson & Team · Erstgespräch mit {name}'
                # Quelle in die Notizen mit reinpacken (so steht „durch wen gekommen" am Termin)
                quelle_note = f'Aufmerksam via: {referred_by}' if referred_by else 'Aufmerksam: (keine Angabe)'
                termin_notes = f'{quelle_note}\n{phone or "(kein Tel.)"} · {email}\nAuto via /start.'
                if message:
                    termin_notes += f'\n„{message[:200]}"'
                db.execute('''INSERT INTO appointments
                              (owner_id, title, client_name, termin_date, termin_time,
                               typ, status, notizen)
                              VALUES (?, ?, ?, ?, ?, 'kundentermin', 'geplant', ?)''',
                           (owner_id, termin_title, name, wunsch_datum, wunsch_zeit or '14:00', termin_notes))
                termin_extra = f' + Termin {wunsch_datum} {wunsch_zeit or "14:00"}'
            except Exception as e:
                print(f'[public_lead] Termin-Auto-Insert fehlgeschlagen: {e}')

        db.commit()
        db.close()

        # Push an Berater bei Termin-Wunsch
        if termin_extra:
            try:
                send_push_to_user(owner_id,
                    title=f'📅 Neuer Lead WILL Termin: {name}',
                    body=f'{wunsch_datum} {wunsch_zeit or "14:00"} · ruf zurück, bestätige',
                    url='/termine', urgent=True, tag='public_lead_termin',
                    push_type='lead_won')
            except Exception:
                pass

        log_activity(owner_id, 'public_lead',
                    f'🌐 Neue öffentliche Anmeldung: {name} ({email}){termin_extra}',
                    icon='🌐', color='gold')

        # E-Mails: KEINE automatische Bestätigung an den Bewerber.
        # Stattdessen geht der Lead in die Owner-Inbox + Admin-Notification.
        # Bestätigungs-Mail wird erst manuell vom Owner versendet (oder bei
        # Admin-Genehmigung — siehe /genehmigungen/<id>/bestaetigen).
        if is_smtp_configured():
            admin_email = (get_setting('smtp_from_email') or '').strip()
            if admin_email:
                try:
                    notify_text = f"Neue Anmeldung über öffentliche Form:\n\nName: {name}\nE-Mail: {email}\nTelefon: {phone}\n\nNachricht:\n{message or '(keine)'}\n\nEmpfohlen von: {referred_by or '–'}\n\n→ Direkt bearbeiten: http://localhost:5001/admin/inbox"
                    send_email(admin_email, f'Neue Anmeldung: {name}', notify_text, sent_by=None, category='signup')
                except Exception:
                    pass

        return render_template('public_lead.html', success=True)

    # GET: Form anzeigen
    db = get_db()
    # Optional ?ref=<wert> aus URL — Token (name-slug oder altes Random)
    # ODER Berater-Name (Backwards-kompatibel)
    ref = request.args.get('ref', '').strip().lower()
    ref_token = ''
    referred_by_prefill = request.args.get('ref', '').strip()
    matched_owner_display = ''
    # Token-Erkennung: 3-50 Zeichen, nur a-z0-9-
    if ref and 3 <= len(ref) <= 50:
        import re
        if re.match(r'^[a-z0-9-]+$', ref):
            owner = db.execute(
                'SELECT id, name FROM users WHERE lead_token = ? AND active = 1', (ref,)
            ).fetchone()
            if owner:
                ref_token = ref
                referred_by_prefill = ''  # nicht nötig — Banner zeigt's
                matched_owner_display = owner['name']
    db.close()
    return render_template('public_lead.html',
                           referred_by=referred_by_prefill,
                           ref_token=ref_token,
                           matched_owner_display=matched_owner_display,
                           today_iso=date.today().isoformat())


# === PUBLIC BOOKING — Kalendly-Style Slot-Picker ===
def _generate_booking_slots(owner_id, days_ahead=14):
    """Kalendly-Style: 30-Min-Slots Mo-Fr 9:00-17:30, Sa/So gesperrt.
    Bestehende Owner-Termine sperren auch ±30 Min als Puffer."""
    db = get_db()
    today = date.today()
    end_date = today + timedelta(days=days_ahead)
    booked_rows = db.execute('''
        SELECT termin_date, termin_time FROM appointments
        WHERE owner_id=? AND termin_date >= ? AND termin_date <= ?
              AND status IN ('geplant', 'bestätigt')
    ''', (owner_id, today.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))).fetchall()
    db.close()

    # Booked-Set: jeder Termin sperrt sich SELBST + Slot davor + Slot danach (30 Min Puffer)
    booked_set = set()
    for r in booked_rows:
        if not r['termin_time']:
            continue
        try:
            tdt = datetime.strptime(f"{r['termin_date']} {r['termin_time'][:5]}", '%Y-%m-%d %H:%M')
        except ValueError:
            continue
        # Termin-Slot + ±30 Min Puffer
        for delta_min in (-30, 0, 30):
            blocked = tdt + timedelta(minutes=delta_min)
            booked_set.add(blocked.strftime('%Y-%m-%d_%H:%M'))

    # Slot-Definition: Mo-Fr 9:00-17:30 in 30-Min-Schritten (18 Slots/Tag)
    SLOT_START_HOUR = 9
    SLOT_END_HOUR = 18  # last slot starts 17:30, ends 18:00
    weekday_slots = []
    h = SLOT_START_HOUR
    while h < SLOT_END_HOUR:
        weekday_slots.append(f'{h:02d}:00')
        weekday_slots.append(f'{h:02d}:30')
        h += 1

    days = []
    for offset in range(0, days_ahead):
        d = today + timedelta(days=offset)
        weekday = d.weekday()  # 0=Mo, 5=Sa, 6=So
        if weekday >= 5:  # Sa + So gesperrt
            continue
        slots = []
        for t in weekday_slots:
            # Heute: nur Slots in der Zukunft (mind. 2h Puffer)
            if offset == 0:
                slot_dt = datetime.combine(d, datetime.strptime(t, '%H:%M').time())
                if slot_dt < datetime.now() + timedelta(hours=2):
                    continue
            key = f"{d.strftime('%Y-%m-%d')}_{t}"
            slots.append({
                'time': t,
                'available': key not in booked_set,
                'datetime_iso': f"{d.strftime('%Y-%m-%d')}T{t}",
            })
        days.append({
            'date': d,
            'date_iso': d.strftime('%Y-%m-%d'),
            'date_short': d.strftime('%d.%m.'),
            'weekday_short': ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'][weekday],
            'slots': slots,
            'has_free': any(s['available'] for s in slots),
        })
    return days


@app.route('/buchen', methods=['GET', 'POST'])
@app.route('/termin', methods=['GET', 'POST'])
def public_booking():
    """Kalendly-ähnliche Buchung: Lead wählt Slot → Termin + Lead automatisch im CRM."""
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
    block_key = f'public_booking:{client_ip}'

    # Owner ermitteln (Admin als Default oder via ?ref=)
    db = get_db()
    ref = (request.args.get('ref') or request.form.get('referred_by') or '').strip()
    owner_id = None
    if ref:
        ref_user = db.execute(
            "SELECT id FROM users WHERE LOWER(email) = ? OR LOWER(name) LIKE ? AND active = 1",
            (ref.lower(), f'%{ref.lower()}%')
        ).fetchone()
        if ref_user:
            owner_id = ref_user['id']
    if not owner_id:
        admin = db.execute("SELECT id, name FROM users WHERE role = 'admin' AND active = 1 LIMIT 1").fetchone()
        owner_id = admin['id'] if admin else 1
    owner_row = db.execute('SELECT name FROM users WHERE id=?', (owner_id,)).fetchone()
    owner_name = owner_row['name'] if owner_row else 'Najib'
    db.close()

    if request.method == 'POST':
        if is_login_blocked(block_key):
            return render_template('booking.html', error='Zu viele Anfragen — bitte später nochmal.',
                                   days=_generate_booking_slots(owner_id), owner_name=owner_name)

        name = (request.form.get('name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        phone = (request.form.get('phone') or '').strip()
        slot_iso = (request.form.get('slot') or '').strip()  # Format: 2026-05-15T14:00
        message = (request.form.get('message') or '').strip()
        interesse = (request.form.get('interesse') or 'beratung').strip().lower()
        privacy = request.form.get('privacy')

        if not name or not email or '@' not in email:
            return render_template('booking.html', error='Bitte Name + gültige E-Mail eingeben.',
                                   days=_generate_booking_slots(owner_id), owner_name=owner_name)
        if not slot_iso or 'T' not in slot_iso:
            return render_template('booking.html', error='Bitte einen Termin-Slot wählen.',
                                   days=_generate_booking_slots(owner_id), owner_name=owner_name)
        if not privacy:
            return render_template('booking.html', error='Bitte Datenschutzerklärung akzeptieren.',
                                   days=_generate_booking_slots(owner_id), owner_name=owner_name)

        record_login_attempt(block_key, success=False)

        # Slot parsen
        try:
            slot_date, slot_time = slot_iso.split('T')
            datetime.strptime(slot_date, '%Y-%m-%d')
            datetime.strptime(slot_time, '%H:%M')
        except Exception:
            return render_template('booking.html', error='Ungültiger Termin-Slot.',
                                   days=_generate_booking_slots(owner_id), owner_name=owner_name)

        # Conflict-Check: Slot noch frei?
        db = get_db()
        conflict = db.execute('''SELECT id FROM appointments
                                 WHERE owner_id=? AND termin_date=? AND termin_time=?
                                       AND status IN ('geplant','bestätigt')''',
                              (owner_id, slot_date, slot_time)).fetchone()
        if conflict:
            db.close()
            return render_template('booking.html', error='Dieser Slot wurde gerade weggebucht — bitte einen anderen wählen.',
                                   days=_generate_booking_slots(owner_id), owner_name=owner_name)

        # 1. Lead anlegen
        interesse_tag = '🎯 BERATUNG · ' if interesse == 'beratung' else '🚀 KARRIERE · '
        notizen_lead = f'{interesse_tag}Termin direkt online gebucht: {slot_date} um {slot_time} Uhr. {message}'
        liste_typ = 'rk' if interesse == 'karriere' else 'vk'
        cur = db.execute('''INSERT INTO leads (owner_id, name, email, phone, status, notizen,
                            source, public_message, liste_typ)
                            VALUES (?, ?, ?, ?, 'kontakt', ?, 'public-booking', ?, ?)''',
                       (owner_id, name, email, phone, notizen_lead, message, liste_typ))
        lead_id = cur.lastrowid

        # 2. Termin anlegen
        title = f"{'Beratung' if interesse == 'beratung' else 'Kennenlern-Gespräch'}: {name}"
        notizen_termin = f'📞 Online-Buchung · Lead-ID #{lead_id} · {email}{" · " + phone if phone else ""}'
        db.execute('''INSERT INTO appointments
                      (owner_id, title, client_name, termin_date, termin_time, typ, status, notizen)
                      VALUES (?, ?, ?, ?, ?, 'kundentermin', 'geplant', ?)''',
                   (owner_id, title, name, slot_date, slot_time, notizen_termin))
        db.commit()
        db.close()

        # Activity-Log + Push
        log_activity(owner_id, 'booking',
                     f'📅 Online-Termin gebucht: {name} am {slot_date} um {slot_time}',
                     icon='📅', color='gold')
        try:
            send_push_to_user(owner_id,
                              title=f'📅 Neuer Online-Termin!',
                              body=f'{name} hat Slot {slot_date} {slot_time} gebucht',
                              url='/termine', urgent=True, tag='booking',
                              push_type='appointment_made')
        except Exception:
            pass

        # Cache invalidieren
        cache_invalidate('ctx:'); cache_invalidate('coach_acts:')

        # Bestätigungs-Mail wenn SMTP konfiguriert
        if is_smtp_configured():
            try:
                send_email(email,
                           f'Termin bestätigt: {slot_date} um {slot_time}',
                           f'Hi {name.split()[0]},\n\nvielen Dank für deine Buchung!\n\nDein Termin: {slot_date} um {slot_time} Uhr\nMit: {owner_name}\n\nIch melde mich kurz vorher mit Details.\n\nBis bald!\n{owner_name}',
                           sent_by=None, category='termin')
            except Exception:
                pass

        return render_template('booking.html', success=True, slot_date=slot_date, slot_time=slot_time, owner_name=owner_name)

    # GET: Slot-Picker zeigen
    days = _generate_booking_slots(owner_id)
    return render_template('booking.html', days=days, owner_name=owner_name, ref=ref)


@app.route('/strukturbomben')
@login_required
def strukturbomben():
    """Highlight-Feed: alle wichtigen Events der Struktur (Aufstiege, Großvertraege, neue Partner, Streaks)."""
    db = get_db()
    # Scope = current_user + komplette Downline
    ids = [current_user.id] + get_all_descendants(current_user.id)
    ph = ','.join('?' * len(ids))
    bombs = []

    # 🎯 Großverträge der letzten 30 Tage (≥3000 EH oder Volumen ≥ 10k)
    big_contracts = db.execute(f'''
        SELECT c.id, c.client_name, c.einheiten, c.volumen, c.abschluss_date, c.created_at,
               u.id as uid, u.name as owner_name
        FROM contracts c JOIN users u ON c.owner_id = u.id
        WHERE c.owner_id IN ({ph})
          AND c.status = 'abgeschlossen'
          AND (c.einheiten >= 3000 OR c.volumen >= 10000)
          AND date(COALESCE(c.abschluss_date, c.created_at)) >= date('now','-30 days')
        ORDER BY date(COALESCE(c.abschluss_date, c.created_at)) DESC LIMIT 20
    ''', ids).fetchall()
    for c in big_contracts:
        bombs.append({
            'when': c['abschluss_date'] or c['created_at'],
            'icon': '🎯', 'color': '#d4a843',
            'title': f'Großvertrag: {int(c["einheiten"] or 0):,} EH'.replace(',', '.'),
            'subtitle': f'{c["owner_name"]} · {c["client_name"] or "Kunde"} · {int(c["volumen"] or 0):,}€'.replace(',', '.'),
            'link': f'/vertraege?focus={c["id"]}',
            'kind': 'großvertrag',
        })

    # 🆕 Neue Partner (joined letzten 14 Tagen)
    new_partners = db.execute(f'''
        SELECT id, name, joined_date, parent_id,
               (SELECT name FROM users WHERE id = u.parent_id) as mentor_name
        FROM users u
        WHERE id IN ({ph}) AND active = 1
          AND date(joined_date) >= date('now','-14 days')
          AND id != ?
        ORDER BY joined_date DESC LIMIT 20
    ''', ids + [current_user.id]).fetchall()
    for p in new_partners:
        bombs.append({
            'when': p['joined_date'],
            'icon': '🆕', 'color': '#22c55e',
            'title': f'Neuer Partner: {p["name"]}',
            'subtitle': f'Mentor: {p["mentor_name"] or "—"}',
            'link': f'/partner/{p["id"]}/profil',
            'kind': 'neuer_partner',
        })

    # 🔥 Streak-Stars (≥7 Tage)
    streak_stars = db.execute(f'''
        SELECT id, name, streak_days FROM users
        WHERE id IN ({ph}) AND active = 1 AND COALESCE(streak_days,0) >= 7
        ORDER BY streak_days DESC LIMIT 10
    ''', ids).fetchall()
    today_iso = date.today().isoformat()
    for s in streak_stars:
        bombs.append({
            'when': today_iso,
            'icon': '🔥', 'color': '#f97316',
            'title': f'Streak-Star: {s["name"]} · {s["streak_days"]} Tage in Folge',
            'subtitle': 'tägliche Aktivität ohne Unterbrechung',
            'link': f'/partner/{s["id"]}/profil',
            'kind': 'streak',
        })

    # 📈 Top-Performer der Woche (meiste abgeschlossene Verträge in 7 Tagen)
    top_performers = db.execute(f'''
        SELECT u.id, u.name, COUNT(c.id) as n_contracts, COALESCE(SUM(c.einheiten),0) as eh
        FROM users u JOIN contracts c ON c.owner_id = u.id
        WHERE u.id IN ({ph}) AND c.status = 'abgeschlossen'
          AND date(COALESCE(c.abschluss_date, c.created_at)) >= date('now','-7 days')
        GROUP BY u.id, u.name HAVING n_contracts >= 2
        ORDER BY eh DESC LIMIT 5
    ''', ids).fetchall()
    for t in top_performers:
        bombs.append({
            'when': today_iso,
            'icon': '📈', 'color': '#3b82f6',
            'title': f'Top-Woche: {t["name"]}',
            'subtitle': f'{t["n_contracts"]} Verträge · {int(t["eh"]):,} EH in 7 Tagen'.replace(',', '.'),
            'link': f'/partner/{t["id"]}/profil',
            'kind': 'top_performer',
        })
    db.close()
    # Sort all by date desc
    bombs.sort(key=lambda b: b['when'] or '', reverse=True)
    return render_template('strukturbomben.html', bombs=bombs, total_team=len(ids))


@app.route('/admin/switch/<int:target_id>', methods=['POST'])
@login_required
def switch_user(target_id):
    """Impersonate als Geschäftspartner — Admin oder Upliner mit target in eigener Downline.
    Speichert Original-User-ID in session damit man zurück-switchen kann.
    Audit-Log: jeder Switch wird protokolliert."""
    descendants = get_all_descendants(current_user.id)
    if not (current_user.has_admin_access or target_id in descendants):
        flash('Keine Berechtigung — nur Admin oder Downline-Upliner.', 'error')
        return redirect(url_for('dashboard'))
    if target_id == current_user.id:
        return redirect(url_for('dashboard'))
    db = get_db()
    target = db.execute('SELECT * FROM users WHERE id=? AND active=1', (target_id,)).fetchone()
    db.close()
    if not target:
        flash('Geschäftspartner nicht gefunden oder inaktiv.', 'error')
        return redirect(url_for('dashboard'))
    # Original-ID merken VOR logout (nur beim ersten Switch — nested switches behalten den ersten)
    original_id = session.get('impersonator_id') or current_user.id
    original_name = session.get('impersonator_name') or current_user.name
    log_activity(original_id, 'switch_in',
                 f'⇄ Switch in Kontext von {target["name"]}', icon='⇄', color='gold')
    logout_user()
    login_user(User(target), remember=False)
    session['impersonator_id'] = original_id
    session['impersonator_name'] = original_name
    session.permanent = True
    flash(f'Du arbeitest jetzt als {target["name"]}. Klick „Zurück" im Banner oben um wieder zu dir zu wechseln.', 'info')
    return redirect(url_for('dashboard'))


@app.route('/admin/switch-back', methods=['POST'])
@login_required
def switch_back():
    """Zurück zum Original-User aus Impersonation."""
    original_id = session.get('impersonator_id')
    if not original_id or original_id == current_user.id:
        return redirect(url_for('dashboard'))
    db = get_db()
    orig = db.execute('SELECT * FROM users WHERE id=?', (original_id,)).fetchone()
    db.close()
    if not orig:
        # Original nicht mehr da — komplett ausloggen
        session.pop('impersonator_id', None)
        session.pop('impersonator_name', None)
        return redirect(url_for('logout'))
    impersonated_name = current_user.name
    log_activity(original_id, 'switch_back',
                 f'⇄ Zurück von {impersonated_name}', icon='⇄', color='gold')
    logout_user()
    login_user(User(orig), remember=True)
    session.pop('impersonator_id', None)
    session.pop('impersonator_name', None)
    flash(f'Zurück in deinem eigenen Account.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/api/deploy', methods=['POST'])
def api_deploy():
    """GitHub-Webhook-Receiver: pullt main + reload WSGI bei jedem Push.
    Verifiziert HMAC-SHA256 Signature mit Secret aus app_settings.
    Idempotent: ignoriert Pushes auf andere Branches."""
    secret = get_setting('deploy_webhook_secret')
    if not secret:
        return jsonify({'error': 'Webhook nicht konfiguriert — geh zu /admin/deploy'}), 503

    # 1) Signature verifizieren (HMAC-SHA256)
    sig_header = request.headers.get('X-Hub-Signature-256') or ''
    if not sig_header.startswith('sha256='):
        return jsonify({'error': 'Invalid X-Hub-Signature-256'}), 403
    expected = 'sha256=' + hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_header):
        return jsonify({'error': 'Signature mismatch'}), 403

    # 2) Nur Pushes auf main durchlassen (ignoriere Tags, andere Branches, ping)
    event = request.headers.get('X-GitHub-Event', '')
    payload = request.get_json(silent=True) or {}
    if event == 'ping':
        return jsonify({'status': 'pong', 'msg': 'Webhook erreichbar'}), 200
    if event != 'push' or payload.get('ref') != 'refs/heads/main':
        return jsonify({'status': 'ignored', 'event': event, 'ref': payload.get('ref')}), 200

    # 3) git pull --ff-only + WSGI touch
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    wsgi_path = '/var/www/proacademy-business_de_wsgi.py'
    sha = (payload.get('after') or '')[:8]
    msg = ((payload.get('head_commit') or {}).get('message') or '')[:200]
    try:
        pull = subprocess.run(['git', 'pull', '--ff-only'], cwd=repo_dir,
                              capture_output=True, text=True, timeout=60)
        out = ((pull.stdout or '') + (pull.stderr or ''))[:1500]
        if pull.returncode != 0:
            db = get_db()
            db.execute('INSERT INTO deploy_log (sha, message, status, output, triggered_by) VALUES (?,?,?,?,?)',
                       (sha, msg, 'pull_failed', out, 'webhook'))
            db.commit(); db.close()
            return jsonify({'status': 'pull_failed', 'output': out[-500:]}), 500
        # Touch WSGI to reload Flask app
        wsgi_msg = ''
        if os.path.isfile(wsgi_path):
            try:
                os.utime(wsgi_path, None)
                wsgi_msg = ' · WSGI touched'
            except Exception as e:
                wsgi_msg = f' · WSGI touch failed: {e}'
        db = get_db()
        db.execute('INSERT INTO deploy_log (sha, message, status, output, triggered_by) VALUES (?,?,?,?,?)',
                   (sha, msg, 'ok', out + wsgi_msg, 'webhook'))
        db.commit(); db.close()
        return jsonify({'status': 'deployed', 'sha': sha, 'message': msg, 'wsgi': wsgi_msg.strip()}), 200
    except subprocess.TimeoutExpired:
        return jsonify({'status': 'timeout', 'msg': 'git pull > 60s'}), 504
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)[:300]}), 500


@app.route('/admin/deploy')
@login_required
def admin_deploy():
    """Auto-Deploy-Konfiguration: zeigt Webhook-URL + Secret + History."""
    if not current_user.has_admin_access:
        flash('Nur Admin', 'error')
        return redirect(url_for('dashboard'))
    secret = get_setting('deploy_webhook_secret')
    if not secret:
        secret = secrets.token_urlsafe(32)
        set_setting('deploy_webhook_secret', secret)
    db = get_db()
    deploys = db.execute('SELECT * FROM deploy_log ORDER BY id DESC LIMIT 30').fetchall()
    db.close()
    base = (request.url_root or '').rstrip('/')
    webhook_url = f'{base}/api/deploy'
    return render_template('admin_deploy.html', secret=secret, webhook_url=webhook_url, deploys=deploys)


@app.route('/admin/deploy/manual', methods=['POST'])
@login_required
def admin_deploy_manual():
    """Manueller Deploy-Trigger via Admin-UI (z.B. wenn Webhook fail)."""
    if not current_user.has_admin_access:
        return redirect(url_for('admin_deploy'))
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    wsgi_path = '/var/www/proacademy-business_de_wsgi.py'
    try:
        pull = subprocess.run(['git', 'pull', '--ff-only'], cwd=repo_dir,
                              capture_output=True, text=True, timeout=60)
        out = ((pull.stdout or '') + (pull.stderr or ''))[:1500]
        ok = pull.returncode == 0
        if ok and os.path.isfile(wsgi_path):
            os.utime(wsgi_path, None)
        db = get_db()
        db.execute('INSERT INTO deploy_log (sha, message, status, output, triggered_by) VALUES (?,?,?,?,?)',
                   ('manual', f'Trigger durch {current_user.name}', 'ok' if ok else 'fail', out, 'admin_ui'))
        db.commit(); db.close()
        flash('✓ Deploy ausgeführt' if ok else f'✗ Deploy fail: {out[-200:]}', 'success' if ok else 'error')
    except Exception as e:
        flash(f'Deploy-Exception: {e}', 'error')
    return redirect(url_for('admin_deploy'))


CONTENT_QUESTIONS = [
    ('motivation', 'Was treibt dich täglich an im Vertrieb?',
     'Stichworte reichen — z.B. Familie, Freiheit, Status, Impact.'),
    ('zielgruppe', 'Wen willst du primär ansprechen?',
     'z.B. junge Berufseinsteiger 25-35, Selbstständige, Familien mit Kindern, Karriere-Wechsler.'),
    ('region', 'In welcher Region/Stadt arbeitest du?',
     'z.B. München, Großraum Stuttgart, Berlin, ganz DACH online.'),
    ('alleinstellung', 'Was unterscheidet dich von anderen Vertriebspartnern?',
     'Was sagen Kunden/Partner über dich? z.B. „immer erreichbar", „erklärt einfach".'),
    ('wertversprechen', 'Was versprichst du Kunden konkret?',
     'In 1 Satz: „Ich helfe dir / wir machen X / du bekommst Y."'),
    ('format_lieblings', 'Welches Content-Format magst DU am liebsten zu drehen?',
     'Reels, Talking Head, Carousel, Story, Live, Foto-Posts mit Caption?'),
    ('themen_top3', 'Deine 3 Lieblings-Themen die du erklären könntest?',
     'z.B. ETF-Sparplan, Berufsunfähigkeit, Karriere im Vertrieb, BAV…'),
    ('persona', 'Welcher Stil passt zu dir?',
     'professionell-ruhig, motivierend-laut, story-getrieben-emotional, witzig-locker, daten-driven?'),
    ('tabu', 'Welche Themen oder Sprachstile NICHT?',
     'z.B. „kein Politik-Content", „kein Schimpfwörter", „nicht zu pushy".'),
    ('cta', 'Was soll der typische Call-to-Action sein?',
     'z.B. „DM für Termin", „Link in Bio", „Kommentar mit BERATUNG", „Website besuchen".'),
]

# 7-Tage-Schema: Mo bis So
CONTENT_SCHEMA_7DAYS = [
    ('Montag', 'Story', 'Persönliche Geschichte oder Erfolgs-Anekdote — emotional einsteigen.'),
    ('Dienstag', 'Tipp', 'Konkreter Mehrwert: ein Tipp den deine Zielgruppe sofort nutzen kann.'),
    ('Mittwoch', 'Frage', 'Frage in die Community stellen — Engagement bauen.'),
    ('Donnerstag', 'Behind the Scenes', 'Zeig wie du arbeitest — Termin, Vorbereitung, Tag im Leben.'),
    ('Freitag', 'Erfolg', 'Kunden- oder Partner-Erfolg feiern. Vorher/Nachher, Zahlen, Zitat.'),
    ('Samstag', 'Provokant', 'Mut zu Meinung — gegen Mainstream, eigene Sicht klar machen.'),
    ('Sonntag', 'Inspiration', 'Motivation für die neue Woche — Spruch, Vision, dein Warum.'),
]


def _content_profile_get(user_id):
    db = get_db()
    row = db.execute('SELECT antworten_json FROM content_profile WHERE user_id=?', (user_id,)).fetchone()
    db.close()
    if row and row['antworten_json']:
        try:
            return json.loads(row['antworten_json'])
        except Exception:
            return {}
    return {}


def _content_suggestion(profile, day_idx):
    """Generiert einen tagespassenden Vorschlag aus Profil + Schema."""
    day_name, format_typ, beschreibung = CONTENT_SCHEMA_7DAYS[day_idx]
    persona = profile.get('persona', '')
    zielgruppe = profile.get('zielgruppe', '')
    themen = profile.get('themen_top3', '')
    cta = profile.get('cta', '')
    region = profile.get('region', '')
    format_lieb = profile.get('format_lieblings', '')

    # Einfaches Prompt-Template — fügt Profil-Bezug ein
    suggestion = {
        'day_name': day_name,
        'format_typ': format_typ,
        'beschreibung': beschreibung,
        'idee': '',
        'cta_hinweis': cta or 'DM für Termin · Link in Bio',
    }
    # Konkrete Idee je nach Tag
    ideen = {
        0: f'Erzähl wie du {persona or "dich"} im Vertrieb gefunden hast — der Wendepunkt-Moment für {zielgruppe or "deine Zielgruppe"}.',
        1: f'Ein konkreter Tipp aus {themen.split(",")[0].strip() if themen else "deinem Top-Thema"} — den deine Zielgruppe HEUTE umsetzen kann. Max 60s.',
        2: f'Frage stellen: „Was ist eure größte Sorge bei {themen.split(",")[0].strip() if themen else "Geld/Vorsorge"}?" — antworte auf alle Kommentare.',
        3: f'Zeig ein Termin-Setup{" in " + region if region else ""}: Café-Tisch, Notizblock, Tee — was Kunden NICHT sehen.',
        4: f'Erfolg von einem Kunden/Partner teilen — anonym OK. Was hat sich konkret bei ihm verändert?',
        5: f'Eine Meinung die viele nicht hören wollen — z.B. „Ohne 5 Anrufe pro Tag wirst du im Vertrieb nicht reich".',
        6: f'Inspiration für die Woche — dein „Warum" in einem Satz. Was treibt dich morgens aus dem Bett?',
    }
    suggestion['idee'] = ideen.get(day_idx, '')
    suggestion['format_lieb'] = format_lieb
    return suggestion


@app.route('/content-coach', methods=['GET'])
@login_required
def content_coach():
    """Content-Coach Hauptseite: Tagesvorschlag + Setup-Link."""
    profile = _content_profile_get(current_user.id)
    setup_done = bool(profile and profile.get('motivation'))
    day_idx = date.today().weekday()  # 0=Mo, 6=So
    suggestion = _content_suggestion(profile, day_idx) if setup_done else None
    # 7-Tage-Übersicht
    week = []
    for i in range(7):
        s = _content_suggestion(profile, i)
        s['is_today'] = (i == day_idx)
        week.append(s)
    return render_template('content_coach.html',
        profile=profile, setup_done=setup_done,
        suggestion=suggestion, week=week, today=date.today().isoformat())


@app.route('/content-coach/setup', methods=['GET', 'POST'])
@login_required
def content_coach_setup():
    """Interview-Form: User antwortet auf 10 Fragen → Profil wird gespeichert."""
    if request.method == 'POST':
        antworten = {}
        for key, _q, _hint in CONTENT_QUESTIONS:
            v = (request.form.get(key) or '').strip()[:600]
            if v:
                antworten[key] = v
        db = get_db()
        db.execute('''INSERT INTO content_profile (user_id, antworten_json, updated_at)
                      VALUES (?, ?, CURRENT_TIMESTAMP)
                      ON CONFLICT(user_id) DO UPDATE SET
                        antworten_json=excluded.antworten_json,
                        updated_at=CURRENT_TIMESTAMP''',
                  (current_user.id, json.dumps(antworten, ensure_ascii=False)))
        db.commit()
        db.close()
        flash(f'Content-Profil gespeichert · {len(antworten)} Antworten erfasst.', 'success')
        return redirect(url_for('content_coach'))
    profile = _content_profile_get(current_user.id)
    return render_template('content_coach_setup.html',
        questions=CONTENT_QUESTIONS, profile=profile)


IMPRESSUM_FIELDS = [
    ('imp_name', 'Vor- und Nachname / Firma', 'Najib Tchatikpi · Pro Academy'),
    ('imp_strasse', 'Straße + Hausnummer', ''),
    ('imp_plz_ort', 'PLZ + Ort', ''),
    ('imp_land', 'Land', 'Deutschland'),
    ('imp_email', 'E-Mail-Adresse', 'najib@ntpro.de'),
    ('imp_telefon', 'Telefon', ''),
    ('imp_ust', 'USt-IdNr (falls vorhanden)', ''),
    ('imp_handelsregister', 'Handelsregister-Nummer (falls Verein/GmbH)', ''),
    ('imp_verantwortlich', 'Verantwortlich für Inhalt (§18 MStV)', 'Najib Tchatikpi'),
    ('imp_beschreibung', 'Tätigkeit (kurze Beschreibung)',
     'Pro Academy ist eine Selbstorganisation zur Förderung von Karriereeinstieg im Strukturvertrieb. Wir bringen Menschen zusammen die in einem etablierten Vertriebssystem starten möchten und unterstützen sie mit Tools, Coaching und Community.'),
]


@app.route('/inbox')
@login_required
def inbox():
    """In-App-Notification-Center — zeigt letzte Push-Notifications des Users.
    Ersatz für E-Mail-Versand bei Routine-Events (Reminder, Termine, Aufstiege etc.)."""
    db = get_db()
    rows = db.execute('''SELECT id, push_type, ref_key, sent_at
                         FROM push_log WHERE user_id=?
                         ORDER BY sent_at DESC LIMIT 100''', (current_user.id,)).fetchall()
    db.close()
    # Map push_type → label + icon (für UI)
    type_meta = {
        'daily_motivate': ('Daily Motivation', '☀'),
        'admin_alert': ('Admin-Alarm', '⚠'),
        'audit-fail': ('System-Fehler', '✗'),
        'patch_note': ('Update', '★'),
        'newsletter': ('Branchen-News', '▢'),
        'birthday': ('Geburtstag', '◯'),
        'streak': ('Streak', '◉'),
        'registrierung': ('Neue Anmeldung', '◎'),
        'audit-fail': ('Audit-Fail', '⚠'),
    }
    notifications = []
    for r in rows:
        meta = type_meta.get(r['push_type'], (r['push_type'] or 'Benachrichtigung', '●'))
        notifications.append({
            'id': r['id'],
            'label': meta[0],
            'icon': meta[1],
            'detail': r['ref_key'] or '',
            'sent_at': r['sent_at'],
        })
    return render_template('inbox.html', notifications=notifications)


@app.route('/impressum')
def impressum_page():
    """Öffentliches Impressum — rendert aus app_settings."""
    data = {key: get_setting(key, default) for key, _label, default in IMPRESSUM_FIELDS}
    return render_template('impressum.html', data=data)


@app.route('/admin/impressum', methods=['GET', 'POST'])
@login_required
def admin_impressum():
    """Editor für Impressum-Felder (nur Admin)."""
    if not current_user.has_admin_access:
        flash('Nur Admin', 'error')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        for key, _label, _default in IMPRESSUM_FIELDS:
            v = (request.form.get(key) or '').strip()[:1000]
            set_setting(key, v)
        flash('✓ Impressum gespeichert', 'success')
        return redirect(url_for('admin_impressum'))
    data = {key: get_setting(key, default) for key, _label, default in IMPRESSUM_FIELDS}
    return render_template('admin_impressum.html', fields=IMPRESSUM_FIELDS, data=data)


@app.route('/whats-new')
@login_required
def whats_new():
    """Patch-Notes — User sieht alle Updates + markiert als gelesen."""
    db = get_db()
    patches = db.execute('''
        SELECT p.*,
               (SELECT seen_at FROM patch_notes_seen WHERE user_id=? AND patch_id=p.id) AS seen_at
        FROM patch_notes p ORDER BY p.published_at DESC LIMIT 50
    ''', (current_user.id,)).fetchall()
    # Auto-mark als gelesen für unread
    marked = 0
    for p in patches:
        if not p['seen_at']:
            db.execute('INSERT OR IGNORE INTO patch_notes_seen (user_id, patch_id) VALUES (?, ?)',
                      (current_user.id, p['id']))
            marked += 1
    db.commit()
    db.close()
    if marked:
        cache_invalidate(f'ctx:patch_unread:{current_user.id}')
    return render_template('whats_new.html', patches=patches)


@app.route('/api/whats-new/unread')
@login_required
def api_whats_new_unread():
    """Counter für Sidebar-Badge — anzahl ungelesener Patches."""
    db = get_db()
    n = db.execute('''SELECT COUNT(*) c FROM patch_notes p
                      WHERE NOT EXISTS (SELECT 1 FROM patch_notes_seen
                                        WHERE user_id=? AND patch_id=p.id)''',
                  (current_user.id,)).fetchone()['c']
    db.close()
    return jsonify({'unread': n})


@app.route('/admin/patch/new', methods=['GET', 'POST'])
@login_required
def admin_patch_new():
    """Admin: neuen Patch-Note anlegen + optional Push an alle User."""
    if not current_user.has_admin_access:
        flash('Nur Admin', 'error')
        return redirect(url_for('whats_new'))
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()[:200]
        summary = (request.form.get('summary') or '').strip()[:500]
        body_md = (request.form.get('body_md') or '').strip()
        version = (request.form.get('version') or '').strip()[:30]
        kategorie = (request.form.get('kategorie') or 'feature').strip()[:30]
        push_all = request.form.get('push_all') == '1'
        if not title:
            flash('Titel fehlt', 'error')
            return redirect(url_for('admin_patch_new'))
        db = get_db()
        cur = db.execute('''INSERT INTO patch_notes (version, title, summary, body_md, kategorie, pushed)
                            VALUES (?, ?, ?, ?, ?, ?)''',
                        (version or None, title, summary or None, body_md or None, kategorie, 1 if push_all else 0))
        patch_id = cur.lastrowid
        db.commit()
        push_count = 0
        if push_all:
            users = db.execute('''SELECT DISTINCT u.id FROM users u
                                  JOIN push_subscriptions ps ON ps.user_id=u.id
                                  WHERE u.active=1''').fetchall()
            push_title = f'Update: {title[:60]}'
            push_body = (summary or 'Neues Feature in der App — jetzt anschauen.')[:140]
            for u in users:
                try:
                    if send_push_to_user(u['id'], title=push_title, body=push_body,
                                         url='/whats-new', push_type='patch_note',
                                         tag=f'patch-{patch_id}'):
                        push_count += 1
                except Exception:
                    pass
        db.close()
        cache_invalidate('ctx:patch_unread:')  # Badge bei allen Usern resetten
        flash(f'Patch-Note „{title}" angelegt' + (f' · Push an {push_count} User' if push_all else ''), 'success')
        return redirect(url_for('whats_new'))
    return render_template('admin_patch_new.html')


@app.route('/daily-checkin', methods=['GET', 'POST'])
@login_required
def daily_checkin():
    """Täglicher Check-in: User trägt Anrufe + Termine ein.
    Wird vom täglichen Push-Reminder verlinkt. Streak + Vorwoche-Vergleich."""
    today_iso = date.today().isoformat()
    db = get_db()

    if request.method == 'POST':
        try:
            anrufe = int(request.form.get('anrufe', 0) or 0)
            termine = int(request.form.get('termine', 0) or 0)
        except (ValueError, TypeError):
            anrufe, termine = 0, 0
        notiz = (request.form.get('notiz') or '').strip()[:500]
        # Upsert (UNIQUE auf user_id, datum)
        db.execute('''INSERT INTO daily_checkins (user_id, datum, anrufe, termine, notiz)
                      VALUES (?, ?, ?, ?, ?)
                      ON CONFLICT(user_id, datum) DO UPDATE SET
                        anrufe=excluded.anrufe, termine=excluded.termine, notiz=excluded.notiz,
                        created_at=CURRENT_TIMESTAMP''',
                  (current_user.id, today_iso, anrufe, termine, notiz))
        db.commit()
        flash(f'Check-in gespeichert: {anrufe} Anrufe, {termine} Termine.', 'success')
        log_activity(current_user.id, 'daily_checkin',
                     f'Check-in: {anrufe} Anrufe · {termine} Termine',
                     icon='✓', color='gold')

    # Heute: bereits eingetragen?
    today_row = db.execute('SELECT * FROM daily_checkins WHERE user_id=? AND datum=?',
                          (current_user.id, today_iso)).fetchone()
    # Streak: aufeinanderfolgende Tage mit Check-in (rückwärts ab heute)
    last30 = db.execute('''SELECT datum, anrufe, termine FROM daily_checkins
                           WHERE user_id=? AND datum >= date('now', '-30 days')
                           ORDER BY datum DESC''', (current_user.id,)).fetchall()
    streak = 0
    cur_date = date.today()
    dates_set = {r['datum'] for r in last30}
    while cur_date.isoformat() in dates_set:
        streak += 1
        cur_date -= timedelta(days=1)
    # Wochen-Vergleich (letzte 7 Tage vs Vorwoche)
    sum_anrufe_7 = sum(r['anrufe'] or 0 for r in last30 if r['datum'] >= (date.today() - timedelta(days=6)).isoformat())
    sum_termine_7 = sum(r['termine'] or 0 for r in last30 if r['datum'] >= (date.today() - timedelta(days=6)).isoformat())
    sum_anrufe_prev = sum(r['anrufe'] or 0 for r in last30 if (date.today() - timedelta(days=13)).isoformat() <= r['datum'] <= (date.today() - timedelta(days=7)).isoformat())
    sum_termine_prev = sum(r['termine'] or 0 for r in last30 if (date.today() - timedelta(days=13)).isoformat() <= r['datum'] <= (date.today() - timedelta(days=7)).isoformat())
    db.close()
    return render_template('daily_checkin.html',
        today=today_row, streak=streak, last30=last30[:14],
        sum_anrufe_7=sum_anrufe_7, sum_termine_7=sum_termine_7,
        sum_anrufe_prev=sum_anrufe_prev, sum_termine_prev=sum_termine_prev,
        today_iso=today_iso)


@app.route('/newsletter')
@login_required
def newsletter():
    """Branchen-News-Feed + auto-mark als gelesen (Sidebar-Badge geht weg)."""
    db = get_db()
    kategorie = request.args.get('kategorie') or 'all'
    if kategorie == 'all':
        items = db.execute('''SELECT * FROM newsletter_items
                              ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 100''').fetchall()
    else:
        items = db.execute('''SELECT * FROM newsletter_items WHERE kategorie=?
                              ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 100''',
                          (kategorie,)).fetchall()
    cats = db.execute('SELECT kategorie, COUNT(*) c FROM newsletter_items GROUP BY kategorie').fetchall()
    cat_counts = {r['kategorie']: r['c'] for r in cats}
    cat_counts['all'] = sum(cat_counts.values())
    # Auto-mark als gelesen — User hat den Feed gerade geöffnet
    db.execute('''INSERT INTO newsletter_last_seen (user_id, last_seen_at)
                  VALUES (?, CURRENT_TIMESTAMP)
                  ON CONFLICT(user_id) DO UPDATE SET last_seen_at=CURRENT_TIMESTAMP''',
              (current_user.id,))
    db.commit()
    db.close()
    cache_invalidate(f'ctx:newsletter_unread:{current_user.id}')
    return render_template('newsletter.html', items=items, kategorie=kategorie, cat_counts=cat_counts)


@app.route('/admin/newsletter/refresh', methods=['POST'])
@login_required
def admin_newsletter_refresh():
    """Triggert den Newsletter-Agent — fetcht aktuelle News von den Quellen."""
    if not current_user.has_admin_access:
        flash('Nur Admin', 'error')
        return redirect(url_for('newsletter'))
    import subprocess
    try:
        result = subprocess.run(['python3', 'scripts/newsletter_agent.py'],
                              capture_output=True, text=True, timeout=60,
                              cwd=os.path.dirname(os.path.abspath(__file__)))
        out = (result.stdout or '') + (result.stderr or '')
        # Letzte Zeile mit "neu" extrahieren
        flash(f'Newsletter-Refresh fertig. Output: {out[-200:]}', 'success' if result.returncode == 0 else 'error')
    except Exception as e:
        flash(f'Refresh-Fehler: {e}', 'error')
    return redirect(url_for('newsletter'))


@app.route('/grundseminar')
@login_required
def grundseminar():
    """Grundseminar-Teilnehmerliste — RK-Leads die als kommend markiert sind.
    Admin/HREP+ können per ?scope=<id|me|all> filtern."""
    deadlines = get_production_deadlines()
    scope = (request.args.get('scope') or 'me').lower()
    db = get_db()
    directs = []
    if current_user.has_admin_access or get_all_descendants(current_user.id):
        directs = db.execute('SELECT id, name FROM users WHERE parent_id=? AND active=1 ORDER BY name', (current_user.id,)).fetchall()
    if scope == 'all':
        owner_ids = [current_user.id] + get_all_descendants(current_user.id)
        scope_label = '🌐 Gesamte Struktur'
    elif scope.isdigit():
        sid = int(scope)
        # Berechtigung: Admin oder Sub muss in eigener Downline liegen
        if current_user.has_admin_access or sid in get_all_descendants(current_user.id):
            owner_ids = [sid] + get_all_descendants(sid)
            sname = db.execute('SELECT name FROM users WHERE id=?', (sid,)).fetchone()
            scope_label = f'⬢ {sname["name"] if sname else "?"}'
        else:
            owner_ids = [current_user.id]
            scope_label = f'👤 {current_user.name}'
            scope = 'me'
    else:
        owner_ids = [current_user.id]
        scope_label = f'👤 {current_user.name}'
        scope = 'me'
    ph = ','.join('?' * len(owner_ids))
    teilnehmer = db.execute(f'''
        SELECT l.id, l.name, l.email, l.phone, l.status, l.notizen, l.created_at, l.updated_at,
               u.name as owner_name
        FROM leads l LEFT JOIN users u ON l.owner_id = u.id
        WHERE l.owner_id IN ({ph})
          AND COALESCE(l.liste_typ,'vk') = 'rk'
          AND l.status IN ('gewonnen', 'angemeldet')
        ORDER BY COALESCE(l.updated_at, l.created_at) DESC LIMIT 200
    ''', owner_ids).fetchall()
    pending = db.execute(f'''
        SELECT l.id, l.name, l.status, u.name as owner_name
        FROM leads l LEFT JOIN users u ON l.owner_id = u.id
        WHERE l.owner_id IN ({ph})
          AND COALESCE(l.liste_typ,'vk') = 'rk'
          AND COALESCE(l.status,'') NOT IN ('gewonnen','angemeldet','verloren','storno','tot','')
        ORDER BY l.created_at DESC LIMIT 50
    ''', owner_ids).fetchall()
    db.close()
    return render_template('grundseminar.html',
        deadlines=deadlines, teilnehmer=teilnehmer, pending=pending,
        directs=directs, scope=scope, scope_label=scope_label)


@app.route('/meine-leads')
@login_required
def meine_leads_inbox():
    """Owner-Inbox: zeigt dem User die Leads die ÜBER SEINEN Lead-Token-Link
    reingekommen sind. Filterbar VK/RK. Ersetzt für non-Admin die /admin/inbox."""
    typ = (request.args.get('typ') or 'all').lower()
    if typ not in ('vk', 'rk', 'all'):
        typ = 'all'
    db = get_db()
    base = '''SELECT l.* FROM leads l
              WHERE l.source = 'public' AND l.owner_id = ?'''
    args = [current_user.id]
    if typ == 'vk':
        rows = db.execute(base + " AND COALESCE(l.liste_typ,'vk') = 'vk' ORDER BY l.created_at DESC LIMIT 200", args).fetchall()
    elif typ == 'rk':
        rows = db.execute(base + " AND COALESCE(l.liste_typ,'vk') = 'rk' ORDER BY l.created_at DESC LIMIT 200", args).fetchall()
    else:
        rows = db.execute(base + " ORDER BY l.created_at DESC LIMIT 200", args).fetchall()
    cnt_vk = db.execute("SELECT COUNT(*) c FROM leads WHERE source='public' AND owner_id=? AND COALESCE(liste_typ,'vk')='vk'", (current_user.id,)).fetchone()['c']
    cnt_rk = db.execute("SELECT COUNT(*) c FROM leads WHERE source='public' AND owner_id=? AND COALESCE(liste_typ,'vk')='rk'", (current_user.id,)).fetchone()['c']
    db.close()
    return render_template('meine_leads_inbox.html', leads=rows, typ=typ, cnt_vk=cnt_vk, cnt_rk=cnt_rk, cnt_all=cnt_vk + cnt_rk)


@app.route('/admin/inbox')
@login_required
def admin_inbox():
    """Bewerber/Customer-Lead-Inbox — filterbar nach typ=vk|rk|all."""
    if not current_user.has_admin_access:
        flash('Keine Berechtigung', 'error')
        return redirect(url_for('dashboard'))
    typ = (request.args.get('typ') or 'all').lower()
    if typ not in ('vk', 'rk', 'all'):
        typ = 'all'
    db = get_db()
    base_sql = '''
        SELECT l.*, u.name as owner_name, u.instagram_handle as owner_ig, u.tiktok_handle as owner_tt
        FROM leads l LEFT JOIN users u ON l.owner_id = u.id
        WHERE l.source = 'public'
    '''
    if typ == 'vk':
        rows = db.execute(base_sql + " AND COALESCE(l.liste_typ,'vk') = 'vk' ORDER BY l.created_at DESC LIMIT 200").fetchall()
    elif typ == 'rk':
        rows = db.execute(base_sql + " AND COALESCE(l.liste_typ,'vk') = 'rk' ORDER BY l.created_at DESC LIMIT 200").fetchall()
    else:
        rows = db.execute(base_sql + " ORDER BY l.created_at DESC LIMIT 200").fetchall()
    cnt_vk = db.execute("SELECT COUNT(*) c FROM leads WHERE source='public' AND COALESCE(liste_typ,'vk')='vk'").fetchone()['c']
    cnt_rk = db.execute("SELECT COUNT(*) c FROM leads WHERE source='public' AND COALESCE(liste_typ,'vk')='rk'").fetchone()['c']
    db.close()
    return render_template('admin_inbox.html', leads=rows, typ=typ, cnt_vk=cnt_vk, cnt_rk=cnt_rk, cnt_all=cnt_vk + cnt_rk)


# In-Memory Rate-Limit-Tracker für Reset-Endpoint
_RESET_RL = {}  # {ip_or_email: [timestamp, timestamp, ...]}
_RESET_RL_WINDOW = 300   # 5 Minuten
_RESET_RL_MAX = 3        # max 3 requests pro window

def _reset_rate_limit_ok(key):
    """Returns True if request erlaubt, False bei rate-limit-hit. Cleanup-Side-Effekt."""
    import time as _t
    now = _t.time()
    window_start = now - _RESET_RL_WINDOW
    timestamps = [t for t in _RESET_RL.get(key, []) if t > window_start]
    if len(timestamps) >= _RESET_RL_MAX:
        _RESET_RL[key] = timestamps
        return False
    timestamps.append(now)
    _RESET_RL[key] = timestamps
    # Garbage-Collect: alte Einträge löschen
    if len(_RESET_RL) > 2000:
        for k in list(_RESET_RL.keys()):
            if not any(t > window_start for t in _RESET_RL[k]):
                _RESET_RL.pop(k, None)
    return True


@app.route('/passwort-vergessen', methods=['GET', 'POST'])
def passwort_vergessen():
    """Self-Service Password-Reset: User trägt E-Mail (oder Telefon) ein,
    bekommt Token-Link per Mail. Bei SMS (kein Provider): Code per E-Mail.
    Admin bekommt jede Reset-Anfrage als BCC zur Backup.
    Rate-Limit: max 3 Requests/5min pro IP+Identifier."""
    if request.method == 'POST':
        # Rate-Limit Check (vor Anti-Enum, damit wir nicht zur Enumeration einladen)
        ip = request.remote_addr or 'unknown'
        identifier = (request.form.get('email') or request.form.get('phone') or '').strip().lower()
        rl_key = f'{ip}|{identifier}'
        if not _reset_rate_limit_ok(rl_key):
            flash('Zu viele Reset-Versuche. Bitte 5 Minuten warten und nochmal versuchen.', 'error')
            return render_template('passwort_vergessen.html', sent=False, method=request.form.get('method', 'email'),
                                   send_status=None)
        email = (request.form.get('email') or '').strip().lower()
        phone = (request.form.get('phone') or '').strip()
        method = (request.form.get('method') or 'email').lower()
        if method not in ('email', 'sms'):
            method = 'email'
        send_status = 'pending'
        send_error = None
        target_label = ''
        admin_email = get_setting('smtp_from_email') or 'najib@ntpro.de'

        if method == 'email' and email:
            db = get_db()
            row = db.execute('SELECT id, name, email FROM users WHERE LOWER(email)=? AND active=1', (email,)).fetchone()
            if row:
                token = secrets.token_urlsafe(32)
                expires = (datetime.now() + timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
                db.execute('INSERT INTO password_resets (user_id, token, method, expires_at, ip) VALUES (?,?,?,?,?)',
                           (row['id'], token, 'email', expires, request.remote_addr or ''))
                db.commit()
                db.close()
                base = (request.url_root or '').rstrip('/')
                reset_url = f"{base}/passwort-zuruecksetzen/{token}"
                text = (f"Hallo {row['name']},\n\n"
                        f"Du hast einen Passwort-Reset für Pro Academy angefordert.\n"
                        f"Klick zum Zurücksetzen (Link gültig 2 Stunden):\n\n{reset_url}\n\n"
                        f"Wenn du das nicht warst, ignoriere diese Mail einfach.\n\nProAcademy")
                html = (f'<p>Hallo {row["name"]},</p>'
                        f'<p>Du hast einen Passwort-Reset für <strong>Pro Academy</strong> angefordert.</p>'
                        f'<p style="margin:24px 0"><a href="{reset_url}" style="background:#d4a843;color:#0f1c3f;'
                        f'padding:14px 26px;border-radius:10px;text-decoration:none;font-weight:800;display:inline-block">'
                        f'→ Passwort jetzt zurücksetzen</a></p>'
                        f'<p style="color:#64748b;font-size:13px">Link gilt 2 Stunden. Wenn der Button nicht klickbar ist, '
                        f'kopier diese URL in den Browser:<br><code style="background:#f3f4f6;padding:4px 8px;border-radius:4px">{reset_url}</code></p>'
                        f'<p style="color:#64748b;font-size:12px;margin-top:30px">War das nicht du? Einfach ignorieren — niemand kann ohne den Link dein Passwort ändern.</p>')
                ok, err = send_email(row['email'], 'Passwort zurücksetzen — Pro Academy', text,
                                     body_html=html, sent_by=None,
                                     reply_to=admin_email, category='password_reset',
                                     bcc=admin_email if row['email'].lower() != admin_email.lower() else None)
                send_status = 'ok' if ok else 'fail'
                send_error = err
                target_label = row['email']
            else:
                db.close()
                # Anti-Enumeration: tu so als ob alles OK
                send_status = 'ok'
                target_label = email
        elif method == 'sms' and phone:
            # SMS-Pfad: Code per E-Mail an User schicken (kein SMS-Provider konfiguriert).
            db = get_db()
            phone_norm = ''.join(c for c in phone if c.isdigit() or c == '+')
            rows = db.execute('SELECT id, name, phone, email FROM users WHERE phone IS NOT NULL AND active=1').fetchall()
            match = next((r for r in rows if r['phone'] and ''.join(c for c in r['phone'] if c.isdigit() or c == '+') == phone_norm), None)
            if match:
                code = ''.join(secrets.choice('0123456789') for _ in range(6))
                token = secrets.token_urlsafe(32)
                expires = (datetime.now() + timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
                db.execute('INSERT INTO password_resets (user_id, token, method, sms_code, expires_at, ip) VALUES (?,?,?,?,?,?)',
                           (match['id'], token, 'sms', code, expires, request.remote_addr or ''))
                db.commit()
                db.close()
                base = (request.url_root or '').rstrip('/')
                entry_url = f"{base}/passwort-zuruecksetzen-sms?token={token}"
                if match['email']:
                    text = (f"Hallo {match['name']},\n\n"
                            f"Dein Code zum Passwort-Reset (gültig 15 Minuten):\n\n"
                            f"   {code}\n\n"
                            f"Trag den Code hier ein:\n{entry_url}\n\n"
                            f"⚠ SMS-Versand ist aktuell deaktiviert — wir schicken dir den Code per E-Mail.\n"
                            f"Wenn du das nicht warst, ignoriere diese Mail.\n\nProAcademy")
                    html = (f'<p>Hallo {match["name"]},</p>'
                            f'<p>Dein <strong>Code zum Passwort-Reset</strong> (gültig 15 Minuten):</p>'
                            f'<div style="font-size:32px;font-weight:800;letter-spacing:8px;background:#f3f4f6;padding:18px 30px;'
                            f'border-radius:12px;text-align:center;color:#0f1c3f;margin:18px 0">{code}</div>'
                            f'<p><a href="{entry_url}" style="background:#d4a843;color:#0f1c3f;padding:12px 22px;'
                            f'border-radius:8px;text-decoration:none;font-weight:800;display:inline-block">→ Code eingeben</a></p>'
                            f'<p style="color:#64748b;font-size:12px;margin-top:24px">⚠ SMS-Versand ist aktuell deaktiviert — '
                            f'du bekommst den Code per E-Mail. War das nicht du? Einfach ignorieren.</p>')
                    ok, err = send_email(match['email'], f'Reset-Code: {code} — Pro Academy', text,
                                         body_html=html, sent_by=None,
                                         reply_to=admin_email, category='password_reset',
                                         bcc=admin_email if match['email'].lower() != admin_email.lower() else None)
                    send_status = 'ok' if ok else 'fail'
                    send_error = err
                    target_label = f'{match["email"]} (für Telefon-Reset)'
                else:
                    # Phone-only User: log + admin-Mail mit Code
                    print(f'[reset-sms-no-email] User {match["name"]} ({phone_norm}) — Code: {code}, URL: {entry_url}')
                    if admin_email:
                        send_email(admin_email,
                                   f'SMS-Reset für {match["name"]}',
                                   f'User {match["name"]} ({phone_norm}) hat SMS-Reset gemacht aber hat keine E-Mail.\nCode: {code}\nURL: {entry_url}',
                                   sent_by=None, category='password_reset')
                    send_status = 'phone_only'
                    target_label = phone_norm
            else:
                db.close()
                send_status = 'ok'  # anti-enum
                target_label = phone
        return render_template('passwort_vergessen.html', sent=True, method=method,
                               send_status=send_status, send_error=send_error,
                               target_label=target_label)
    return render_template('passwort_vergessen.html', sent=False, method=request.args.get('method','email'),
                           send_status=None)


@app.route('/passwort-zuruecksetzen/<token>', methods=['GET', 'POST'])
def passwort_zuruecksetzen(token):
    """Reset-Page mit Token aus E-Mail-Link."""
    db = get_db()
    row = db.execute('''SELECT pr.*, u.name as user_name, u.email as user_email
                        FROM password_resets pr JOIN users u ON pr.user_id = u.id
                        WHERE pr.token=? AND pr.used_at IS NULL''', (token,)).fetchone()
    if not row:
        db.close()
        flash('Reset-Link ungültig oder bereits benutzt.', 'error')
        return redirect(url_for('login'))
    # Expiration check
    try:
        exp = datetime.strptime(row['expires_at'], '%Y-%m-%d %H:%M:%S')
    except Exception:
        exp = datetime.now() - timedelta(seconds=1)
    if datetime.now() > exp:
        db.close()
        flash('Reset-Link abgelaufen — fordere einen neuen an.', 'error')
        return redirect(url_for('passwort_vergessen'))
    if request.method == 'POST':
        new_pw = (request.form.get('password') or '').strip()
        confirm = (request.form.get('confirm') or '').strip()
        if len(new_pw) < 6:
            flash('Passwort muss mindestens 6 Zeichen haben', 'error')
            db.close()
            return render_template('passwort_zuruecksetzen.html', token=token, user_name=row['user_name'])
        if new_pw != confirm:
            flash('Passwörter stimmen nicht überein', 'error')
            db.close()
            return render_template('passwort_zuruecksetzen.html', token=token, user_name=row['user_name'])
        db.execute('UPDATE users SET password = ?, must_change_password = 0 WHERE id = ?',
                   (hash_password(new_pw), row['user_id']))
        db.execute('UPDATE password_resets SET used_at = CURRENT_TIMESTAMP WHERE id = ?', (row['id'],))
        db.commit()
        db.close()
        flash('✅ Passwort erfolgreich zurückgesetzt — du kannst dich jetzt einloggen.', 'success')
        return redirect(url_for('login'))
    db.close()
    return render_template('passwort_zuruecksetzen.html', token=token, user_name=row['user_name'])


@app.route('/passwort-zuruecksetzen-sms', methods=['GET', 'POST'])
def passwort_zuruecksetzen_sms():
    """SMS-Pfad: 6-stelliger Code (aus SMS) + Token (aus URL).
    Brute-Force-Schutz: nach 5 falschen Versuchen wird der Token invalidiert."""
    SMS_MAX_ATTEMPTS = 5
    token = request.args.get('token') or request.form.get('token') or ''
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        new_pw = (request.form.get('password') or '').strip()
        confirm = (request.form.get('confirm') or '').strip()
        db = get_db()
        row = db.execute('SELECT * FROM password_resets WHERE token=? AND method=? AND used_at IS NULL',
                         (token, 'sms')).fetchone()
        if not row:
            db.close()
            flash('Code falsch oder abgelaufen.', 'error')
            return render_template('passwort_zuruecksetzen_sms.html', token=token)
        # Brute-Force-Schutz: max 5 Versuche bevor Token invalidiert wird
        attempts = row['sms_attempts'] or 0
        if attempts >= SMS_MAX_ATTEMPTS:
            db.execute('UPDATE password_resets SET used_at = CURRENT_TIMESTAMP WHERE id = ?', (row['id'],))
            db.commit(); db.close()
            flash(f'Zu viele Fehlversuche — Token gesperrt. Fordere einen neuen Code an.', 'error')
            return redirect(url_for('passwort_vergessen'))
        if row['sms_code'] != code:
            db.execute('UPDATE password_resets SET sms_attempts = sms_attempts + 1 WHERE id = ?', (row['id'],))
            db.commit()
            remaining = SMS_MAX_ATTEMPTS - (attempts + 1)
            db.close()
            flash(f'Code falsch. Noch {remaining} Versuch{"e" if remaining != 1 else ""} bevor der Token gesperrt wird.', 'error')
            return render_template('passwort_zuruecksetzen_sms.html', token=token)
        try:
            exp = datetime.strptime(row['expires_at'], '%Y-%m-%d %H:%M:%S')
        except Exception:
            exp = datetime.now() - timedelta(seconds=1)
        if datetime.now() > exp:
            db.close()
            flash('Code abgelaufen — fordere einen neuen an.', 'error')
            return redirect(url_for('passwort_vergessen'))
        if len(new_pw) < 6 or new_pw != confirm:
            db.close()
            flash('Passwort muss min. 6 Zeichen haben und übereinstimmen.', 'error')
            return render_template('passwort_zuruecksetzen_sms.html', token=token)
        db.execute('UPDATE users SET password = ?, must_change_password = 0 WHERE id = ?',
                   (hash_password(new_pw), row['user_id']))
        db.execute('UPDATE password_resets SET used_at = CURRENT_TIMESTAMP WHERE id = ?', (row['id'],))
        db.commit()
        db.close()
        flash('✅ Passwort erfolgreich zurückgesetzt.', 'success')
        return redirect(url_for('login'))
    return render_template('passwort_zuruecksetzen_sms.html', token=token)


@app.route('/passwort-aendern', methods=['GET', 'POST'])
@login_required
def passwort_aendern():
    """Pflicht-Passwort-Änderung beim ersten Login."""
    if request.method == 'POST':
        new_pw = (request.form.get('password') or '').strip()
        confirm = (request.form.get('confirm') or '').strip()
        if len(new_pw) < 6:
            flash('Passwort muss mindestens 6 Zeichen haben', 'error')
            return render_template('passwort_aendern.html')
        if new_pw != confirm:
            flash('Passwörter stimmen nicht überein', 'error')
            return render_template('passwort_aendern.html')
        db = get_db()
        db.execute('UPDATE users SET password = ?, must_change_password = 0 WHERE id = ?',
                   (hash_password(new_pw), current_user.id))
        db.commit()
        db.close()
        session.pop('must_change_password', None)
        flash('✅ Passwort erfolgreich geändert!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('passwort_aendern.html')


@app.route('/assistent')
@login_required
def assistent():
    """KI-Chat-Assistent Coach."""
    db = get_db()
    msgs = db.execute(
        'SELECT role, content, created_at FROM chat_messages WHERE user_id=? ORDER BY id ASC LIMIT 50',
        (current_user.id,)
    ).fetchall()
    db.close()
    return render_template('assistent.html', messages=msgs, ai_configured=is_ai_configured())


@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    """Sendet Nachricht an Assistent + bekommt Antwort als JSON."""
    msg = (request.get_json(silent=True) or {}).get('message', '').strip() if request.is_json else (request.form.get('message') or '').strip()
    if not msg:
        return jsonify({'error': 'Keine Nachricht'}), 400
    text, err = chat_with_assistant(current_user.id, msg)
    if err:
        return jsonify({'error': err}), 500
    return jsonify({'response': text})


@app.route('/api/chat/clear', methods=['POST'])
@login_required
def api_chat_clear():
    db = get_db()
    db.execute('DELETE FROM chat_messages WHERE user_id=?', (current_user.id,))
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/willkommen')
@login_required
def willkommen():
    """Onboarding-Wizard für neue Partner mit KI-Stimme."""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (current_user.id,)).fetchone()
    db.close()
    return render_template('willkommen.html', user=user, my_career=get_career_level_for_user(current_user.id))


@app.route('/willkommen/abschluss', methods=['POST'])
@login_required
def willkommen_abschluss():
    """Markiert Onboarding als abgeschlossen + speichert Vision falls eingegeben."""
    vision = (request.form.get('vision') or '').strip()
    start_tour = request.form.get('start_tour') == '1'
    db = get_db()
    if vision:
        db.execute('UPDATE users SET vision = ?, onboarding_done = 1 WHERE id = ?', (vision, current_user.id))
    else:
        db.execute('UPDATE users SET onboarding_done = 1 WHERE id = ?', (current_user.id,))
    db.commit()
    db.close()
    flash('Willkommen im Team! Los geht\'s 🚀', 'success')
    # Start-Tour-Flag mitgeben
    if start_tour:
        return redirect(url_for('dashboard') + '?tour=start')
    return redirect(url_for('dashboard'))


@app.route('/willkommen/skip', methods=['POST'])
@login_required
def willkommen_skip():
    """Onboarding überspringen (kann später unter /willkommen wieder aufgerufen werden)."""
    db = get_db()
    db.execute('UPDATE users SET onboarding_done = 1 WHERE id = ?', (current_user.id,))
    db.commit()
    db.close()
    return redirect(url_for('dashboard'))


@app.route('/einstellungen', methods=['GET', 'POST'])
@login_required
def einstellungen():
    """Account-Einstellungen für jeden User."""
    db = get_db()
    if request.method == 'POST':
        # Aktuell speichern wir hauptsächlich Vision/Telefon im Profil,
        # Theme bleibt clientseitig (LocalStorage). Hier können später
        # weitere Server-Side Settings dazu (E-Mail-Benachrichtigungen, etc.)
        flash('Einstellungen gespeichert!', 'success')
        return redirect(url_for('einstellungen'))
    user = db.execute('SELECT * FROM users WHERE id = ?', (current_user.id,)).fetchone()
    db.close()
    return render_template('einstellungen.html', user=user)


@app.route('/api/vision-seen', methods=['POST'])
@login_required
def vision_seen():
    session.pop('show_vision', None)
    return jsonify({'ok': True})


@app.route('/api/achievements/unseen')
@login_required
def api_unseen_achievements():
    """Liefert neu freigeschaltete Achievements (für Modal)."""
    items = get_unseen_achievements(current_user.id)
    for it in items:
        it['tier_color'] = ACHIEVEMENT_TIER_COLORS.get(it.get('tier', 'silver'), '#94a3b8')
    return jsonify({'unseen': items})


@app.route('/api/achievements/seen', methods=['POST'])
@login_required
def api_mark_achievements_seen():
    mark_achievements_seen(current_user.id)
    return jsonify({'ok': True})


def get_ki_recommendations(user_id, scope_user_id=None):
    """KI-Empfehlungen für heute & diese Woche - die wichtigsten Aktionen.
    scope_user_id=None = ganzer Vertrieb."""
    db = get_db()

    if scope_user_id:
        ids = [scope_user_id] + get_all_descendants(scope_user_id)
    else:
        ids = [r['id'] for r in db.execute('SELECT id FROM users WHERE active = 1').fetchall()]

    if not ids:
        db.close()
        return []
    ph = ','.join('?' * len(ids))

    recs = []
    today = date.today()

    # 1) Geburtstage HEUTE
    bds = db.execute(f'''
        SELECT id, name, phone, birthday FROM users
        WHERE id IN ({ph}) AND birthday IS NOT NULL AND active = 1
        AND substr(birthday, 6, 5) = ?
    ''', ids + [today.strftime('%m-%d')]).fetchall()
    bd_kunden = db.execute(f'''
        SELECT id, name, phone, birthday FROM leads
        WHERE owner_id IN ({ph}) AND birthday IS NOT NULL
        AND substr(birthday, 6, 5) = ?
    ''', ids + [today.strftime('%m-%d')]).fetchall()
    for b in list(bds) + list(bd_kunden):
        recs.append({
            'priority': 'critical', 'icon': '🎂', 'color': 'orange',
            'title': f'Heute Geburtstag: {b["name"]}',
            'detail': f'Anrufen für persönlichen Gruß — perfekter Touchpoint',
            'action_label': '📞 Anrufen', 'action_url': f'tel:{b["phone"]}' if b['phone'] else None,
            'category': 'birthday'
        })

    # 2) Inaktive > 14 Tage (Schweige-Risiko)
    silent = db.execute(f'''
        SELECT u.id, u.name, u.phone, u.last_login, u.joined_date,
               julianday('now') - julianday(COALESCE(u.last_login, u.joined_date)) as silence_days
        FROM users u
        WHERE u.id IN ({ph}) AND u.active = 1
        AND julianday('now') - julianday(COALESCE(u.last_login, u.joined_date)) > 14
        ORDER BY silence_days DESC LIMIT 3
    ''', ids).fetchall()
    for s in silent:
        recs.append({
            'priority': 'high', 'icon': '🔇', 'color': 'red',
            'title': f'{s["name"]} schweigt seit {int(s["silence_days"])} Tagen',
            'detail': 'Kontaktieren bevor er ganz weg ist',
            'action_label': '📞 Anrufen', 'action_url': f'tel:{s["phone"]}' if s['phone'] else None,
            'category': 'silence'
        })

    # 3) Kurz vor Beförderung (>80%)
    eh_rows = db.execute(f'''
        SELECT u.id, u.name, u.phone, u.manual_career_level,
               COALESCE(SUM(c.einheiten), 0) as eh
        FROM users u
        LEFT JOIN contracts c ON c.owner_id = u.id AND c.status="abgeschlossen" AND c.recherche_status="freigegeben"
        WHERE u.id IN ({ph}) AND u.active = 1
        GROUP BY u.id
    ''', ids).fetchall()
    for r in eh_rows:
        career = next((cl for cl in CAREER_LEVELS if cl['level'] == (r['manual_career_level'] or 1)), CAREER_LEVELS[0])
        next_lvl = next((cl for cl in CAREER_LEVELS if cl['level'] == career['level'] + 1), None)
        if not next_lvl: continue
        progress = (r['eh'] / next_lvl['min_eh'] * 100) if next_lvl['min_eh'] > 0 else 0
        if 80 <= progress < 100:
            recs.append({
                'priority': 'high', 'icon': '🚀', 'color': 'gold',
                'title': f'{r["name"]} kurz vor {next_lvl["short"]}',
                'detail': f'{int(progress)}% erreicht — nur noch {int(next_lvl["min_eh"] - r["eh"])} EH',
                'action_label': '🎯 Coaching', 'action_url': f'/coaching/{r["id"]}',
                'category': 'promotion'
            })

    # 4) Hängende Recherchen
    pending = db.execute(f'''
        SELECT c.id, c.client_name, c.einheiten, u.name as berater, u.phone as berater_phone, u.id as berater_id,
               julianday('now') - julianday(c.created_at) as tage
        FROM contracts c JOIN users u ON c.owner_id = u.id
        WHERE c.recherche_status IN ('ausstehend','') AND c.einheiten > 0
        AND c.owner_id IN ({ph})
        AND julianday('now') - julianday(c.created_at) > 7
        ORDER BY tage DESC LIMIT 3
    ''', ids).fetchall()
    for p in pending:
        recs.append({
            'priority': 'medium', 'icon': '⏳', 'color': 'orange',
            'title': f'Recherche hängt: {p["client_name"]} ({int(p["einheiten"])} EH)',
            'detail': f'Seit {int(p["tage"])} Tagen offen — bei {p["berater"]} nachfragen',
            'action_label': '✏️ Vertrag', 'action_url': f'/vertraege/{p["id"]}/edit',
            'category': 'research'
        })

    # 5) Ohne Vision (Profil-Pflege)
    no_vision = db.execute(f'''
        SELECT id, name FROM users
        WHERE id IN ({ph}) AND active = 1 AND (vision IS NULL OR vision = '')
        AND login_count > 0 LIMIT 3
    ''', ids).fetchall()
    for n in no_vision:
        recs.append({
            'priority': 'low', 'icon': '★', 'color': 'purple',
            'title': f'{n["name"]} hat noch keine Vision',
            'detail': 'Erinnern: persönliches Warum motiviert dauerhaft',
            'action_label': '🎯 Coaching', 'action_url': f'/coaching/{n["id"]}',
            'category': 'vision'
        })

    # 6) Onboarding nicht abgeschlossen + > 14 Tage dabei
    ob_stuck = db.execute(f'''
        SELECT id, name, phone,
               (onboarding_endgespraech + onboarding_einarbeitung_1 + onboarding_einarbeitung_2 +
                onboarding_einarbeitung_3 + onboarding_seminar_bezahlt) as ob_score,
               julianday('now') - julianday(joined_date) as tage_dabei
        FROM users
        WHERE id IN ({ph}) AND active = 1
        AND julianday('now') - julianday(joined_date) > 14
        AND (onboarding_endgespraech + onboarding_einarbeitung_1 + onboarding_einarbeitung_2 +
             onboarding_einarbeitung_3 + onboarding_seminar_bezahlt) < 3
        LIMIT 3
    ''', ids).fetchall()
    for o in ob_stuck:
        recs.append({
            'priority': 'medium', 'icon': '🎓', 'color': 'blue',
            'title': f'Onboarding hängt: {o["name"]}',
            'detail': f'Erst {o["ob_score"]}/5 Schritte · {int(o["tage_dabei"])} Tage dabei',
            'action_label': '🎯 Coaching', 'action_url': f'/coaching/{o["id"]}',
            'category': 'onboarding'
        })

    # Sortieren: critical > high > medium > low
    prio_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
    recs.sort(key=lambda r: prio_order.get(r['priority'], 9))

    db.close()
    return recs[:10]


def get_konversations_starter(user_id):
    """KI-generierte Konversations-Starter für Coaching-Calls — basierend auf User-Daten."""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        db.close()
        return []

    own_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (user_id,)).fetchone()['s']
    contracts = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=?', (user_id,)).fetchone()['c']
    recent_contracts = db.execute('SELECT client_name, einheiten, status FROM contracts WHERE owner_id=? ORDER BY created_at DESC LIMIT 3', (user_id,)).fetchall()
    pending = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND recherche_status IN ("ausstehend","")', (user_id,)).fetchone()['c']
    appts_done = db.execute('SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND status="erledigt"', (user_id,)).fetchone()['c']

    starters = []

    # Positiv beginnen
    if recent_contracts:
        last = recent_contracts[0]
        starters.append({
            'icon': '🎉', 'category': 'Positiv-Start',
            'text': f'Gratuliere zum Vertrag mit {last["client_name"]} ({int(last["einheiten"])} EH)! Wie lief das Gespräch?'
        })
    if appts_done >= 5:
        starters.append({
            'icon': '👏', 'category': 'Anerkennung',
            'text': f'Du hast schon {appts_done} Termine geführt. Was funktioniert für dich gerade besonders gut?'
        })

    # Vision/Motivation
    if user['vision']:
        starters.append({
            'icon': '★', 'category': 'Motivation',
            'text': f'Wenn du daran denkst „{user["vision"][:60]}…" — was ist dein nächster konkreter Schritt?'
        })

    # Hängendes ansprechen
    if pending > 0:
        starters.append({
            'icon': '⏳', 'category': 'Hängende Themen',
            'text': f'Du hast {pending} Recherchen offen. Bei welchem Kunden brauchst du Unterstützung?'
        })

    # Karriere-Push
    career = career_for_row(user['manual_career_level'], own_eh)
    next_lvl = next((cl for cl in CAREER_LEVELS if cl['level'] == career['level'] + 1), None)
    if next_lvl:
        eh_to_go = max(0, next_lvl['min_eh'] - own_eh)
        starters.append({
            'icon': '🚀', 'category': 'Karriere',
            'text': f'Bis {next_lvl["short"]} fehlen dir noch {int(eh_to_go)} EH. Was ist dein Plan für die nächsten 30 Tage?'
        })

    # Coaching-Frage
    starters.append({
        'icon': '🤔', 'category': 'Reflexion',
        'text': 'Wenn du zurückblickst — was war diese Woche dein größter Erfolg? Was lief weniger gut?'
    })
    starters.append({
        'icon': '💪', 'category': 'Aktion',
        'text': 'Was ist die EINE Sache, die du diese Woche unbedingt schaffst?'
    })

    db.close()
    return starters


def get_forecast(user_id):
    """Predictive Analytics: wann erreicht der User die nächste Stufe?
    Linear-Regression auf den letzten 90 Tagen."""
    db = get_db()
    user = db.execute('SELECT manual_career_level FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        db.close()
        return None

    own_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (user_id,)).fetchone()['s']
    last_90_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben" AND date(abschluss_date) >= date("now", "-90 days")', (user_id,)).fetchone()['s']
    last_30_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben" AND date(abschluss_date) >= date("now", "-30 days")', (user_id,)).fetchone()['s']
    db.close()

    career = career_for_row(user['manual_career_level'], own_eh)
    next_lvl = next((c for c in CAREER_LEVELS if c['level'] == career['level'] + 1), None)
    if not next_lvl:
        return {'reached': True, 'message': '👑 Höchste Stufe erreicht'}

    eh_to_go = max(0, next_lvl['min_eh'] - own_eh)
    if eh_to_go == 0:
        return {'reached': True, 'message': f'✓ Du hast bereits die EH-Schwelle für {next_lvl["short"]} überschritten'}

    # Pace: gewichtet aus 30 + 90 Tagen (30d 70%, 90d 30%)
    weekly_30 = (last_30_eh / 30) * 7
    weekly_90 = (last_90_eh / 90) * 7
    weekly_pace = weekly_30 * 0.7 + weekly_90 * 0.3

    if weekly_pace <= 0:
        return {
            'reached': False, 'next_level': next_lvl, 'eh_to_go': int(eh_to_go),
            'weeks': None, 'pace': 0,
            'message': f'Bei aktuellem Tempo: keine EH-Bewegung — handeln nötig 🚨',
            'urgent': True
        }

    weeks = eh_to_go / weekly_pace
    months = weeks / 4.33
    target_date = (date.today() + timedelta(days=int(weeks * 7))).strftime('%d.%m.%Y')
    return {
        'reached': False, 'next_level': next_lvl, 'eh_to_go': int(eh_to_go),
        'weeks': round(weeks, 1), 'months': round(months, 1),
        'pace': int(weekly_pace), 'target_date': target_date,
        'message': f'Bei {int(weekly_pace)} EH/Woche: {next_lvl["short"]} in ca. {round(weeks)} Wochen ({target_date})',
        'urgent': False
    }


def detect_anomalies(scope_user_id=None):
    """Cached für 30 Min."""
    ckey = f'anomalies:{scope_user_id or "global"}'
    cached = cache_get(ckey)
    if cached is not None: return cached
    result = _detect_anomalies_uncached(scope_user_id)
    cache_set(ckey, result, ttl=1800)
    return result


def _detect_anomalies_uncached(scope_user_id=None):
    """Anomalie-Detection: wer hat plötzlich >50% Einbruch?"""
    db = get_db()
    if scope_user_id:
        ids = [scope_user_id] + get_all_descendants(scope_user_id)
    else:
        ids = [r['id'] for r in db.execute('SELECT id FROM users WHERE active = 1').fetchall()]
    if not ids:
        db.close()
        return []
    ph = ','.join('?' * len(ids))

    # Vergleich letzte 30d vs. davor 30d
    rows = db.execute(f'''
        SELECT u.id, u.name, u.phone,
               COALESCE(SUM(CASE WHEN date(c.abschluss_date) >= date('now', '-30 days') THEN c.einheiten ELSE 0 END), 0) as eh_30,
               COALESCE(SUM(CASE WHEN date(c.abschluss_date) BETWEEN date('now', '-60 days') AND date('now', '-31 days') THEN c.einheiten ELSE 0 END), 0) as eh_prev_30
        FROM users u
        LEFT JOIN contracts c ON c.owner_id = u.id AND c.status = "abgeschlossen" AND c.recherche_status = "freigegeben"
        WHERE u.id IN ({ph}) AND u.active = 1
        GROUP BY u.id
    ''', ids).fetchall()
    db.close()

    anomalies = []
    for r in rows:
        if r['eh_prev_30'] >= 200 and r['eh_30'] < r['eh_prev_30'] * 0.5:
            drop = ((r['eh_prev_30'] - r['eh_30']) / r['eh_prev_30']) * 100
            anomalies.append({
                'id': r['id'], 'name': r['name'], 'phone': r['phone'],
                'eh_30': int(r['eh_30']), 'eh_prev_30': int(r['eh_prev_30']),
                'drop_pct': int(drop),
                'message': f'{r["name"]}: -{int(drop)}% Einbruch ({int(r["eh_prev_30"])} → {int(r["eh_30"])} EH)'
            })
    anomalies.sort(key=lambda a: -a['drop_pct'])
    return anomalies[:5]


def get_activity_heatmap(user_id, days=180):
    """Liefert Heatmap-Daten: pro Tag die Anzahl Aktivitäten."""
    db = get_db()
    rows = db.execute('''
        SELECT date(created_at) as datum, COUNT(*) as count
        FROM activity_log
        WHERE user_id = ? AND date(created_at) >= date('now', '-' || ? || ' days')
        GROUP BY datum
    ''', (user_id, days)).fetchall()
    # Plus Verträge / Termine / Leads als Aktivitäts-Indikatoren
    contract_rows = db.execute('''
        SELECT date(created_at) as datum, COUNT(*) as count
        FROM contracts WHERE owner_id = ? AND date(created_at) >= date('now', '-' || ? || ' days')
        GROUP BY datum
    ''', (user_id, days)).fetchall()
    db.close()
    counts = {}
    for r in rows:
        counts[r['datum']] = counts.get(r['datum'], 0) + r['count']
    for r in contract_rows:
        counts[r['datum']] = counts.get(r['datum'], 0) + r['count'] * 2  # Verträge zählen doppelt
    # Liste aller Tage rückwärts
    today = date.today()
    result = []
    for i in range(days, -1, -1):
        d = today - timedelta(days=i)
        d_str = d.strftime('%Y-%m-%d')
        result.append({
            'date': d_str,
            'count': counts.get(d_str, 0),
            'weekday': d.weekday(),  # 0 = Monday
            'month': d.month,
        })
    return result


@app.route('/trophaeen')
@login_required
def trophaeen():
    """Übersicht aller eigenen Achievements + verfügbarer."""
    db = get_db()
    user_codes_rows = db.execute('SELECT achievement_code, unlocked_at FROM user_achievements WHERE user_id=? ORDER BY unlocked_at DESC', (current_user.id,)).fetchall()
    user_codes = {r['achievement_code']: r['unlocked_at'] for r in user_codes_rows}
    db.close()

    # Sortiert nach Tier dann erreicht/nicht-erreicht
    tier_order = {'platinum': 0, 'gold': 1, 'silver': 2, 'bronze': 3}
    items = []
    for a in ACHIEVEMENTS:
        items.append({
            **a,
            'tier_color': ACHIEVEMENT_TIER_COLORS.get(a['tier'], '#94a3b8'),
            'unlocked': a['code'] in user_codes,
            'unlocked_at': user_codes.get(a['code']),
        })
    items.sort(key=lambda x: (not x['unlocked'], tier_order.get(x['tier'], 9)))
    unlocked = sum(1 for i in items if i['unlocked'])
    return render_template('trophaeen.html', items=items, unlocked_count=unlocked, total_count=len(items))


@app.route('/api/search')
@login_required
def api_search():
    """Globale Suche über Partner, Kunden (Leads), Verträge, Termine."""
    q = (request.args.get('q') or '').strip()
    if not q or len(q) < 1:
        return jsonify({'results': []})

    db = get_db()
    pattern = f'%{q}%'

    # Scope: Admin sieht alle, Partner nur eigene Downline
    if current_user.role == 'admin':
        scope_ids = [r['id'] for r in db.execute('SELECT id FROM users WHERE active = 1').fetchall()]
    else:
        scope_ids = [current_user.id] + get_all_descendants(current_user.id)

    if not scope_ids:
        db.close()
        return jsonify({'results': []})
    ph = ','.join('?' * len(scope_ids))
    results = []

    # Partner
    partners = db.execute(
        f'SELECT id, name, email, phone, manual_career_level FROM users WHERE id IN ({ph}) AND active = 1 AND (LOWER(name) LIKE LOWER(?) OR LOWER(email) LIKE LOWER(?)) LIMIT 6',
        scope_ids + [pattern, pattern]
    ).fetchall()
    for p in partners:
        career = next((c for c in CAREER_LEVELS if c['level'] == (p['manual_career_level'] or 1)), CAREER_LEVELS[0])
        results.append({
            'type': 'partner', 'icon': '👤', 'group': 'Partner',
            'id': p['id'], 'title': p['name'],
            'subtitle': f"{career['short']} · {p['email']}",
            'url': f'/coaching/{p["id"]}',
            'badge': career['short'], 'badge_color': career['color']
        })

    # Leads / Kunden
    leads = db.execute(
        f'SELECT l.id, l.name, l.phone, l.status, u.name as berater FROM leads l JOIN users u ON l.owner_id = u.id WHERE l.owner_id IN ({ph}) AND (LOWER(l.name) LIKE LOWER(?) OR LOWER(l.phone) LIKE LOWER(?) OR LOWER(l.email) LIKE LOWER(?)) ORDER BY l.created_at DESC LIMIT 6',
        scope_ids + [pattern, pattern, pattern]
    ).fetchall()
    for l in leads:
        results.append({
            'type': 'lead', 'icon': '◇', 'group': 'Kunden / Namensliste',
            'id': l['id'], 'title': l['name'],
            'subtitle': f"Status: {l['status']} · Berater: {l['berater']}",
            'url': f'/leads/{l["id"]}/edit',
            'badge': l['status'], 'badge_color': '#3b82f6'
        })

    # Verträge
    contracts = db.execute(
        f'SELECT c.id, c.client_name, c.produkt, c.einheiten, c.status, c.recherche_status, u.name as berater FROM contracts c JOIN users u ON c.owner_id = u.id WHERE c.owner_id IN ({ph}) AND (LOWER(c.client_name) LIKE LOWER(?) OR LOWER(c.produkt) LIKE LOWER(?)) ORDER BY c.created_at DESC LIMIT 6',
        scope_ids + [pattern, pattern]
    ).fetchall()
    for c in contracts:
        results.append({
            'type': 'contract', 'icon': '📄', 'group': 'Verträge',
            'id': c['id'], 'title': c['client_name'],
            'subtitle': f"{c['produkt']} · {int(c['einheiten'] or 0)} EH · {c['berater']}",
            'url': f'/vertraege/{c["id"]}/edit',
            'badge': c['status'], 'badge_color': '#10b981' if c['status'] == 'abgeschlossen' else '#94a3b8'
        })

    # Termine
    termine = db.execute(
        f'SELECT a.id, a.title, a.client_name, a.termin_date, a.status, u.name as berater FROM appointments a JOIN users u ON a.owner_id = u.id WHERE a.owner_id IN ({ph}) AND (LOWER(a.title) LIKE LOWER(?) OR LOWER(COALESCE(a.client_name,"")) LIKE LOWER(?)) ORDER BY a.termin_date DESC LIMIT 4',
        scope_ids + [pattern, pattern]
    ).fetchall()
    for a in termine:
        results.append({
            'type': 'termin', 'icon': '◷', 'group': 'Termine',
            'id': a['id'], 'title': a['title'],
            'subtitle': f"{a['termin_date']} · {a['client_name'] or '–'} · {a['berater']}",
            'url': f'/termine/{a["id"]}/edit',
            'badge': a['status'], 'badge_color': '#8b5cf6'
        })

    db.close()
    return jsonify({'results': results})


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


ONBOARDING_ROADMAP_TASKS = [
    # Tag 1: Setup
    {'day': 1, 'code': 'profil_foto', 'title': 'Profilfoto hochladen', 'detail': 'Damit dein Team weiß wer du bist', 'url': '/profil', 'icon': '📷'},
    {'day': 1, 'code': 'vision_first', 'title': 'Deine Vision aufschreiben', 'detail': 'Warum machst du das? In 2 Sätzen', 'url': '/profil', 'icon': '★'},
    {'day': 1, 'code': 'first_call_upline', 'title': 'Strukturhöher anrufen', 'detail': 'Erstes Tagesgespräch — Plan für die Woche', 'url': '/team', 'icon': '☎'},
    # Tag 2: Namensliste
    {'day': 2, 'code': 'namensliste_10', 'title': '10 Personen in Namensliste', 'detail': 'Familie, Freunde, Kollegen — alle rein', 'url': '/namensliste', 'icon': '◎'},
    {'day': 2, 'code': 'first_call_3', 'title': '3 Anrufe heute', 'detail': 'Aus der Liste — wenigstens 3 Personen kontaktieren', 'url': '/namensliste', 'icon': '☎'},
    # Tag 3: Termine
    {'day': 3, 'code': 'first_termin', 'title': 'Ersten Termin planen', 'detail': 'Klein anfangen — mit jemandem aus deiner Komfort-Zone', 'url': '/termine/neu', 'icon': '◷'},
    {'day': 3, 'code': 'rk_3', 'title': '3 RK-Kontakte (Recruiting)', 'detail': 'Wer könnte Geschäftspartner werden? In RK-Liste', 'url': '/namensliste?typ=rk', 'icon': '◇'},
    # Tag 4: Verträge verstehen
    {'day': 4, 'code': 'training_video', 'title': 'Hauptprodukt verstanden', 'detail': 'Ergo Rente Chance Video durcharbeiten', 'url': '/weiterbildung', 'icon': '▤'},
    {'day': 4, 'code': 'first_call_5', 'title': '5 Anrufe heute', 'detail': 'Steigerung — 5 Personen aus Namensliste', 'url': '/namensliste', 'icon': '☎'},
    # Tag 5: Erstes Erfolgserlebnis
    {'day': 5, 'code': 'second_termin', 'title': '2. Termin diese Woche', 'detail': 'Wiederholung schafft Routine', 'url': '/termine/neu', 'icon': '◷'},
    {'day': 5, 'code': 'check_with_upline', 'title': 'Wochen-Check mit Strukturhöher', 'detail': 'Was lief gut, was muss anders?', 'url': '/team', 'icon': '⬢'},
    # Tag 6: Aufbauen
    {'day': 6, 'code': 'namensliste_30', 'title': '30 Kontakte in Namensliste', 'detail': 'Weiter ausbauen — Ziel: 100', 'url': '/namensliste', 'icon': '◎'},
    {'day': 6, 'code': 'first_vertrag', 'title': 'Ersten Vertrag eintragen', 'detail': 'Auch wenn klein — der erste Schritt zählt', 'url': '/vertraege/neu', 'icon': '€'},
    # Tag 7: Reflektion
    {'day': 7, 'code': 'reflektion', 'title': 'Wochen-Reflektion mit Strukturhöher', 'detail': 'Was lief gut, was nicht? Persönlich, nicht in App.', 'url': '/team', 'icon': '⬢'},
    {'day': 7, 'code': 'plan_next_week', 'title': 'Nächste Woche planen mit Mentor', 'detail': '5 konkrete Aktionen für KW2 — gemeinsam', 'url': '/team', 'icon': '✓'},

    # ═══════ WOCHE 2 — VERTIEFEN ═══════
    {'day': 14, 'code': 'eh_50', 'title': '50 EH safe', 'detail': 'Erste Verträge live — auch klein', 'url': '/vertraege', 'icon': '€'},
    {'day': 14, 'code': 'rk_10', 'title': '10 RK-Kontakte (Recruiting)', 'detail': 'Wer kommt mit ins Team?', 'url': '/namensliste?typ=rk', 'icon': '◇'},
    {'day': 14, 'code': 'mentor_call_2', 'title': '2× Mentor-Call diese Woche', 'detail': 'Nicht App fragen — Mensch fragen', 'url': '/team', 'icon': '☎'},

    # ═══════ WOCHE 3 — STEIGERUNG ═══════
    {'day': 21, 'code': 'eh_120', 'title': '120 EH zwischenstand', 'detail': 'Auf Kurs für 250 EH im 1. Monat', 'url': '/vertraege', 'icon': '€'},
    {'day': 21, 'code': 'first_gp', 'title': 'Ersten Geschäftspartner einbinden', 'detail': 'Mentor-Walking — gemeinsam mit Strukturhöher', 'url': '/team/neu', 'icon': '🤝'},

    # ═══════ WOCHE 4 — 1-MONATS-ZIEL ═══════
    {'day': 28, 'code': 'eh_250', 'title': '🎯 250 EH im 1. Monat (HARD-Ziel)', 'detail': 'Najib-Ziel — die safe-Zone für Stufe-Aufstieg', 'url': '/vertraege', 'icon': '🏆'},
    {'day': 28, 'code': 'gps_3', 'title': '🎯 3 Geschäftspartner eingebunden', 'detail': 'Ohne sie keine Stufenwechsel', 'url': '/team', 'icon': '🤝'},
    {'day': 28, 'code': 'monatsbilanz_mentor', 'title': 'Monats-Bilanz mit Strukturhöher', 'detail': 'Was war gut, was muss Monat 2 anders?', 'url': '/team', 'icon': '⬢'},

    # ═══════ MONAT 2 — TEAM AUFBAUEN ═══════
    {'day': 42, 'code': 'first_partner_termin', 'title': '1. Partner-Termin gemeinsam', 'detail': 'Du nimmst deinen GP zu seinem 1. Termin mit', 'url': '/termine', 'icon': '◷'},
    {'day': 42, 'code': 'eh_500', 'title': '500 EH erreicht (kumuliert)', 'detail': 'Stetig wachsen — kein Burnout', 'url': '/vertraege', 'icon': '€'},
    {'day': 56, 'code': 'mentor_weekly', 'title': 'Wöchentlicher Mentor-Slot fix', 'detail': '1× pro Woche fester Termin mit Strukturhöher', 'url': '/team', 'icon': '⬢'},
    {'day': 56, 'code': 'gp_5', 'title': '5 GPs aktiv', 'detail': 'Aufbau für LREP-Stufe', 'url': '/team', 'icon': '🤝'},

    # ═══════ MONAT 3 — LREP-VORBEREITUNG ═══════
    {'day': 70, 'code': 'eh_800', 'title': '800 EH (LREP-Schwelle nähert sich)', 'detail': 'Stetig — Mentor-Check 1×/Woche', 'url': '/vertraege', 'icon': '€'},
    {'day': 84, 'code': 'first_recruit_solo', 'title': 'Erster GP solo eingebunden', 'detail': 'Ohne Mentor — du machst es allein', 'url': '/team/neu', 'icon': '🚀'},
    {'day': 90, 'code': 'lrep_check', 'title': 'LREP-Bereitschaft mit Mentor checken', 'detail': 'Bist du soweit? Was fehlt noch?', 'url': '/team', 'icon': '⬢'},

    # ═══════ MONAT 4 — KARRIERE-PLAN ═══════
    {'day': 112, 'code': 'lrep_or_plan', 'title': 'LREP erreicht ODER Plan B mit Mentor', 'detail': 'Falls noch nicht: konkreter Plan was fehlt', 'url': '/team', 'icon': '🏆'},
    {'day': 112, 'code': 'gp_eigenes_team', 'title': 'Mein erster GP rekrutiert eigenen GP', 'detail': 'Multiplikation startet — coach deinen Partner', 'url': '/team', 'icon': '⬢'},

    # ═══════ MONAT 5 — SKALIEREN ═══════
    {'day': 140, 'code': 'eh_1500', 'title': '1500 EH kumuliert', 'detail': 'HREP rückt näher — Vollgas mit Team', 'url': '/vertraege', 'icon': '€'},
    {'day': 140, 'code': 'team_meeting', 'title': '1. eigenes Team-Meeting', 'detail': 'Du führst — alle deine GPs zusammen', 'url': '/team', 'icon': '👥'},

    # ═══════ MONAT 6 — STUFE 2 STABIL ═══════
    {'day': 180, 'code': 'lrep_stable', 'title': '🏆 LREP stabil + Plan für HREP', 'detail': '6-Monats-Bilanz mit Strukturhöher', 'url': '/team', 'icon': '🏆'},
    {'day': 180, 'code': 'mentor_others', 'title': 'Du mentorest selbst', 'detail': 'Dein 1. GP wird LREP — du bist die Hilfe', 'url': '/team', 'icon': '⬢'},
]


def get_onboarding_progress(user_id):
    """Returns dict mit progress + active_day + tasks für UI."""
    db = get_db()
    user = db.execute('SELECT manual_career_level, role FROM users WHERE id=?', (user_id,)).fetchone()
    if not user or user['role'] == 'admin':
        db.close()
        return None
    if (user['manual_career_level'] or 1) > 1:
        db.close()
        return None  # nur für Stufe-1
    # Day-Berechnung: erstes done-Datum als Start nehmen, fallback heute
    first_done = db.execute('SELECT MIN(completed_at) as fd FROM onboarding_roadmap WHERE user_id=?', (user_id,)).fetchone()
    try:
        if first_done and first_done['fd']:
            start = datetime.strptime(first_done['fd'][:10], '%Y-%m-%d').date()
        else:
            start = date.today()
        days_since = (date.today() - start).days
        active_day = min(7, max(1, days_since + 1))
    except Exception:
        active_day = 1
    # Done-Status pro Task
    done_rows = db.execute('SELECT day_number, task_code FROM onboarding_roadmap WHERE user_id=? AND completed_at IS NOT NULL', (user_id,)).fetchall()
    done = {(r['day_number'], r['task_code']) for r in done_rows}
    db.close()
    # Tasks pro Tag aufbereitet
    days = {}
    total_tasks = len(ONBOARDING_ROADMAP_TASKS)
    total_done = 0
    for t in ONBOARDING_ROADMAP_TASKS:
        d = t['day']
        if d not in days: days[d] = []
        is_done = (d, t['code']) in done
        if is_done: total_done += 1
        days[d].append({**t, 'done': is_done})
    return {
        'days': days, 'active_day': active_day,
        'total_tasks': total_tasks, 'total_done': total_done,
        'pct': round(total_done / total_tasks * 100, 1) if total_tasks else 0,
        'completed': total_done == total_tasks,
    }


@app.route('/onboarding/roadmap')
@login_required
def onboarding_roadmap():
    progress = get_onboarding_progress(current_user.id)
    if not progress:
        flash('Onboarding-Roadmap nur für Stufe 1 (REP).', 'info')
        return redirect(url_for('dashboard'))
    return render_template('onboarding_roadmap.html', p=progress)


@app.route('/onboarding/task/<int:day>/<code>/done', methods=['POST'])
@login_required
def onboarding_task_done(day, code):
    db = get_db()
    db.execute('INSERT OR REPLACE INTO onboarding_roadmap (user_id, day_number, task_code, completed_at) VALUES (?, ?, ?, datetime("now"))',
               (current_user.id, day, code))
    db.commit()
    db.close()
    return redirect(url_for('onboarding_roadmap'))


@app.route('/onboarding/task/<int:day>/<code>/undo', methods=['POST'])
@login_required
def onboarding_task_undo(day, code):
    db = get_db()
    db.execute('DELETE FROM onboarding_roadmap WHERE user_id=? AND day_number=? AND task_code=?',
               (current_user.id, day, code))
    db.commit()
    db.close()
    return redirect(url_for('onboarding_roadmap'))


# ====== JIBSON-PERSONAL-COACH (Admin-only Spezial-Section) ======
def get_admin_personal_dashboard(user_id):
    """Cached für 3 Min."""
    ckey = f'adm_pers:{user_id}'
    cached_v = cache_get(ckey)
    if cached_v is not None:
        return cached_v
    try:
        result = _get_admin_personal_dashboard_uncached(user_id)
    except Exception as ex:
        print(f"[get_admin_personal_dashboard] failed: {ex}")
        result = None
    cache_set(ckey, result, ttl=1800)
    return result


def _get_admin_personal_dashboard_uncached(user_id):
    """Persönliches Command-Center für den Admin/Top-Performer.
    Zeigt: Monats-EH-Ziel, GP-Gespräche, Zielgespräche, Strang-Status."""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        db.close()
        return None
    # EH-Ziel diesen Monat (default 5000, hardcoded für Najib)
    monthly_target = 5000
    # Aktuelle EH diesen Monat (gesamt-team inkl. Downline)
    descendants = [user_id] + get_all_descendants(user_id)
    ph = ','.join('?' * len(descendants))
    cur_month = date.today().strftime('%Y-%m')
    eh_this_month = db.execute(f'''SELECT COALESCE(SUM(einheiten),0) as s FROM contracts
                                    WHERE owner_id IN ({ph})
                                      AND status="abgeschlossen" AND recherche_status="freigegeben"
                                      AND strftime("%Y-%m", abschluss_date) = ?''',
                                descendants + [cur_month]).fetchone()['s']
    # GP-Gespräche (Termin mit typ='kundentermin' oder 'erstgespraech' diesen Monat)
    gp_count = db.execute(f'''SELECT COUNT(*) as c FROM appointments
                               WHERE owner_id IN ({ph})
                                 AND status='erledigt'
                                 AND strftime("%Y-%m", termin_date) = ?''',
                          descendants + [cur_month]).fetchone()['c']
    # Zielgespräche (Termin mit typ='recruiting' oder 'schulung')
    zg_count = db.execute(f'''SELECT COUNT(*) as c FROM appointments
                               WHERE owner_id IN ({ph})
                                 AND typ IN ('recruiting','schulung','seminar')
                                 AND strftime("%Y-%m", termin_date) = ?''',
                          descendants + [cur_month]).fetchone()['c']
    # Tage diesen Monat (für Tagesziel-Berechnung)
    today_d = date.today()
    days_in_month = ((today_d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
    days_passed = today_d.day
    days_left = max(1, days_in_month - days_passed)
    eh_remaining = max(0, monthly_target - eh_this_month)
    eh_per_day_needed = round(eh_remaining / days_left, 1)
    # Strang-Status pro direkter Sub-Struktur
    direct = db.execute('SELECT id, name, photo_path FROM users WHERE parent_id=? AND active=1 ORDER BY name', (user_id,)).fetchall()
    strang_status = []
    for d in direct:
        chain = [d['id']] + get_all_descendants(d['id'])
        cph = ','.join('?' * len(chain))
        s_eh = db.execute(f'''SELECT COALESCE(SUM(einheiten),0) as s FROM contracts
                              WHERE owner_id IN ({cph})
                                AND status="abgeschlossen" AND recherche_status="freigegeben"
                                AND strftime("%Y-%m", abschluss_date) = ?''',
                          chain + [cur_month]).fetchone()['s']
        strang_status.append({
            'id': d['id'], 'name': d['name'], 'photo': d['photo_path'],
            'eh_month': float(s_eh),
            'partners': len(chain),
        })
    strang_status.sort(key=lambda x: -x['eh_month'])
    db.close()
    return {
        'monthly_target': monthly_target,
        'eh_this_month': float(eh_this_month),
        'eh_remaining': eh_remaining,
        'eh_per_day_needed': eh_per_day_needed,
        'days_passed': days_passed,
        'days_in_month': days_in_month,
        'days_left': days_left,
        'pct': round(eh_this_month / monthly_target * 100, 1) if monthly_target else 0,
        'gp_count': gp_count,
        'zg_count': zg_count,
        'strang_status': strang_status,
    }


# ====== TYPEFORM/FORMSPREE WEBHOOK ======
@app.route('/api/webhook/lead/<token>', methods=['POST', 'GET'])
def webhook_lead(token):
    """Public Webhook-Endpoint für Typeform / Formspree / Zapier.
    POST mit JSON oder Form-Data: {name, email, phone, message?, source?}
    Token-protected — jeder User kriegt eigenen Token unter /webhook-setup."""
    db = get_db()
    tk = db.execute('SELECT * FROM webhook_tokens WHERE token=?', (token,)).fetchone()
    if not tk:
        db.close()
        return jsonify({'error': 'invalid token'}), 401
    if request.method == 'GET':
        # Test-Endpoint
        db.close()
        return jsonify({'ok': True, 'msg': 'Webhook aktiv. POST hierher um Lead anzulegen.', 'owner_id': tk['owner_id']})
    # POST: Lead extrahieren (akzeptiert JSON oder Form-Data)
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    # Typeform-Spezialfall: kommt als nested JSON
    if 'form_response' in data:
        answers = data['form_response'].get('answers', [])
        for a in answers:
            t = a.get('type')
            if t == 'short_text' or t == 'long_text':
                data.setdefault('name', a.get('text'))
            elif t == 'email':
                data.setdefault('email', a.get('email'))
            elif t == 'phone_number':
                data.setdefault('phone', a.get('phone_number'))
    name = (data.get('name') or data.get('Name') or '').strip()
    email = (data.get('email') or data.get('Email') or '').strip()
    phone = (data.get('phone') or data.get('Phone') or data.get('telefon') or '').strip()
    msg = (data.get('message') or data.get('Nachricht') or '').strip()
    source = (data.get('source') or 'webhook').strip()
    if not name and not email and not phone:
        db.close()
        return jsonify({'error': 'no data', 'received': data}), 400
    if not name: name = email or phone or 'Webhook-Lead'
    # Lead anlegen
    db.execute('''INSERT INTO leads (owner_id, name, email, phone, status, source, notizen, liste_typ)
                  VALUES (?, ?, ?, ?, 'neu', ?, ?, ?)''',
               (tk['owner_id'], name, email, phone, source, msg or None, tk['list_typ'] or 'vk'))
    # Token-Stats
    db.execute('UPDATE webhook_tokens SET last_used=datetime("now"), request_count=COALESCE(request_count,0)+1 WHERE id=?', (tk['id'],))
    db.commit()
    db.close()
    log_activity(tk['owner_id'], 'webhook_lead', f'Neuer Lead via Webhook: {name}', icon='◎', color='gold')
    # Push an Owner
    try:
        send_push_to_user(tk['owner_id'],
            title=f'◎ Neuer Lead!',
            body=f'{name}{" · " + phone if phone else ""}{" · " + email if email else ""}',
            url='/namensliste', urgent=True, tag='webhook',
            push_type='lead_won')
    except Exception:
        pass
    return jsonify({'ok': True, 'lead': name})


@app.route('/webhook-setup')
@login_required
def webhook_setup():
    db = get_db()
    tokens = db.execute('SELECT * FROM webhook_tokens WHERE owner_id=? ORDER BY id DESC', (current_user.id,)).fetchall()
    db.close()
    return render_template('webhook_setup.html', tokens=tokens)


@app.route('/webhook-setup/create', methods=['POST'])
@login_required
def webhook_create():
    import secrets
    token = secrets.token_urlsafe(24)
    label = (request.form.get('label') or '').strip() or 'Default'
    list_typ = request.form.get('list_typ', 'vk')
    if list_typ not in ('vk', 'rk'): list_typ = 'vk'
    db = get_db()
    db.execute('INSERT INTO webhook_tokens (owner_id, token, label, list_typ) VALUES (?, ?, ?, ?)',
               (current_user.id, token, label, list_typ))
    db.commit()
    db.close()
    flash(f'Webhook-Token „{label}" erstellt!', 'success')
    return redirect(url_for('webhook_setup'))


@app.route('/webhook-setup/<int:tid>/delete', methods=['POST'])
@login_required
def webhook_delete(tid):
    db = get_db()
    db.execute('DELETE FROM webhook_tokens WHERE id=? AND owner_id=?', (tid, current_user.id))
    db.commit()
    db.close()
    flash('Webhook gelöscht.', 'info')
    return redirect(url_for('webhook_setup'))


def _update_streak(user_id):
    """Streak-Logik: täglicher Login zählt. +1 wenn gestern auch, reset auf 1 wenn Lücke.
    Returns: (current_streak, is_new_today)"""
    db = get_db()
    row = db.execute('SELECT streak_days, streak_last_date FROM users WHERE id=?', (user_id,)).fetchone()
    if not row:
        db.close()
        return (0, False)
    today = date.today()
    today_iso = today.isoformat()
    last = row['streak_last_date']
    cur = row['streak_days'] or 0
    is_new_today = False
    if last == today_iso:
        db.close()
        return (cur, False)  # heute schon gezählt
    yesterday_iso = (today - timedelta(days=1)).isoformat()
    if last == yesterday_iso:
        cur += 1  # weiter
    else:
        cur = 1  # reset / start
    is_new_today = True
    db.execute('UPDATE users SET streak_days=?, streak_last_date=? WHERE id=?',
               (cur, today_iso, user_id))
    db.commit()
    db.close()
    return (cur, is_new_today)


def _has_vision_today(user_id):
    db = get_db()
    today_iso = date.today().isoformat()
    row = db.execute('SELECT id FROM vision_entries WHERE user_id=? AND datum=?',
                     (user_id, today_iso)).fetchone()
    db.close()
    return row is not None


@app.route('/api/vision/today', methods=['POST'])
@login_required
def vision_today():
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    skip = data.get('skip', False)
    db = get_db()
    today_iso = date.today().isoformat()
    if skip:
        # Skip — speichere leeren Eintrag damit nicht mehr gefragt wird
        db.execute('INSERT OR IGNORE INTO vision_entries (user_id, datum, text) VALUES (?, ?, ?)',
                   (current_user.id, today_iso, '__skip__'))
    elif text and len(text) >= 5:
        db.execute('INSERT OR REPLACE INTO vision_entries (user_id, datum, text) VALUES (?, ?, ?)',
                   (current_user.id, today_iso, text[:500]))
    else:
        db.close()
        return jsonify({'ok': False, 'error': 'text too short'}), 400
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/api/vision/recent')
@login_required
def vision_recent():
    """Letzte 7 Vision-Einträge (für Wochen-Briefing)."""
    db = get_db()
    rows = db.execute(
        'SELECT datum, text FROM vision_entries WHERE user_id=? AND text != "__skip__" ORDER BY datum DESC LIMIT 7',
        (current_user.id,)).fetchall()
    db.close()
    return jsonify({'entries': [{'datum': r['datum'], 'text': r['text']} for r in rows]})


def get_admin_dashboard_stats(user_id):
    """Konsolidiert ALLE 19 inline Admin-Dashboard-Queries.
    SWR-Cache: returnt sofort (auch stale), refresht im Background.
    Bei contract/lead/termin-INSERT wird via cache_invalidate('admin_dash:') geleert.
    Effekt: Dashboard NIE mehr Cold-Hit nach erstem Erfolg."""
    ckey = f'admin_dash:{user_id}'
    return cache_swr(ckey, lambda: _build_admin_dashboard_stats(user_id), ttl=1800)


def _build_admin_dashboard_stats(user_id):
    """Macht die echten 19 Queries — wird von cache_swr aufgerufen."""
    db = get_db()
    try:
        # ─── Globale Counts ───
        stats = {
            'total_users': db.execute('SELECT COUNT(*) as c FROM users WHERE active = 1').fetchone()['c'],
            'total_leads': db.execute('SELECT COUNT(*) as c FROM leads').fetchone()['c'],
            'total_contracts': db.execute('SELECT COUNT(*) as c FROM contracts WHERE status = "abgeschlossen" AND recherche_status = "freigegeben"').fetchone()['c'],
            'total_volumen': db.execute('SELECT COALESCE(SUM(volumen), 0) as s FROM contracts WHERE status = "abgeschlossen" AND recherche_status = "freigegeben"').fetchone()['s'],
            'total_einheiten': db.execute('SELECT COALESCE(SUM(einheiten), 0) as s FROM contracts WHERE status = "abgeschlossen" AND recherche_status = "freigegeben"').fetchone()['s'],
            'open_appointments': db.execute('SELECT COUNT(*) as c FROM appointments WHERE status = "geplant"').fetchone()['c'],
        }
        # ─── Top Performer ───
        top_rows = db.execute('''
            SELECT u.id, u.name, u.level, u.manual_career_level,
                   COALESCE(SUM(c.einheiten), 0) as einheiten,
                   COUNT(c.id) as vertraege
            FROM users u
            LEFT JOIN contracts c ON c.owner_id = u.id AND c.status = "abgeschlossen" AND recherche_status = "freigegeben"
            WHERE u.active = 1
            GROUP BY u.id ORDER BY einheiten DESC LIMIT 10
        ''').fetchall()
        top_performer = []
        for r in top_rows:
            d = dict(r)
            d['career'] = career_for_row(r['manual_career_level'], r['einheiten'])
            top_performer.append(d)
        stats['top_performer'] = top_performer
        # ─── Direkte Partner ───
        direct_rows = db.execute('''
            SELECT u.*, COUNT(c.id) as vertraege,
                   COALESCE(SUM(c.einheiten), 0) as einheiten,
                   COALESCE(SUM(c.volumen), 0) as volumen
            FROM users u
            LEFT JOIN contracts c ON c.owner_id = u.id AND c.status = "abgeschlossen" AND recherche_status = "freigegeben"
            WHERE u.parent_id = ? AND u.active = 1
            GROUP BY u.id
        ''', (user_id,)).fetchall()
        direct_partners = []
        for r in direct_rows:
            d = dict(r)
            d['career'] = career_for_row(r['manual_career_level'], r['einheiten'])
            try:
                act = get_user_activity_today(r['id'])
                d['active_today'] = act['active_today']
                la = act['last_active_at']
                d['last_active_days'] = (date.today() - datetime.strptime(la[:10], '%Y-%m-%d').date()).days if la else None
            except Exception:
                d['active_today'] = False
                d['last_active_days'] = None
            direct_partners.append(d)
        stats['direct_partners'] = direct_partners
        # ─── Letzte Verträge ───
        stats['recent_contracts'] = [dict(r) for r in db.execute('''
            SELECT c.*, u.name as berater_name FROM contracts c
            JOIN users u ON c.owner_id = u.id
            ORDER BY c.created_at DESC LIMIT 5
        ''').fetchall()]
        # ─── Monatliche Daten (6 Monate) ───
        stats['monthly_data'] = [dict(r) for r in db.execute('''
            SELECT strftime('%Y-%m', abschluss_date) as monat,
                   COUNT(*) as anzahl, SUM(einheiten) as einheiten
            FROM contracts
            WHERE status = "abgeschlossen" AND recherche_status = "freigegeben"
              AND abschluss_date >= date('now', '-6 months')
            GROUP BY monat ORDER BY monat
        ''').fetchall()]
        # ─── Vormonats-Vergleich (8 queries → 1 Funktion) ───
        cur_month = date.today().strftime('%Y-%m')
        prev_month = f'{date.today().year - 1}-12' if date.today().month == 1 else f'{date.today().year}-{date.today().month - 1:02d}'
        def _stat_for_month(month):
            return {
                'eh': db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE status="abgeschlossen" AND recherche_status="freigegeben" AND strftime("%Y-%m", abschluss_date)=?', (month,)).fetchone()['s'],
                'vtr': db.execute('SELECT COUNT(*) as c FROM contracts WHERE status="abgeschlossen" AND recherche_status="freigegeben" AND strftime("%Y-%m", abschluss_date)=?', (month,)).fetchone()['c'],
                'partner': db.execute('SELECT COUNT(*) as c FROM users WHERE strftime("%Y-%m", joined_date)=? AND active=1', (month,)).fetchone()['c'],
                'volumen': db.execute('SELECT COALESCE(SUM(volumen),0) as s FROM contracts WHERE status="abgeschlossen" AND recherche_status="freigegeben" AND strftime("%Y-%m", abschluss_date)=?', (month,)).fetchone()['s'],
            }
        cur_stats = _stat_for_month(cur_month)
        prev_stats = _stat_for_month(prev_month)
        def pct_change(cur, prev):
            if prev == 0: return 100 if cur > 0 else 0
            return ((cur - prev) / prev) * 100
        stats['comparison'] = {
            'cur_month': cur_month, 'prev_month': prev_month,
            'cur': cur_stats, 'prev': prev_stats,
            'eh_pct': pct_change(cur_stats['eh'], prev_stats['eh']),
            'vtr_pct': pct_change(cur_stats['vtr'], prev_stats['vtr']),
            'partner_pct': pct_change(cur_stats['partner'], prev_stats['partner']),
            'volumen_pct': pct_change(cur_stats['volumen'], prev_stats['volumen']),
        }
        # ─── Partner-Wachstum (12 Monate) ───
        stats['partner_growth'] = [dict(r) for r in db.execute('''
            SELECT strftime('%Y-%m', joined_date) as monat, COUNT(*) as neue_partner
            FROM users WHERE active = 1 AND joined_date >= date('now', '-12 months')
            GROUP BY monat ORDER BY monat
        ''').fetchall()]
    finally:
        db.close()
    return stats  # cache_set erfolgt durch cache_swr-Wrapper


@app.route('/dashboard')
@login_required
def dashboard():
    # Streak update (1× pro Tag)
    _update_streak(current_user.id)
    # 1× pro Tag (lazy beim ersten Dashboard-Load): Daily-Pushes
    _maybe_run_daily_pushes_lazy()
    # Catch-Up-Check: erstmaliger Login → Wizard zeigen
    if needs_catchup(current_user.id):
        return redirect(url_for('onboarding_catchup'))
    db = get_db()
    own_eh = get_user_total_eh(current_user.id, include_team=False)
    team_eh = get_user_total_eh(current_user.id, include_team=True)
    career = get_career_level_for_user(current_user.id)
    next_level = get_next_level(career['level'])
    progress_pct = 100
    eh_to_next = 0
    if next_level:
        progress_pct = min(100, (own_eh / next_level['min_eh']) * 100) if next_level['min_eh'] > 0 else 0
        eh_to_next = max(0, next_level['min_eh'] - own_eh)
    conversion = get_conversion_rate(current_user.id, include_team=(current_user.role == 'admin'))
    my_commissions = get_commissions_for_user(current_user.id)

    if current_user.role == 'admin':
        # ─── Mega-Cache: 19 inline queries → 1 cached call (TTL 30 Min) ───
        admin_stats = get_admin_dashboard_stats(current_user.id)
        total_users = admin_stats['total_users']
        total_leads = admin_stats['total_leads']
        total_contracts = admin_stats['total_contracts']
        total_volumen = admin_stats['total_volumen']
        total_einheiten = admin_stats['total_einheiten']
        open_appointments = admin_stats['open_appointments']
        top_performer = admin_stats['top_performer']
        direct_partners = admin_stats['direct_partners']
        recent_contracts = admin_stats['recent_contracts']
        monthly_data = admin_stats['monthly_data']
        comparison = admin_stats['comparison']
        # Inaktive bleibt eigener Helper (cached separat)
        inactive_team = get_inactive_team_members(current_user.id, days=1, scope='all')[:10]

        # Geschäftspartner-Entwicklung (12 Monate) — aus Mega-Cache
        partner_growth = admin_stats['partner_growth']

        admin_user = db.execute('SELECT vision FROM users WHERE id = ?', (current_user.id,)).fetchone()
        admin_vision = (admin_user['vision'] if admin_user else '') or ''
        admin_show_vision = session.pop('show_vision', False) and admin_vision.strip() != ''
        db.close()
        # KI-Coach: Top-3 Anrufe für Quick-Card — strang-isoliert (eigene Downline)
        coach_insights = get_smart_insights(scope_user_id=current_user.id)
        # Personalisierte Begrüßung
        greeting = get_greeting_for_user(current_user.name, career, next_level, own_eh, eh_to_next)
        # Monats- + Halbjahres-Daten + Karriere-Kriterien
        period_stats = get_period_stats(scope_user_id=None)
        career_criteria = get_career_criteria_status(current_user.id)
        # Production Deadlines
        deadlines = get_production_deadlines()
        # Power-KI-Empfehlungen + Forecast + Anomalien
        ki_recs = get_ki_recommendations(current_user.id, scope_user_id=None)
        forecast = get_forecast(current_user.id)
        anomalies = detect_anomalies(scope_user_id=None)
        ai_briefing = ai_generate_weekly_briefing(current_user.id)
        coach_actions = get_coach_actions(current_user.id, max_actions=5)
        structure_dist = get_structure_distribution(current_user.id, scope='all')
        forecast_30d = get_quoten_forecast(current_user.id, days=30)
        strang_status = get_strang_status(current_user.id)
        struktur_news = get_struktur_news(current_user.id, days=7, limit=10)
        db_s = get_db()
        s_row = db_s.execute('SELECT streak_days FROM users WHERE id=?', (current_user.id,)).fetchone()
        db_s.close()
        streak_days = (s_row['streak_days'] or 0) if s_row else 0
        vision_needed = not _has_vision_today(current_user.id)
        admin_personal = get_admin_personal_dashboard(current_user.id)
        return render_template('dashboard_admin.html',
            total_users=total_users, total_leads=total_leads,
            total_contracts=total_contracts, total_volumen=total_volumen,
            total_einheiten=total_einheiten, open_appointments=open_appointments,
            top_performer=top_performer, direct_partners=direct_partners,
            inactive_team=inactive_team,
            recent_contracts=recent_contracts,
            monthly_data=json.dumps([dict(r) for r in monthly_data]),
            own_eh=own_eh, team_eh=team_eh, career=career, next_level=next_level,
            progress_pct=progress_pct, eh_to_next=eh_to_next, all_levels=CAREER_LEVELS,
            conversion=conversion, termine_pro_abschluss=TERMINE_PRO_ABSCHLUSS,
            my_commissions=my_commissions, comparison=comparison,
            partner_growth=json.dumps([dict(r) for r in partner_growth]),
            vision_text=admin_vision, show_vision=admin_show_vision,
            coach_insights=coach_insights, greeting=greeting,
            period_stats=period_stats, career_criteria=career_criteria,
            ki_recs=ki_recs, forecast=forecast, anomalies=anomalies,
            ai_briefing=ai_briefing, deadlines=deadlines,
            coach_actions=coach_actions, structure_dist=structure_dist,
            forecast_30d=forecast_30d, strang_status=strang_status, struktur_news=struktur_news,
            streak_days=streak_days, vision_needed=vision_needed
        )
    else:
        stats = get_team_stats(current_user.id)
        # Top Performer der GESAMTEN Struktur (sichtbar für alle)
        top_rows = db.execute('''
            SELECT u.id, u.name, u.level,
                   COALESCE(SUM(c.einheiten), 0) as einheiten,
                   COUNT(c.id) as vertraege
            FROM users u
            LEFT JOIN contracts c ON c.owner_id = u.id AND c.status = "abgeschlossen" AND recherche_status = "freigegeben"
            WHERE u.active = 1
            GROUP BY u.id
            ORDER BY einheiten DESC
            LIMIT 10
        ''').fetchall()
        global_top = []
        for r in top_rows:
            d = dict(r)
            d['career'] = get_career_level_for_user(r['id'])
            d['is_me'] = (r['id'] == current_user.id)
            global_top.append(d)

        my_leads = db.execute('SELECT * FROM leads WHERE owner_id = ? ORDER BY created_at DESC LIMIT 5', (current_user.id,)).fetchall()
        my_appointments = db.execute('SELECT * FROM appointments WHERE owner_id = ? AND status = "geplant" ORDER BY termin_date LIMIT 5', (current_user.id,)).fetchall()

        direct_rows = db.execute('''
            SELECT u.*, COUNT(c.id) as vertraege,
                   COALESCE(SUM(c.einheiten), 0) as einheiten
            FROM users u
            LEFT JOIN contracts c ON c.owner_id = u.id AND c.status = "abgeschlossen" AND recherche_status = "freigegeben"
            WHERE u.parent_id = ? AND u.active = 1
            GROUP BY u.id
        ''', (current_user.id,)).fetchall()
        direct_team = []
        for r in direct_rows:
            d = dict(r)
            d['career'] = career_for_row(r['manual_career_level'], r['einheiten'])
            act = get_user_activity_today(r['id'])
            d['active_today'] = act['active_today']
            try:
                la = act['last_active_at']
                d['last_active_days'] = (date.today() - datetime.strptime(la[:10], '%Y-%m-%d').date()).days if la else None
            except Exception:
                d['last_active_days'] = None
            direct_team.append(d)

        current_month = date.today().strftime('%Y-%m')
        quota = db.execute('SELECT * FROM quotas WHERE user_id = ? AND monat = ?', (current_user.id, current_month)).fetchone()

        # Monatsentwicklung der letzten 6 Monate (eigenes Team)
        team_ids = [current_user.id] + get_all_descendants(current_user.id)
        ph = ','.join('?' * len(team_ids))
        monthly_data = db.execute(f'''
            SELECT strftime('%Y-%m', abschluss_date) as monat,
                   COUNT(*) as anzahl, COALESCE(SUM(einheiten),0) as einheiten,
                   COALESCE(SUM(volumen),0) as volumen
            FROM contracts
            WHERE status = "abgeschlossen" AND recherche_status = "freigegeben"
              AND owner_id IN ({ph})
              AND abschluss_date >= date('now', '-6 months')
            GROUP BY monat ORDER BY monat
        ''', team_ids).fetchall()

        # Vision aus DB holen (für Modal)
        own_user = db.execute('SELECT vision FROM users WHERE id = ?', (current_user.id,)).fetchone()
        vision_text = (own_user['vision'] if own_user else '') or ''
        show_vision = session.pop('show_vision', False) and vision_text.strip() != ''

        db.close()
        # KI-Coach: Insights für Partner (nur eigene Downline)
        coach_insights = get_smart_insights(scope_user_id=current_user.id)
        greeting = get_greeting_for_user(current_user.name, career, next_level, own_eh, eh_to_next)
        period_stats = get_period_stats(scope_user_id=current_user.id)
        career_criteria = get_career_criteria_status(current_user.id)
        ki_recs = get_ki_recommendations(current_user.id, scope_user_id=current_user.id)
        forecast = get_forecast(current_user.id)
        ai_briefing = ai_generate_weekly_briefing(current_user.id)
        deadlines = get_production_deadlines()
        coach_actions = get_coach_actions(current_user.id, max_actions=5)
        structure_dist = get_structure_distribution(current_user.id, scope='all')
        forecast_30d = get_quoten_forecast(current_user.id, days=30)
        strang_status = get_strang_status(current_user.id)
        struktur_news = get_struktur_news(current_user.id, days=7, limit=8)
        # Streak holen (kein update — schon oben passiert)
        db_s = get_db()
        s_row = db_s.execute('SELECT streak_days FROM users WHERE id=?', (current_user.id,)).fetchone()
        db_s.close()
        streak_days = (s_row['streak_days'] or 0) if s_row else 0
        vision_needed = not _has_vision_today(current_user.id)
        return render_template('dashboard_partner.html',
            stats=stats, my_leads=my_leads, my_appointments=my_appointments,
            direct_team=direct_team, quota=quota,
            own_eh=own_eh, team_eh=team_eh, career=career, next_level=next_level,
            progress_pct=progress_pct, eh_to_next=eh_to_next, all_levels=CAREER_LEVELS,
            conversion=conversion, termine_pro_abschluss=TERMINE_PRO_ABSCHLUSS,
            my_commissions=my_commissions, global_top=global_top,
            monthly_data=json.dumps([dict(r) for r in monthly_data]),
            vision_text=vision_text, show_vision=show_vision,
            coach_insights=coach_insights, greeting=greeting,
            period_stats=period_stats, career_criteria=career_criteria,
            ki_recs=ki_recs, forecast=forecast, ai_briefing=ai_briefing,
            deadlines=deadlines, coach_actions=coach_actions, structure_dist=structure_dist,
            forecast_30d=forecast_30d, strang_status=strang_status, struktur_news=struktur_news,
            streak_days=streak_days, vision_needed=vision_needed,
            admin_personal=None  # nur Admin hat das — Partner nicht
        )


# === LEADS ===
@app.route('/leads')
@login_required
def leads():
    db = get_db()
    if current_user.role == 'admin':
        rows = db.execute('SELECT l.*, u.name as berater_name FROM leads l JOIN users u ON l.owner_id = u.id ORDER BY l.created_at DESC').fetchall()
    else:
        ids = [current_user.id] + get_all_descendants(current_user.id)
        ph = ','.join('?' * len(ids))
        rows = db.execute(f'SELECT l.*, u.name as berater_name FROM leads l JOIN users u ON l.owner_id = u.id WHERE l.owner_id IN ({ph}) ORDER BY l.created_at DESC', ids).fetchall()
    db.close()
    return render_template('leads.html', leads=rows)


@app.route('/leads/neu', methods=['GET', 'POST'])
@login_required
def lead_neu():
    pre_typ = (request.args.get('typ') or 'vk').lower()
    if pre_typ not in ('vk', 'rk'): pre_typ = 'vk'
    if request.method == 'POST':
        liste_typ = (request.form.get('liste_typ') or 'vk').lower()
        if liste_typ not in ('vk', 'rk'): liste_typ = 'vk'
        db = get_db()
        db.execute('''INSERT INTO leads (owner_id, name, email, phone, birthday, produkt, status, notizen, liste_typ)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (current_user.id, request.form['name'], request.form.get('email', ''),
             request.form.get('phone', ''), request.form.get('birthday') or None,
             request.form.get('produkt', ''),
             request.form.get('status', 'neu'), request.form.get('notizen', ''), liste_typ))
        db.commit()
        db.close()
        log_activity(current_user.id, 'lead_neu',
            f'{current_user.name} hat „{request.form["name"]}" zur {"Rekrutierungs" if liste_typ == "rk" else "Vertriebs"}-Liste hinzugefügt',
            icon='◇', color='purple')
        flash(f'{("Rekrutierungs" if liste_typ == "rk" else "Vertriebs")}-Kontakt angelegt!', 'success')
        return redirect(url_for('namensliste', typ=liste_typ))
    return render_template('lead_form.html', lead=None, pre_typ=pre_typ)


@app.route('/leads/<int:lead_id>/edit', methods=['GET', 'POST'])
@login_required
def lead_edit(lead_id):
    db = get_db()
    lead = db.execute('SELECT * FROM leads WHERE id = ?', (lead_id,)).fetchone()
    if not lead:
        db.close()
        return redirect(url_for('namensliste'))
    if request.method == 'POST':
        liste_typ = (request.form.get('liste_typ') or lead['liste_typ'] or 'vk').lower()
        if liste_typ not in ('vk', 'rk'): liste_typ = 'vk'
        new_status = request.form.get('status', 'neu')
        # Wenn Status zu "kontaktiert" wechselt → Zeitstempel setzen (falls noch nicht da)
        kontakt_at = lead['kontaktiert_at']
        if new_status in ('kontakt', 'angebot', 'gewonnen', 'abgeschlossen') and not kontakt_at:
            kontakt_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        db.execute('''UPDATE leads SET name=?, email=?, phone=?, birthday=?, produkt=?, status=?,
                      notizen=?, liste_typ=?, kontaktiert_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?''',
            (request.form['name'], request.form.get('email', ''), request.form.get('phone', ''),
             request.form.get('birthday') or None,
             request.form.get('produkt', ''), new_status,
             request.form.get('notizen', ''), liste_typ, kontakt_at, lead_id))
        db.commit()
        db.close()
        # Push: wenn Status zu "gewonnen" wechselt
        if new_status in ('gewonnen', 'abgeschlossen') and lead['status'] not in ('gewonnen', 'abgeschlossen'):
            try:
                send_push_to_user(current_user.id,
                    title=f'✓ Lead gewonnen: {request.form["name"]}',
                    body=f'{liste_typ.upper()}-Kontakt erfolgreich gewonnen!',
                    url='/namensliste', tag='lead_won', push_type='lead_won')
            except Exception:
                pass
        flash('Kontakt aktualisiert!', 'success')
        return redirect(url_for('namensliste', typ=liste_typ))
    db.close()
    return render_template('lead_form.html', lead=lead, pre_typ=lead['liste_typ'] or 'vk')


@app.route('/leads/<int:lead_id>/delete', methods=['POST'])
@login_required
def lead_delete(lead_id):
    db = get_db()
    db.execute('DELETE FROM leads WHERE id = ?', (lead_id,))
    db.commit()
    db.close()
    return redirect(url_for('leads'))


# === VERTRÄGE ===
@app.route('/vertraege')
@login_required
def vertraege():
    db = get_db()
    if current_user.role == 'admin':
        rows = db.execute('SELECT c.*, u.name as berater_name FROM contracts c JOIN users u ON c.owner_id = u.id ORDER BY c.created_at DESC').fetchall()
    else:
        ids = [current_user.id] + get_all_descendants(current_user.id)
        ph = ','.join('?' * len(ids))
        rows = db.execute(f'SELECT c.*, u.name as berater_name FROM contracts c JOIN users u ON c.owner_id = u.id WHERE c.owner_id IN ({ph}) ORDER BY c.created_at DESC', ids).fetchall()
    db.close()
    return render_template('vertraege.html', vertraege=rows, eh_faktor=EH_FAKTOR)


@app.route('/vertraege/neu', methods=['GET', 'POST'])
@login_required
def vertrag_neu():
    if request.method == 'POST':
        volumen = float(request.form.get('volumen', 0) or 0)
        einheiten = volumen * EH_FAKTOR
        lead_id_raw = request.form.get('lead_id', '').strip()
        lead_id = int(lead_id_raw) if lead_id_raw and lead_id_raw.isdigit() else None
        db = get_db()
        cur = db.execute('''INSERT INTO contracts
                (owner_id, client_name, produkt, volumen, einheiten, provision, status, abschluss_date, notizen,
                 recherche_done, telefonat_done, unterlagen_done, nachweise_done, unterschrieben, freizeichnung_done,
                 recherche_status, kunde_birthday, lead_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (current_user.id, request.form['client_name'], request.form['produkt'],
             volumen, einheiten, float(request.form.get('provision', 0) or 0),
             request.form.get('status', 'offen'), request.form.get('abschluss_date', ''),
             request.form.get('notizen', ''),
             1 if request.form.get('recherche_done') else 0,
             1 if request.form.get('telefonat_done') else 0,
             1 if request.form.get('unterlagen_done') else 0,
             1 if request.form.get('nachweise_done') else 0,
             1 if request.form.get('unterschrieben') else 0,
             1 if request.form.get('freizeichnung_done') else 0,
             request.form.get('recherche_status', 'ausstehend'),
             request.form.get('kunde_birthday', '').strip() or None,
             lead_id))
        new_id = cur.lastrowid
        # Falls Lead gekoppelt: Lead auf "abgeschlossen" setzen
        if lead_id and request.form.get('status') == 'abgeschlossen':
            db.execute('UPDATE leads SET status="abgeschlossen" WHERE id=? AND owner_id=?', (lead_id, current_user.id))
        # Falls neuer Kunde mit Birthday eingegeben → auch in Namensliste anlegen wenn nicht vorhanden
        if not lead_id and request.form.get('kunde_birthday', '').strip():
            existing = db.execute('SELECT id FROM leads WHERE owner_id=? AND LOWER(name)=LOWER(?)',
                                  (current_user.id, request.form['client_name'])).fetchone()
            if not existing:
                db.execute('INSERT INTO leads (owner_id, name, birthday, status, source) VALUES (?, ?, ?, "abgeschlossen", "vertrag")',
                           (current_user.id, request.form['client_name'], request.form.get('kunde_birthday', '').strip()))
        db.commit()
        db.close()
        auto_promote_user(current_user.id)
        recalculate_all_commissions()
        cache_invalidate('ctx:'); cache_invalidate('news:'); cache_invalidate('coach_acts:'); cache_invalidate('forecast:'); cache_invalidate('strang:'); cache_invalidate('adm_pers:'); cache_invalidate('admin_dash:')
        if request.form.get('status') == 'abgeschlossen' and request.form.get('recherche_status') == 'freigegeben':
            log_activity(current_user.id, 'vertrag_abgeschlossen',
                f'{current_user.name} hat Vertrag „{request.form["client_name"]}" abgeschlossen ({einheiten:.0f} EH)',
                icon='🎉', color='green')
            # Push an Upline (falls vorhanden)
            try:
                u_row = get_db().execute('SELECT parent_id FROM users WHERE id=?', (current_user.id,)).fetchone()
                if u_row and u_row['parent_id']:
                    send_push_to_user(u_row['parent_id'],
                        title=f'🎉 {current_user.name} hat abgeschlossen!',
                        body=f'{request.form["client_name"]} · {int(einheiten)} EH',
                        url='/team', tag='abschluss', push_type='contract_done')
            except Exception:
                pass
        else:
            log_activity(current_user.id, 'vertrag_neu',
                f'{current_user.name} hat neuen Vertrag „{request.form["client_name"]}" angelegt ({einheiten:.0f} EH)',
                icon='📄', color='gold')
        flash(f'Vertrag angelegt! ({einheiten:.0f} EH)', 'success')
        return redirect(url_for('vertraege'))
    # GET: Lead-Liste für Selector mit birthday/phone
    db = get_db()
    leads_for_select = db.execute(
        'SELECT id, name, birthday, phone, produkt FROM leads WHERE owner_id=? ORDER BY name',
        (current_user.id,)).fetchall()
    db.close()
    return render_template('vertrag_form.html', vertrag=None, eh_faktor=EH_FAKTOR,
                           leads_for_select=leads_for_select)


@app.route('/vertraege/<int:vid>/edit', methods=['GET', 'POST'])
@login_required
def vertrag_edit(vid):
    db = get_db()
    vertrag = db.execute('SELECT * FROM contracts WHERE id = ?', (vid,)).fetchone()
    if not vertrag:
        db.close()
        return redirect(url_for('vertraege'))
    if request.method == 'POST':
        volumen = float(request.form.get('volumen', 0) or 0)
        einheiten = volumen * EH_FAKTOR
        owner_id = vertrag['owner_id']
        lead_id_raw = request.form.get('lead_id', '').strip()
        lead_id = int(lead_id_raw) if lead_id_raw and lead_id_raw.isdigit() else None
        db.execute('''UPDATE contracts SET client_name=?, produkt=?, volumen=?, einheiten=?, provision=?, status=?, abschluss_date=?, notizen=?,
                      recherche_done=?, telefonat_done=?, unterlagen_done=?, nachweise_done=?, unterschrieben=?, freizeichnung_done=?,
                      recherche_status=?, kunde_birthday=?, lead_id=?
                      WHERE id=?''',
            (request.form['client_name'], request.form['produkt'], volumen, einheiten,
             float(request.form.get('provision', 0) or 0), request.form.get('status', 'offen'),
             request.form.get('abschluss_date', ''), request.form.get('notizen', ''),
             1 if request.form.get('recherche_done') else 0,
             1 if request.form.get('telefonat_done') else 0,
             1 if request.form.get('unterlagen_done') else 0,
             1 if request.form.get('nachweise_done') else 0,
             1 if request.form.get('unterschrieben') else 0,
             1 if request.form.get('freizeichnung_done') else 0,
             request.form.get('recherche_status', 'ausstehend'),
             request.form.get('kunde_birthday', '').strip() or None,
             lead_id, vid))
        db.commit()
        db.close()
        auto_promote_user(owner_id)
        recalculate_all_commissions()
        cache_invalidate('ctx:'); cache_invalidate('news:'); cache_invalidate('coach_acts:'); cache_invalidate('forecast:'); cache_invalidate('strang:'); cache_invalidate('adm_pers:'); cache_invalidate('admin_dash:')
        flash(f'Vertrag aktualisiert! ({einheiten:.0f} EH)', 'success')
        return redirect(url_for('vertraege'))
    leads_for_select = db.execute(
        'SELECT id, name, birthday, phone, produkt FROM leads WHERE owner_id=? ORDER BY name',
        (vertrag['owner_id'],)).fetchall()
    db.close()
    return render_template('vertrag_form.html', vertrag=vertrag, eh_faktor=EH_FAKTOR,
                           leads_for_select=leads_for_select)


@app.route('/vertraege/<int:vid>/delete', methods=['POST'])
@login_required
def vertrag_delete(vid):
    db = get_db()
    db.execute('DELETE FROM commissions WHERE contract_id = ?', (vid,))
    db.execute('DELETE FROM contracts WHERE id = ?', (vid,))
    db.commit()
    db.close()
    recalculate_all_commissions()
    return redirect(url_for('vertraege'))


# === TERMINE ===
@app.route('/termine')
@login_required
def termine():
    db = get_db()
    if current_user.role == 'admin':
        rows = db.execute('SELECT a.*, u.name as berater_name FROM appointments a JOIN users u ON a.owner_id = u.id ORDER BY a.termin_date ASC').fetchall()
    else:
        ids = [current_user.id] + get_all_descendants(current_user.id)
        ph = ','.join('?' * len(ids))
        rows = db.execute(f'SELECT a.*, u.name as berater_name FROM appointments a JOIN users u ON a.owner_id = u.id WHERE a.owner_id IN ({ph}) ORDER BY a.termin_date ASC', ids).fetchall()
    db.close()
    return render_template('termine.html', termine=rows)


@app.route('/termine/neu', methods=['GET', 'POST'])
@login_required
def termin_neu():
    if request.method == 'POST':
        db = get_db()
        db.execute('INSERT INTO appointments (owner_id, title, client_name, termin_date, termin_time, typ, status, notizen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (current_user.id, request.form['title'], request.form.get('client_name', ''),
             request.form['termin_date'], request.form.get('termin_time', ''),
             request.form.get('typ', 'kundentermin'), request.form.get('status', 'geplant'),
             request.form.get('notizen', '')))
        db.commit()
        db.close()
        log_activity(current_user.id, 'termin_neu',
            f'{current_user.name} hat Termin „{request.form["title"]}" für {request.form["termin_date"]} angelegt',
            icon='◷', color='blue')
        # Push: Termin-Bestätigung an User selbst
        try:
            send_push_to_user(current_user.id,
                title=f'📅 Termin eingetragen',
                body=f'{request.form["title"]} · {request.form["termin_date"]}',
                url='/termine', tag='termin', push_type='appointment_made')
        except Exception:
            pass
        flash('Termin angelegt!', 'success')
        return redirect(url_for('termine'))
    return render_template('termin_form.html', termin=None)


@app.route('/termine/<int:tid>')
@login_required
def termin_detail(tid):
    """Read-only Detail-Ansicht für einen einzelnen Termin (verlinkt aus Tagesliste)."""
    db = get_db()
    t = db.execute('''SELECT a.*, u.name as owner_name, u.photo_path as owner_photo, u.email as owner_email, u.phone as owner_phone
                      FROM appointments a JOIN users u ON a.owner_id = u.id WHERE a.id = ?''', (tid,)).fetchone()
    if not t:
        db.close()
        flash('Termin nicht gefunden.', 'error')
        return redirect(url_for('termine'))
    # Berechtigung: Owner, Admin oder im Strang
    descendants = get_all_descendants(current_user.id)
    if not (current_user.has_admin_access or t['owner_id'] == current_user.id or t['owner_id'] in descendants):
        # Auch erlaubt wenn current_user Attendee ist
        is_att = False
        try:
            if t['attendee_ids']:
                att_ids = [int(x) for x in json.loads(t['attendee_ids']) if str(x).isdigit()]
                is_att = current_user.id in att_ids
        except Exception:
            pass
        if not is_att:
            db.close()
            flash('Keine Berechtigung für diesen Termin.', 'error')
            return redirect(url_for('termine'))
    # Attendees auflösen
    attendees = []
    try:
        if t['attendee_ids']:
            att_ids = [int(x) for x in json.loads(t['attendee_ids']) if str(x).isdigit()]
            if att_ids:
                ph = ','.join('?' * len(att_ids))
                rows = db.execute(f'SELECT id, name, photo_path, email, phone FROM users WHERE id IN ({ph})', att_ids).fetchall()
                attendees = [dict(r) for r in rows]
    except Exception:
        pass
    db.close()
    # Berechne End-Zeit
    end_time = None
    if t['termin_time']:
        try:
            sh, sm = int(t['termin_time'][:2]), int(t['termin_time'][3:5])
            dur = t['duration_min'] or 60
            tot = sh * 60 + sm + dur
            end_time = f'{(tot // 60) % 24:02d}:{tot % 60:02d}'
        except Exception:
            pass
    return render_template('termin_detail.html', t=t, attendees=attendees, end_time=end_time,
                           can_edit=(current_user.has_admin_access or t['owner_id'] == current_user.id))


@app.route('/termine/<int:tid>/edit', methods=['GET', 'POST'])
@login_required
def termin_edit(tid):
    db = get_db()
    termin = db.execute('SELECT * FROM appointments WHERE id = ?', (tid,)).fetchone()
    if not termin:
        db.close()
        return redirect(url_for('termine'))
    if request.method == 'POST':
        db.execute('UPDATE appointments SET title=?, client_name=?, termin_date=?, termin_time=?, typ=?, status=?, notizen=? WHERE id=?',
            (request.form['title'], request.form.get('client_name', ''),
             request.form['termin_date'], request.form.get('termin_time', ''),
             request.form.get('typ', 'kundentermin'), request.form.get('status', 'geplant'),
             request.form.get('notizen', ''), tid))
        db.commit()
        db.close()
        flash('Termin aktualisiert!', 'success')
        return redirect(url_for('termine'))
    db.close()
    return render_template('termin_form.html', termin=termin)


@app.route('/termine/<int:tid>/delete', methods=['POST'])
@login_required
def termin_delete(tid):
    db = get_db()
    db.execute('DELETE FROM appointments WHERE id = ?', (tid,))
    db.commit()
    db.close()
    return redirect(url_for('termine'))


# === TEAM / STRUKTUR ===
@app.route('/team')
@login_required
def team():
    db = get_db()
    if current_user.role == 'admin':
        rows = db.execute('''
            SELECT u.*, p.name as parent_name,
                   COUNT(DISTINCT l.id) as leads_count,
                   COUNT(DISTINCT c.id) as contracts_count,
                   COALESCE(SUM(c.volumen), 0) as volumen,
                   COALESCE(SUM(c.einheiten), 0) as einheiten
            FROM users u
            LEFT JOIN users p ON u.parent_id = p.id
            LEFT JOIN leads l ON l.owner_id = u.id
            LEFT JOIN contracts c ON c.owner_id = u.id AND c.status = "abgeschlossen" AND recherche_status = "freigegeben"
            WHERE u.active = 1
            GROUP BY u.id ORDER BY u.level, u.name
        ''').fetchall()
    else:
        ids = [current_user.id] + get_all_descendants(current_user.id)
        ph = ','.join('?' * len(ids))
        rows = db.execute(f'''
            SELECT u.*, p.name as parent_name,
                   COUNT(DISTINCT l.id) as leads_count,
                   COUNT(DISTINCT c.id) as contracts_count,
                   COALESCE(SUM(c.volumen), 0) as volumen,
                   COALESCE(SUM(c.einheiten), 0) as einheiten
            FROM users u
            LEFT JOIN users p ON u.parent_id = p.id
            LEFT JOIN leads l ON l.owner_id = u.id
            LEFT JOIN contracts c ON c.owner_id = u.id AND c.status = "abgeschlossen" AND recherche_status = "freigegeben"
            WHERE u.id IN ({ph}) AND u.active = 1
            GROUP BY u.id ORDER BY u.level, u.name
        ''', ids).fetchall()
    members = []
    for r in rows:
        d = dict(r)
        d['career'] = career_for_row(r['manual_career_level'], r['einheiten'])
        members.append(d)
    db.close()
    return render_template('team.html', members=members, all_levels=CAREER_LEVELS)


@app.route('/team/neu', methods=['GET', 'POST'])
@login_required
def team_neu():
    db = get_db()
    if current_user.role == 'admin':
        possible_parents = db.execute('SELECT id, name, level FROM users WHERE active = 1 ORDER BY level, name').fetchall()
    else:
        ids = [current_user.id] + get_all_descendants(current_user.id)
        ph = ','.join('?' * len(ids))
        possible_parents = db.execute(f'SELECT id, name, level FROM users WHERE id IN ({ph}) AND active = 1 ORDER BY level, name', ids).fetchall()

    if request.method == 'POST':
        parent_id = int(request.form['parent_id'])
        parent = db.execute('SELECT level FROM users WHERE id = ?', (parent_id,)).fetchone()
        new_level = (parent['level'] + 1) if parent else 1

        try:
            chosen_level = int(request.form.get('manual_career_level', 1))
            if chosen_level < 1 or chosen_level > 6:
                chosen_level = 1
        except (ValueError, TypeError):
            chosen_level = 1

        is_admin = current_user.role == 'admin'

        if is_admin:
            manual_level = chosen_level
            pending_level = None
            pending_by = None
            pending_at = None
        else:
            manual_level = 1
            if chosen_level > 1:
                pending_level = chosen_level
                pending_by = current_user.id
                pending_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            else:
                pending_level = None
                pending_by = None
                pending_at = None

        email = request.form['email'].strip()
        existing = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
        if existing:
            flash('E-Mail bereits vorhanden!', 'error')
        else:
            generated_password = (request.form.get('password') or '').strip() or generate_random_password()
            try:
                initial_eh_val = float(request.form.get('initial_eh', 0) or 0)
            except (ValueError, TypeError):
                initial_eh_val = 0
            cur = db.execute('''INSERT INTO users (name, email, password, role, parent_id, level, phone,
                          manual_career_level, pending_career_level, pending_by_user_id, pending_at,
                          must_change_password, initial_eh)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (request.form['name'], email, hash_password(generated_password),
                 'partner', parent_id, new_level, request.form.get('phone', ''),
                 manual_level, pending_level, pending_by, pending_at, 1, initial_eh_val))
            new_user_id = cur.lastrowid
            # Auto-Recruiting-Tracking: gibt's einen offenen RK-Lead mit gleichem Namen oder gleicher E-Mail?
            recruited_lead = db.execute('''
                SELECT id FROM leads
                WHERE owner_id=? AND liste_typ='rk'
                  AND status NOT IN ('gewonnen','abgeschlossen','verloren')
                  AND (LOWER(name)=LOWER(?) OR (email IS NOT NULL AND LOWER(email)=LOWER(?)))
                LIMIT 1
            ''', (current_user.id, request.form['name'], email)).fetchone()
            if recruited_lead:
                db.execute('UPDATE leads SET status=?, kontaktiert_at=COALESCE(kontaktiert_at, datetime("now")), updated_at=datetime("now") WHERE id=?',
                           ('gewonnen', recruited_lead['id']))
            db.commit()
            db.close()
            stufe_short = next((cl['short'] for cl in CAREER_LEVELS if cl['level'] == manual_level), 'REP')
            log_activity(new_user_id, 'partner_neu',
                f'{request.form["name"]} ist neuer Geschäftspartner ({stufe_short})',
                icon='●', color='green')
            # Push: Werber kriegt "Du hast jemanden rekrutiert!"
            try:
                send_push_to_user(current_user.id,
                    title=f'◇ Du hast {request.form["name"]} rekrutiert!',
                    body=f'Neuer Geschäftspartner ({stufe_short}) — willkommen heißen!',
                    url=f'/partner/{new_user_id}/profil', urgent=True, tag='recruiting',
                    push_type='partner_recruited')
            except Exception:
                pass

            # Welcome-E-Mail
            mail_status = ''
            if is_smtp_configured():
                ok, err = send_welcome_email(email, request.form['name'], generated_password,
                                              sender_name=current_user.name)
                mail_status = ' 📧 Welcome-E-Mail verschickt.' if ok else f' ⚠ E-Mail fehlgeschlagen: {(err or "")[:80]}'
            else:
                mail_status = ' (SMTP nicht konfiguriert — Login-Daten manuell weitergeben)'

            if pending_level:
                flash(f'Mitglied angelegt! Stufe {pending_level} wartet auf Bestätigung.{mail_status}', 'success')
            else:
                flash(f'Mitglied angelegt! Login: {email} / Passwort: {generated_password}{mail_status}', 'success')
            return redirect(url_for('team'))
    db.close()
    return render_template('team_form.html', member=None, possible_parents=possible_parents, all_levels=CAREER_LEVELS)


@app.route('/partner/<int:uid>/profil')
@login_required
def partner_profil(uid):
    """Vollständiges LinkedIn-Style Profil eines Partners."""
    db = get_db()
    target = db.execute('SELECT * FROM users WHERE id=? AND active=1', (uid,)).fetchone()
    if not target:
        db.close()
        return redirect(url_for('team'))
    descendants = get_all_descendants(current_user.id)
    if not (current_user.has_admin_access or uid == current_user.id or uid in descendants):
        db.close()
        flash('Keine Berechtigung', 'error')
        return redirect(url_for('team'))
    # Pin-Bar: diesen Aufruf für „Zuletzt geöffnet" tracken
    record_partner_view(current_user.id, uid)
    # Career
    own_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (uid,)).fetchone()['s']
    own_eh += target['initial_eh'] or 0
    target_career = career_for_row(target['manual_career_level'], own_eh)
    # Total stats
    contracts_total = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (uid,)).fetchone()['c']
    volumen_total = db.execute('SELECT COALESCE(SUM(volumen),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (uid,)).fetchone()['s']
    provision_total = db.execute('SELECT COALESCE(SUM(amount),0) as s FROM commissions WHERE user_id=?', (uid,)).fetchone()['s']
    leads_total = db.execute('SELECT COUNT(*) as c FROM leads WHERE owner_id=?', (uid,)).fetchone()['c']
    appts_done = db.execute('SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND status="erledigt"', (uid,)).fetchone()['c']
    appts_planned = db.execute('SELECT COUNT(*) as c FROM appointments WHERE owner_id=? AND status="geplant"', (uid,)).fetchone()['c']
    direct_partners = db.execute('SELECT COUNT(*) as c FROM users WHERE parent_id=? AND active=1', (uid,)).fetchone()['c']
    team_size = len(get_all_descendants(uid))
    # Trophies
    trophy_rows = db.execute('SELECT * FROM user_achievements WHERE user_id=? ORDER BY unlocked_at DESC', (uid,)).fetchall()
    trophy_count = len(trophy_rows)
    trophies = []
    for t in trophy_rows[:10]:
        trophies.append({'code': t['achievement_code'], 'unlocked_at': t['unlocked_at']})
    # Last contracts
    last_contracts = db.execute('SELECT * FROM contracts WHERE owner_id=? ORDER BY created_at DESC LIMIT 5', (uid,)).fetchall()
    # Last appointments
    last_appts = db.execute('SELECT * FROM appointments WHERE owner_id=? ORDER BY termin_date DESC LIMIT 5', (uid,)).fetchall()
    # Direct downline
    direct_dl = db.execute('SELECT * FROM users WHERE parent_id=? AND active=1 ORDER BY name', (uid,)).fetchall()
    direct_dl_with_career = []
    for d in direct_dl:
        d_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (d['id'],)).fetchone()['s']
        d_eh += d['initial_eh'] or 0
        direct_dl_with_career.append({**dict(d), 'career': career_for_row(d['manual_career_level'], d_eh), 'eh': d_eh})
    # Activity heatmap (90 Tage)
    heatmap = db.execute('''
        SELECT date(created_at) as d, COUNT(*) as c FROM activity_log
        WHERE user_id=? AND date(created_at) >= date('now','-90 days')
        GROUP BY d ORDER BY d
    ''', (uid,)).fetchall()
    heatmap_dict = {r['d']: r['c'] for r in heatmap}
    # Upline
    upline = None
    if target['parent_id']:
        upline = db.execute('SELECT id, name, photo_path FROM users WHERE id=?', (target['parent_id'],)).fetchone()
        if upline: upline = dict(upline)
    db.close()
    return render_template('partner_profil.html',
        target=target, target_career=target_career, own_eh=own_eh,
        contracts_total=contracts_total, volumen_total=volumen_total, provision_total=provision_total,
        leads_total=leads_total, appts_done=appts_done, appts_planned=appts_planned,
        direct_partners=direct_partners, team_size=team_size,
        trophy_count=trophy_count, trophies=trophies,
        last_contracts=last_contracts, last_appts=last_appts,
        direct_dl=direct_dl_with_career,
        heatmap_dict=heatmap_dict, upline=upline,
        today_iso=date.today().isoformat()
    )


@app.route('/partner/<int:uid>')
@login_required
def partner_today(uid):
    """Was hat ein Partner HEUTE gemacht? Sichtbar für Upline-Kette + Admin."""
    db = get_db()
    target = db.execute('SELECT * FROM users WHERE id=? AND active=1', (uid,)).fetchone()
    if not target:
        db.close()
        flash('Partner nicht gefunden', 'error')
        return redirect(url_for('team'))
    # Berechtigung: Admin, Upline-Kette, oder selbst
    descendants = get_all_descendants(current_user.id)
    if not (current_user.has_admin_access or uid == current_user.id or uid in descendants):
        db.close()
        flash('Keine Berechtigung', 'error')
        return redirect(url_for('team'))
    today = date.today().isoformat()
    activity = get_user_activity_today(uid)
    today_leads = db.execute("SELECT * FROM leads WHERE owner_id=? AND date(created_at)=? ORDER BY created_at DESC", (uid, today)).fetchall()
    today_termine = db.execute("SELECT * FROM appointments WHERE owner_id=? AND (date(created_at)=? OR date(termin_date)=?) ORDER BY termin_date DESC", (uid, today, today)).fetchall()
    today_vertraege = db.execute("SELECT * FROM contracts WHERE owner_id=? AND date(created_at)=? ORDER BY created_at DESC", (uid, today)).fetchall()
    try:
        today_tasks = db.execute("SELECT * FROM user_tasks WHERE user_id=? AND date(datum)=? ORDER BY id", (uid, today)).fetchall()
    except Exception:
        today_tasks = []
    # Aktivitäts-Verlauf der letzten 14 Tage (Heatmap-Lite)
    history = db.execute('''
        SELECT date(created_at) as d, COUNT(*) as c FROM activity_log
        WHERE user_id=? AND date(created_at) >= date('now', '-14 days')
        GROUP BY d ORDER BY d
    ''', (uid,)).fetchall()
    own_eh = get_user_total_eh(uid, include_team=False)
    target_career = career_for_row(target['manual_career_level'], own_eh)
    db.close()
    return render_template('partner_today.html',
        target=target, target_career=target_career, own_eh=own_eh,
        activity=activity, today_leads=today_leads, today_termine=today_termine,
        today_vertraege=today_vertraege, today_tasks=today_tasks,
        history=[dict(h) for h in history], today=today)


@app.route('/team/inaktiv')
@login_required
def team_inactive():
    """Liste aller inaktiven Partner (für Führungskräfte)."""
    days = int(request.args.get('days', 1))
    scope = 'all' if (current_user.has_admin_access or request.args.get('scope') == 'all') else 'direct'
    inactive = get_inactive_team_members(current_user.id, days=days, scope=scope)
    return render_template('team_inactive.html', inactive=inactive, days=days, scope=scope)


@app.route('/team/<int:uid>/edit', methods=['GET', 'POST'])
@login_required
def team_edit(uid):
    db = get_db()
    member = db.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
    if not member:
        db.close()
        return redirect(url_for('team'))

    # Permission: Admin oder Mitglied in eigener Downline (oder eigener Datensatz)
    allowed_ids = [current_user.id] + get_all_descendants(current_user.id)
    is_admin = current_user.role == 'admin'
    if not is_admin and uid not in allowed_ids:
        db.close()
        flash('Keine Berechtigung', 'error')
        return redirect(url_for('team'))

    if is_admin:
        possible_parents = db.execute('SELECT id, name, level FROM users WHERE active = 1 AND id != ? ORDER BY level, name', (uid,)).fetchall()
    else:
        ph = ','.join('?' * len(allowed_ids))
        possible_parents = db.execute(f'SELECT id, name, level FROM users WHERE id IN ({ph}) AND active = 1 AND id != ? ORDER BY level, name', allowed_ids + [uid]).fetchall()

    if request.method == 'POST':
        parent_id = int(request.form['parent_id'])
        parent = db.execute('SELECT level FROM users WHERE id = ?', (parent_id,)).fetchone()
        new_level = (parent['level'] + 1) if parent else 1
        new_password = request.form.get('password', '').strip()

        try:
            chosen_level = int(request.form.get('manual_career_level', member['manual_career_level'] or 1))
            if chosen_level < 1 or chosen_level > 6:
                chosen_level = 1
        except (ValueError, TypeError):
            chosen_level = member['manual_career_level'] or 1

        manual_level = member['manual_career_level'] or 1
        pending_level = member['pending_career_level']
        pending_by = member['pending_by_user_id']
        pending_at = member['pending_at']

        if is_admin:
            manual_level = chosen_level
            pending_level = None
            pending_by = None
            pending_at = None
        else:
            if chosen_level > manual_level:
                pending_level = chosen_level
                pending_by = current_user.id
                pending_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        ob_eg = 1 if request.form.get('onboarding_endgespraech') else 0
        ob_e1 = 1 if request.form.get('onboarding_einarbeitung_1') else 0
        ob_e2 = 1 if request.form.get('onboarding_einarbeitung_2') else 0
        ob_e3 = 1 if request.form.get('onboarding_einarbeitung_3') else 0
        ob_sb = 1 if request.form.get('onboarding_seminar_bezahlt') else 0
        try:
            initial_eh_val = float(request.form.get('initial_eh', 0) or 0)
        except (ValueError, TypeError):
            initial_eh_val = member['initial_eh'] or 0

        if new_password:
            db.execute('''UPDATE users SET name=?, email=?, phone=?, parent_id=?, level=?, password=?,
                          manual_career_level=?, pending_career_level=?, pending_by_user_id=?, pending_at=?,
                          onboarding_endgespraech=?, onboarding_einarbeitung_1=?, onboarding_einarbeitung_2=?,
                          onboarding_einarbeitung_3=?, onboarding_seminar_bezahlt=?, initial_eh=?
                          WHERE id=?''',
                (request.form['name'], request.form['email'], request.form.get('phone', ''),
                 parent_id, new_level, hash_password(new_password),
                 manual_level, pending_level, pending_by, pending_at,
                 ob_eg, ob_e1, ob_e2, ob_e3, ob_sb, initial_eh_val, uid))
        else:
            db.execute('''UPDATE users SET name=?, email=?, phone=?, parent_id=?, level=?,
                          manual_career_level=?, pending_career_level=?, pending_by_user_id=?, pending_at=?,
                          onboarding_endgespraech=?, onboarding_einarbeitung_1=?, onboarding_einarbeitung_2=?,
                          onboarding_einarbeitung_3=?, onboarding_seminar_bezahlt=?, initial_eh=?
                          WHERE id=?''',
                (request.form['name'], request.form['email'], request.form.get('phone', ''),
                 parent_id, new_level,
                 manual_level, pending_level, pending_by, pending_at,
                 ob_eg, ob_e1, ob_e2, ob_e3, ob_sb, initial_eh_val, uid))
        db.commit()
        db.close()
        recalculate_all_commissions()
        cache_invalidate('ctx:'); cache_invalidate('news:'); cache_invalidate('coach_acts:'); cache_invalidate('forecast:'); cache_invalidate('strang:'); cache_invalidate('adm_pers:'); cache_invalidate('admin_dash:')
        if pending_level and not is_admin:
            flash(f'Aktualisiert. Stufen-Änderung auf {pending_level} wartet auf Admin-Bestätigung.', 'success')
        else:
            flash('Mitglied aktualisiert!', 'success')
        return redirect(url_for('team'))
    db.close()
    return render_template('team_form.html', member=member, possible_parents=possible_parents, all_levels=CAREER_LEVELS)


@app.route('/team/<int:uid>/deactivate', methods=['POST'])
@login_required
def team_deactivate(uid):
    db = get_db()
    db.execute('UPDATE users SET active = 0 WHERE id = ?', (uid,))
    db.commit()
    db.close()
    flash('Mitglied deaktiviert', 'info')
    return redirect(url_for('team'))


# === TAGESAUFGABEN ===
@app.route('/aufgaben')
@login_required
def aufgaben():
    db = get_db()
    today = date.today().strftime('%Y-%m-%d')
    user_career = get_career_level_for_user(current_user.id)
    user_level = user_career['level']

    tasks = db.execute('SELECT * FROM daily_tasks WHERE level = ? AND active = 1 ORDER BY sort_order, id', (user_level,)).fetchall()

    user_status = {}
    for t in tasks:
        ut = db.execute('SELECT * FROM user_tasks WHERE user_id = ? AND task_id = ? AND datum = ?',
                        (current_user.id, t['id'], today)).fetchone()
        user_status[t['id']] = {'done': bool(ut and ut['done']), 'done_at': ut['done_at'] if ut else None}

    history = db.execute('''
        SELECT ut.datum, COUNT(*) as total,
               SUM(CASE WHEN ut.done = 1 THEN 1 ELSE 0 END) as done_count
        FROM user_tasks ut
        WHERE ut.user_id = ? AND ut.datum >= date('now', '-13 days')
        GROUP BY ut.datum ORDER BY ut.datum DESC
    ''', (current_user.id,)).fetchall()

    # Eigene Todos für heute
    personal = db.execute('SELECT * FROM personal_todos WHERE user_id=? AND datum=? ORDER BY done, id',
                          (current_user.id, today)).fetchall()
    db.close()
    return render_template('aufgaben.html',
        tasks=tasks, user_status=user_status, today=today, career=user_career,
        history=history, personal_todos=personal)


@app.route('/aufgaben/eigene/neu', methods=['POST'])
@login_required
def personal_todo_create():
    title = (request.form.get('title') or '').strip()
    if not title or len(title) > 200:
        return redirect(url_for('aufgaben'))
    db = get_db()
    db.execute('INSERT INTO personal_todos (user_id, title, datum) VALUES (?, ?, ?)',
               (current_user.id, title, date.today().strftime('%Y-%m-%d')))
    db.commit()
    db.close()
    return redirect(url_for('aufgaben'))


@app.route('/aufgaben/eigene/<int:tid>/toggle', methods=['POST'])
@login_required
def personal_todo_toggle(tid):
    db = get_db()
    row = db.execute('SELECT * FROM personal_todos WHERE id=? AND user_id=?', (tid, current_user.id)).fetchone()
    if row:
        new_done = 0 if row['done'] else 1
        db.execute('UPDATE personal_todos SET done=?, done_at=CASE WHEN ?=1 THEN datetime("now") ELSE NULL END WHERE id=?',
                   (new_done, new_done, tid))
        db.commit()
    db.close()
    return redirect(url_for('aufgaben'))


@app.route('/aufgaben/eigene/<int:tid>/delete', methods=['POST'])
@login_required
def personal_todo_delete(tid):
    db = get_db()
    db.execute('DELETE FROM personal_todos WHERE id=? AND user_id=?', (tid, current_user.id))
    db.commit()
    db.close()
    return redirect(url_for('aufgaben'))


@app.route('/aufgaben/toggle', methods=['POST'])
@login_required
def aufgabe_toggle():
    db = get_db()
    task_id = int(request.form['task_id'])
    today = date.today().strftime('%Y-%m-%d')
    existing = db.execute('SELECT * FROM user_tasks WHERE user_id = ? AND task_id = ? AND datum = ?',
                          (current_user.id, task_id, today)).fetchone()
    if existing:
        new_done = 0 if existing['done'] else 1
        db.execute('UPDATE user_tasks SET done = ?, done_at = CASE WHEN ? = 1 THEN datetime("now") ELSE NULL END WHERE id = ?',
                   (new_done, new_done, existing['id']))
    else:
        db.execute('INSERT INTO user_tasks (user_id, task_id, datum, done, done_at) VALUES (?, ?, ?, 1, datetime("now"))',
                   (current_user.id, task_id, today))
    db.commit()
    db.close()
    return redirect(url_for('aufgaben'))


@app.route('/admin/aufgaben', methods=['GET', 'POST'])
@login_required
def admin_aufgaben():
    if not current_user.has_admin_access:
        flash('Nur Admins haben Zugriff', 'error')
        return redirect(url_for('dashboard'))

    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            db.execute('INSERT INTO daily_tasks (level, title, description, sort_order) VALUES (?, ?, ?, ?)',
                       (int(request.form['level']), request.form['title'],
                        request.form.get('description', ''),
                        int(request.form.get('sort_order', 0) or 0)))
            db.commit()
            flash('Aufgabe hinzugefügt!', 'success')
        elif action == 'delete':
            tid = int(request.form['task_id'])
            db.execute('DELETE FROM user_tasks WHERE task_id = ?', (tid,))
            db.execute('DELETE FROM daily_tasks WHERE id = ?', (tid,))
            db.commit()
            flash('Aufgabe gelöscht', 'info')
        db.close()
        return redirect(url_for('admin_aufgaben'))

    tasks_by_level = {}
    for cl in CAREER_LEVELS:
        tasks_by_level[cl['level']] = db.execute(
            'SELECT * FROM daily_tasks WHERE level = ? AND active = 1 ORDER BY sort_order, id',
            (cl['level'],)
        ).fetchall()
    db.close()
    return render_template('admin_aufgaben.html', tasks_by_level=tasks_by_level, all_levels=CAREER_LEVELS)


# === PROVISIONEN ===
@app.route('/provisionen')
@login_required
def provisionen():
    db = get_db()
    if current_user.role == 'admin':
        ids = [u['id'] for u in db.execute('SELECT id FROM users WHERE active = 1').fetchall()]
    else:
        ids = [current_user.id] + get_all_descendants(current_user.id)

    ph = ','.join('?' * len(ids)) if ids else '0'

    # Gesamt-Provisionen pro User
    user_rows = db.execute(f'''
        SELECT u.id, u.name, u.email, u.level,
               COALESCE(SUM(CASE WHEN c.is_own = 1 THEN c.amount ELSE 0 END), 0) as eigen_provision,
               COALESCE(SUM(CASE WHEN c.is_own = 0 THEN c.amount ELSE 0 END), 0) as differenz_provision,
               COALESCE(SUM(c.amount), 0) as total_provision
        FROM users u
        LEFT JOIN commissions c ON c.user_id = u.id
        WHERE u.id IN ({ph}) AND u.active = 1
        GROUP BY u.id
        ORDER BY total_provision DESC
    ''', ids).fetchall()

    user_list = []
    for r in user_rows:
        d = dict(r)
        own_eh = db.execute('SELECT COALESCE(SUM(einheiten), 0) as s FROM contracts WHERE owner_id = ? AND status = "abgeschlossen" AND recherche_status = "freigegeben"', (r['id'],)).fetchone()['s']
        manual = db.execute('SELECT manual_career_level FROM users WHERE id = ?', (r['id'],)).fetchone()
        d['career'] = career_for_row(manual['manual_career_level'] if manual else 1, own_eh)
        d['own_eh'] = own_eh
        user_list.append(d)

    # Letzte Provisions-Bewegungen
    recent = db.execute(f'''
        SELECT c.*, u.name as user_name, k.client_name, k.einheiten as v_eh, k.produkt
        FROM commissions c
        JOIN users u ON c.user_id = u.id
        JOIN contracts k ON c.contract_id = k.id
        WHERE c.user_id IN ({ph})
        ORDER BY c.created_at DESC LIMIT 30
    ''', ids).fetchall()

    # Totals
    total_paid = sum(u['total_provision'] for u in user_list)
    total_own = sum(u['eigen_provision'] for u in user_list)
    total_diff = sum(u['differenz_provision'] for u in user_list)

    db.close()
    return render_template('provisionen.html',
        users=user_list, recent=recent,
        total_paid=total_paid, total_own=total_own, total_diff=total_diff,
        all_levels=CAREER_LEVELS)


# === LIVE ACTIVITY FEED ===
@app.route('/feed')
@login_required
def feed():
    """Live Feed: Admin sieht systemweit, alle anderen nur eigene Struktur (self + Downline).
    Verhindert dass Geschäftspartner Aktivitäten aus fremden Strukturen sehen."""
    db = get_db()
    if current_user.has_admin_access:
        rows = db.execute('''
            SELECT a.*, u.name as user_name
            FROM activity_log a
            LEFT JOIN users u ON a.user_id = u.id
            ORDER BY a.created_at DESC LIMIT 100
        ''').fetchall()
    else:
        ids = [current_user.id] + get_all_descendants(current_user.id)
        ph = ','.join('?' * len(ids))
        rows = db.execute(f'''
            SELECT a.*, u.name as user_name
            FROM activity_log a
            LEFT JOIN users u ON a.user_id = u.id
            WHERE a.user_id IN ({ph})
            ORDER BY a.created_at DESC LIMIT 100
        ''', ids).fetchall()

    # Gruppiere nach Datum
    grouped = {}
    today = date.today()
    for r in rows:
        d = dict(r)
        try:
            event_dt = datetime.strptime(r['created_at'][:19], '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            event_dt = datetime.now()
        event_date = event_dt.date()
        if event_date == today:
            key = 'Heute'
        elif event_date == today - timedelta(days=1):
            key = 'Gestern'
        else:
            key = event_date.strftime('%d.%m.%Y')
        d['time'] = event_dt.strftime('%H:%M')
        grouped.setdefault(key, []).append(d)
    db.close()
    return render_template('feed.html', grouped=grouped)


# === WOCHENZIELE & RANKING ===
@app.route('/ziele', methods=['GET', 'POST'])
@login_required
def ziele():
    """Eigene Wochenziele setzen + sehen."""
    db = get_db()
    week = get_week_start()

    if request.method == 'POST':
        existing = db.execute('SELECT id FROM weekly_goals WHERE user_id = ? AND week_start = ?',
                              (current_user.id, week)).fetchone()
        params = (
            int(request.form.get('ziel_termine', 0) or 0),
            int(request.form.get('ziel_vertraege', 0) or 0),
            float(request.form.get('ziel_einheiten', 0) or 0),
            int(request.form.get('ziel_neue_partner', 0) or 0),
            int(request.form.get('ziel_anrufe', 0) or 0),
        )
        if existing:
            db.execute('''UPDATE weekly_goals SET ziel_termine=?, ziel_vertraege=?, ziel_einheiten=?,
                          ziel_neue_partner=?, ziel_anrufe=? WHERE id=?''', params + (existing['id'],))
        else:
            db.execute('''INSERT INTO weekly_goals (user_id, week_start, ziel_termine, ziel_vertraege,
                          ziel_einheiten, ziel_neue_partner, ziel_anrufe)
                          VALUES (?, ?, ?, ?, ?, ?, ?)''', (current_user.id, week) + params)
        db.commit()
        flash('Wochenziele gespeichert!', 'success')
        db.close()
        return redirect(url_for('ziele'))

    goal = db.execute('SELECT * FROM weekly_goals WHERE user_id = ? AND week_start = ?',
                      (current_user.id, week)).fetchone()

    # Ist-Werte für aktuelle Woche
    week_start = week
    week_end = (datetime.strptime(week_start, '%Y-%m-%d').date() + timedelta(days=6)).strftime('%Y-%m-%d')

    ist_termine = db.execute('SELECT COUNT(*) as c FROM appointments WHERE owner_id = ? AND date(termin_date) BETWEEN ? AND ?',
                             (current_user.id, week_start, week_end)).fetchone()['c']
    ist_vertraege = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id = ? AND status="abgeschlossen" AND recherche_status="freigegeben" AND date(abschluss_date) BETWEEN ? AND ?',
                               (current_user.id, week_start, week_end)).fetchone()['c']
    ist_einheiten = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id = ? AND status="abgeschlossen" AND recherche_status="freigegeben" AND date(abschluss_date) BETWEEN ? AND ?',
                               (current_user.id, week_start, week_end)).fetchone()['s']
    ist_neue_partner = db.execute('SELECT COUNT(*) as c FROM users WHERE parent_id = ? AND date(joined_date) BETWEEN ? AND ?',
                                  (current_user.id, week_start, week_end)).fetchone()['c']
    db.close()

    today = date.today()
    week_end_d = datetime.strptime(week_end, '%Y-%m-%d').date()
    days_total = 7
    days_passed = (today - datetime.strptime(week_start, '%Y-%m-%d').date()).days + 1
    days_passed = max(1, min(days_total, days_passed))
    week_progress = (days_passed / days_total) * 100

    return render_template('ziele.html',
        goal=goal, week_start=week_start, week_end=week_end,
        ist_termine=ist_termine, ist_vertraege=ist_vertraege,
        ist_einheiten=ist_einheiten, ist_neue_partner=ist_neue_partner,
        days_passed=days_passed, days_total=days_total,
        week_progress=int(week_progress))


@app.route('/ranking')
@login_required
def ranking():
    """Wochen-Ranking aller Mitglieder — alle sehen es."""
    db = get_db()
    week = get_week_start()
    week_end = (datetime.strptime(week, '%Y-%m-%d').date() + timedelta(days=6)).strftime('%Y-%m-%d')

    rows = db.execute('''
        SELECT u.id, u.name, u.email, u.manual_career_level,
               COALESCE(SUM(CASE WHEN c.status="abgeschlossen" AND c.recherche_status="freigegeben" THEN c.einheiten ELSE 0 END), 0) as week_eh,
               COUNT(DISTINCT CASE WHEN c.status="abgeschlossen" AND c.recherche_status="freigegeben" THEN c.id END) as week_vtr
        FROM users u
        LEFT JOIN contracts c ON c.owner_id = u.id AND date(c.abschluss_date) BETWEEN ? AND ?
        WHERE u.active = 1
        GROUP BY u.id
        ORDER BY week_eh DESC, week_vtr DESC
        LIMIT 20
    ''', (week, week_end)).fetchall()

    members = []
    for r in rows:
        d = dict(r)
        d['career'] = next((cl for cl in CAREER_LEVELS if cl['level'] == (r['manual_career_level'] or 1)), CAREER_LEVELS[0])
        d['is_me'] = (r['id'] == current_user.id)
        members.append(d)

    db.close()
    return render_template('ranking.html', members=members, week_start=week, week_end=week_end)


# === COACHING-KARTE ===
@app.route('/coaching/<int:uid>', methods=['GET', 'POST'])
@login_required
def coaching(uid):
    """Coaching-Karte für einen Partner — nur Admin oder Upline."""
    db = get_db()
    member = db.execute('SELECT * FROM users WHERE id = ? AND active = 1', (uid,)).fetchone()
    if not member:
        db.close()
        return redirect(url_for('team'))

    # Berechtigung
    allowed_ids = [current_user.id] + get_all_descendants(current_user.id)
    is_admin = current_user.role == 'admin'
    if not is_admin and uid not in allowed_ids:
        db.close()
        flash('Keine Berechtigung', 'error')
        return redirect(url_for('team'))

    # POST: neue Coaching-Notiz hinzufügen
    if request.method == 'POST':
        note = request.form.get('note', '').strip()
        next_session = request.form.get('next_session_date', '').strip() or None
        if note:
            db.execute('INSERT INTO coaching_notes (target_user_id, author_user_id, note, next_session_date) VALUES (?, ?, ?, ?)',
                       (uid, current_user.id, note, next_session))
            db.commit()
            flash('Coaching-Notiz gespeichert', 'success')
        db.close()
        return redirect(url_for('coaching', uid=uid))

    # GET: Coaching-Daten zusammenstellen
    own_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id = ? AND status="abgeschlossen" AND recherche_status="freigegeben"', (uid,)).fetchone()['s']
    career = next((cl for cl in CAREER_LEVELS if cl['level'] == (member['manual_career_level'] or 1)), CAREER_LEVELS[0])
    next_career = next((cl for cl in CAREER_LEVELS if cl['level'] == career['level'] + 1), None)

    # Statistiken
    total_termine = db.execute('SELECT COUNT(*) as c FROM appointments WHERE owner_id = ? AND status="erledigt"', (uid,)).fetchone()['c']
    total_vertraege = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id = ? AND status="abgeschlossen" AND recherche_status="freigegeben"', (uid,)).fetchone()['c']
    total_leads = db.execute('SELECT COUNT(*) as c FROM leads WHERE owner_id = ?', (uid,)).fetchone()['c']
    pending_research = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id = ? AND recherche_status IN ("ausstehend", "")', (uid,)).fetchone()['c']
    avg_termine_per_close = (total_termine / total_vertraege) if total_vertraege > 0 else 0

    # Direkte Downline
    downline_count = db.execute('SELECT COUNT(*) as c FROM users WHERE parent_id = ? AND active = 1', (uid,)).fetchone()['c']
    full_team = len(get_all_descendants(uid))

    # Letzte Aktivitäten
    recent_activity = db.execute('SELECT * FROM activity_log WHERE user_id = ? ORDER BY created_at DESC LIMIT 10', (uid,)).fetchall()

    # Coaching-Notes
    notes = db.execute('''
        SELECT cn.*, u.name as author_name FROM coaching_notes cn
        LEFT JOIN users u ON cn.author_user_id = u.id
        WHERE cn.target_user_id = ?
        ORDER BY cn.created_at DESC
    ''', (uid,)).fetchall()

    # Onboarding-Score
    ob_score = sum([member['onboarding_endgespraech'], member['onboarding_einarbeitung_1'],
                    member['onboarding_einarbeitung_2'], member['onboarding_einarbeitung_3'],
                    member['onboarding_seminar_bezahlt']])

    # Smart Coaching-Tipps generieren
    tipps = []
    if avg_termine_per_close > 4:
        tipps.append({'icon':'🎯', 'color':'orange',
                     'title':'Termin-Qualität verbessern',
                     'text':f'{avg_termine_per_close:.1f} Termine pro Abschluss (Ziel: 3). Coaching: bessere Vorqualifikation.'})
    elif avg_termine_per_close > 0 and avg_termine_per_close < 2.5:
        tipps.append({'icon':'⭐', 'color':'green',
                     'title':'Top-Performance bei Termin-Qualität',
                     'text':f'Nur {avg_termine_per_close:.1f} Termine pro Abschluss — über dem Schnitt!'})

    if pending_research > 2:
        tipps.append({'icon':'⏳', 'color':'orange',
                     'title':f'{pending_research} hängende Recherchen',
                     'text':'Bitte um Status-Update der ausstehenden Verträge.'})

    if next_career:
        eh_to_go = max(0, next_career['min_eh'] - own_eh)
        progress = (own_eh / next_career['min_eh'] * 100) if next_career['min_eh'] > 0 else 0
        if progress >= 70:
            tipps.append({'icon':'🚀', 'color':'gold',
                         'title':f'Kurz vor {next_career["short"]}',
                         'text':f'Nur noch {int(eh_to_go)} EH! Motivieren, gemeinsam push organisieren.'})

    if downline_count == 0 and member['manual_career_level'] >= 2:
        tipps.append({'icon':'🌱', 'color':'purple',
                     'title':'Noch keine Downline',
                     'text':'Als '+career['short']+' sollten erste Partner aufgebaut werden — Geschäftspartner-Aufbau coachen.'})

    if total_leads < 10 and total_vertraege < 3:
        tipps.append({'icon':'◇', 'color':'blue',
                     'title':'Namensliste ausbauen',
                     'text':f'Nur {total_leads} Personen in der Namensliste — Akquise stärken!'})

    if ob_score < 3 and member['joined_date']:
        try:
            tage_dabei = (date.today() - datetime.strptime(member['joined_date'], '%Y-%m-%d').date()).days
            if tage_dabei > 30:
                tipps.append({'icon':'🎓', 'color':'red',
                             'title':'Onboarding nachholen',
                             'text':f'Erst {ob_score}/5 Schritte erledigt nach {tage_dabei} Tagen.'})
        except (ValueError, TypeError):
            pass

    db.close()
    heatmap = get_activity_heatmap(uid, days=180)
    konv_starter = get_konversations_starter(uid)
    diagnosis = heuristic_coaching_diagnosis(uid)
    return render_template('coaching.html',
        member=dict(member), career=career, next_career=next_career,
        own_eh=own_eh,
        stats={'termine': total_termine, 'vertraege': total_vertraege, 'leads': total_leads,
               'pending_research': pending_research, 'avg_termine_per_close': avg_termine_per_close,
               'downline_count': downline_count, 'full_team': full_team, 'ob_score': ob_score},
        recent_activity=recent_activity, notes=notes, tipps=tipps,
        heatmap=heatmap, konv_starter=konv_starter, diagnosis=diagnosis)


# === KI-COACH ===
@app.route('/coach')
@login_required
def coach():
    """KI-basierte Empfehlungen für den eingeloggten User."""
    scope = None if current_user.role == 'admin' else current_user.id
    insights = get_smart_insights(scope_user_id=scope)
    return render_template('coach.html', insights=insights)


@app.route('/coach/briefing')
@login_required
def coach_briefing():
    """Tägliches Briefing — kompaktes HTML, später als E-Mail versendbar."""
    scope = None if current_user.role == 'admin' else current_user.id
    insights = get_smart_insights(scope_user_id=scope)
    today_str = date.today().strftime('%d.%m.%Y')
    return render_template('coach_briefing.html', insights=insights, today=today_str, user=current_user)


# === STRUKTUR-BAUM ===
@app.route('/struktur')
@login_required
def struktur():
    db = get_db()
    if current_user.role == 'admin':
        # Admin: alle Top-Level User (keine Eltern oder Eltern, die nicht in der DB sind)
        top_users = db.execute('SELECT id FROM users WHERE active = 1 AND (parent_id IS NULL OR parent_id NOT IN (SELECT id FROM users WHERE active = 1)) ORDER BY name').fetchall()
        trees = [build_tree(u['id'], db) for u in top_users]
        trees = [t for t in trees if t]
    else:
        trees = [build_tree(current_user.id, db)]
        trees = [t for t in trees if t]
    db.close()
    return render_template('struktur.html', trees=trees, all_levels=CAREER_LEVELS)


@app.route('/profil/photo', methods=['POST'])
@login_required
def profil_photo_upload():
    """Profil-Foto hochladen (max 4 MB, wird quadratisch gecroppt + als JPEG gespeichert)."""
    if 'photo' not in request.files:
        flash('Kein Foto ausgewählt', 'error')
        return redirect(url_for('profil'))
    f = request.files['photo']
    if not f or not f.filename:
        flash('Kein Foto ausgewählt', 'error')
        return redirect(url_for('profil'))
    # Größen-Check (max 4 MB)
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > 4 * 1024 * 1024:
        flash('Datei zu groß (max 4 MB)', 'error')
        return redirect(url_for('profil'))
    try:
        from PIL import Image
        img = Image.open(f.stream)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        # Quadratisch croppen (zentriert, leicht nach oben für Kopf)
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = max(0, (h - side) // 2 - int(side * 0.05))
        img = img.crop((left, top, left + side, top + side))
        # In 2 Größen speichern
        out_dir = os.path.join(app.root_path, 'static', 'avatars')
        os.makedirs(out_dir, exist_ok=True)
        fname = f'user-{current_user.id}.jpg'
        img.resize((400, 400), Image.LANCZOS).save(os.path.join(out_dir, fname), 'JPEG', quality=88, optimize=True)
        img.resize((96, 96), Image.LANCZOS).save(os.path.join(out_dir, f'user-{current_user.id}-96.jpg'), 'JPEG', quality=88, optimize=True)
        rel = f'/static/avatars/{fname}'
        db = get_db()
        db.execute('UPDATE users SET photo_path=? WHERE id=?', (rel, current_user.id))
        db.commit()
        db.close()
        cache_invalidate('ctx:'); cache_invalidate('news:'); cache_invalidate('coach_acts:'); cache_invalidate('forecast:'); cache_invalidate('strang:'); cache_invalidate('adm_pers:'); cache_invalidate('admin_dash:')
        flash('Foto hochgeladen!', 'success')
    except ImportError:
        flash('Foto-Modul nicht installiert (PIL fehlt)', 'error')
    except Exception as e:
        flash(f'Foto-Fehler: {str(e)[:100]}', 'error')
    return redirect(url_for('profil'))


@app.route('/profil/photo/delete', methods=['POST'])
@login_required
def profil_photo_delete():
    db = get_db()
    db.execute('UPDATE users SET photo_path=NULL WHERE id=?', (current_user.id,))
    db.commit()
    db.close()
    # Datei löschen (best effort)
    for s in ['', '-96']:
        p = os.path.join(app.root_path, 'static', 'avatars', f'user-{current_user.id}{s}.jpg')
        try: os.remove(p)
        except Exception: pass
    cache_invalidate('ctx:'); cache_invalidate('news:'); cache_invalidate('coach_acts:'); cache_invalidate('forecast:'); cache_invalidate('strang:'); cache_invalidate('adm_pers:'); cache_invalidate('admin_dash:')
    flash('Foto gelöscht', 'success')
    return redirect(url_for('profil'))


@app.route('/news')
@login_required
def struktur_news_page():
    """Struktur-News + Top-Performer + Live-Feed in einer Seite."""
    days = int(request.args.get('days', 7))
    if days not in (1, 7, 30, 90): days = 7
    news = get_struktur_news(current_user.id, days=days, limit=30)
    db = get_db()
    descendants = [current_user.id] + get_all_descendants(current_user.id)
    ph = ','.join('?' * len(descendants))
    # Top-Performer der gewählten Periode (nach EH)
    top_rows = db.execute(f'''
        SELECT u.id, u.name, u.photo_path, u.manual_career_level,
               COALESCE(SUM(c.einheiten), 0) as eh,
               COUNT(c.id) as vertraege
        FROM users u
        LEFT JOIN contracts c ON c.owner_id = u.id
            AND c.status='abgeschlossen' AND c.recherche_status='freigegeben'
            AND date(c.abschluss_date) >= date('now', '-{days} days')
        WHERE u.id IN ({ph}) AND u.active=1
        GROUP BY u.id
        ORDER BY eh DESC, vertraege DESC
        LIMIT 10
    ''', descendants).fetchall()
    top_list = []
    for r in top_rows:
        d = dict(r)
        d['career'] = career_for_row(r['manual_career_level'], r['eh'])
        d['is_me'] = (r['id'] == current_user.id)
        top_list.append(d)
    # Aktivitäts-Feed — NUR Erfolge der ganzen Struktur (kein Routine-Lärm)
    # Verträge, neue Partner, Aufstiege, Trophäen, Großverträge — keine Logins/Webhooks/Leads
    SUCCESS_TYPES = (
        'vertrag_neu', 'vertrag_freigegeben', 'großvertrag', 'grossvertrag',
        'partner_neu', 'partner_aktiv', 'recruit_gewonnen', 'recruit',
        'stufen_aufstieg', 'aufstieg', 'career_up',
        'streak_milestone', 'streak_record',
        'trophaeen_neu', 'achievement', 'badge_unlocked',
        'top_performer', 'wochenziel_erreicht', 'booking',
    )
    type_placeholders = ','.join('?' * len(SUCCESS_TYPES))
    activity = db.execute(f'''
        SELECT a.*, u.name as user_name, u.photo_path
        FROM activity_log a
        LEFT JOIN users u ON a.user_id = u.id
        WHERE a.user_id IN ({ph})
          AND a.event_type IN ({type_placeholders})
          AND date(a.created_at) >= date('now', '-{days} days')
        ORDER BY a.created_at DESC LIMIT 30
    ''', descendants + list(SUCCESS_TYPES)).fetchall()
    db.close()
    return render_template('news.html', news=news, top=top_list, activity=activity, days=days)


# Error-Handler — keine weißen Seiten mehr
@app.errorhandler(404)
def err_404(e):
    return render_template('error.html', code=404,
        title='Seite nicht gefunden',
        msg='Die Seite die du suchst existiert nicht oder wurde verschoben.'), 404


@app.errorhandler(500)
def err_500(e):
    try:
        import traceback
        print(f'[ERROR 500] {request.path}: {traceback.format_exc()}')
    except Exception:
        pass
    return render_template('error.html', code=500,
        title='Etwas ist schief gelaufen',
        msg='Ein interner Fehler. Versuch es nochmal — falls weiterhin Probleme, sag deinem Admin Bescheid.'), 500


@app.errorhandler(403)
def err_403(e):
    return render_template('error.html', code=403,
        title='Keine Berechtigung',
        msg='Für diese Seite hast du keine Berechtigung. Frag deinen Admin oder Strukturhöher.'), 403


@app.route('/registrieren', methods=['GET', 'POST'])
def self_register():
    """Self-Registration: Neue Geschäftspartner können sich selbst anmelden.
    Strukturhöher-Name als Pflichtfeld → Account ist pending bis bestätigt."""
    db = get_db()
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        phone = (request.form.get('phone') or '').strip()
        birthday = request.form.get('birthday') or None
        strukt_name = (request.form.get('strukturhoeher_name') or '').strip()
        pw = (request.form.get('password') or '').strip()
        pw2 = (request.form.get('password2') or '').strip()

        # Validation
        errors = []
        if len(name) < 3: errors.append('Name zu kurz')
        if '@' not in email or len(email) < 5: errors.append('E-Mail ungültig')
        if not strukt_name: errors.append('Name deines Strukturhöheren fehlt')
        if len(pw) < 6: errors.append('Passwort zu kurz (min 6 Zeichen)')
        if pw != pw2: errors.append('Passwörter stimmen nicht überein')
        existing = db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
        if existing: errors.append('E-Mail ist schon registriert')

        # Strukturhöher suchen (Name oder E-Mail)
        parent = db.execute(
            'SELECT id, name, email, role FROM users WHERE active=1 AND (LOWER(name)=LOWER(?) OR LOWER(email)=LOWER(?)) LIMIT 1',
            (strukt_name, strukt_name)).fetchone()
        if not parent:
            errors.append(f'Strukturhöher „{strukt_name}" nicht gefunden — frag deinen Mentor nach dem genauen Namen oder E-Mail')

        if errors:
            db.close()
            for e in errors: flash(e, 'error')
            return render_template('self_register.html', form=request.form)

        # Account anlegen — pending bis Strukturhöher bestätigt
        cur = db.execute('''INSERT INTO users
            (name, email, password, role, parent_id, level, phone, birthday,
             manual_career_level, must_change_password, active)
            VALUES (?, ?, ?, 'partner', ?, ?, ?, ?, 1, 0, 0)''',
            (name, email, hash_password(pw), parent['id'],
             1, phone or None, birthday, ))
        new_id = cur.lastrowid
        db.commit()
        db.close()
        log_activity(parent['id'], 'registrierung_pending',
            f'{name} möchte deine Downline werden — bitte bestätigen',
            icon='◎', color='gold')
        # Push an Strukturhöher
        try:
            send_push_to_user(parent['id'],
                title=f'◎ Neue Anmeldung: {name}',
                body=f'{name} möchte in deine Struktur — jetzt bestätigen oder ablehnen.',
                url='/genehmigungen', urgent=True, tag='registrierung')
        except Exception:
            pass
        flash(f'Anmeldung erfolgreich! {parent["name"]} muss dich noch bestätigen — du wirst per E-Mail informiert sobald aktiv.', 'success')
        return redirect(url_for('login'))

    db.close()
    return render_template('self_register.html', form={})


@app.route('/genehmigungen')
@login_required
def genehmigungen_personal():
    """Personal-Approval: Strukturhöhere ab Stufe 2 können neue Registrierungen in ihrer Downline bestätigen."""
    db = get_db()
    own_eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE owner_id=? AND status="abgeschlossen" AND recherche_status="freigegeben"', (current_user.id,)).fetchone()['s']
    user = db.execute('SELECT manual_career_level, role FROM users WHERE id=?', (current_user.id,)).fetchone()
    own_career = career_for_row(user['manual_career_level'], own_eh + (db.execute('SELECT initial_eh FROM users WHERE id=?', (current_user.id,)).fetchone()['initial_eh'] or 0))
    is_eligible = current_user.has_admin_access or own_career['level'] >= 2
    if not is_eligible:
        db.close()
        flash('Stufe 2 (LREP) oder höher erforderlich', 'error')
        return redirect(url_for('dashboard'))

    # Nur direkt unter mir oder in meiner Downline
    downline = [current_user.id] + get_all_descendants(current_user.id)
    ph = ','.join('?' * len(downline))
    if current_user.has_admin_access:
        # Admin sieht alle pending
        pending = db.execute("""
            SELECT u.*, p.name as parent_name FROM users u
            LEFT JOIN users p ON u.parent_id = p.id
            WHERE u.active=0 ORDER BY u.id DESC""").fetchall()
    else:
        pending = db.execute(f"""
            SELECT u.*, p.name as parent_name FROM users u
            LEFT JOIN users p ON u.parent_id = p.id
            WHERE u.active=0 AND u.parent_id IN ({ph}) ORDER BY u.id DESC""", downline).fetchall()
    db.close()
    return render_template('genehmigungen_account.html', pending=pending, is_admin=current_user.has_admin_access)


@app.route('/genehmigungen/<int:uid>/bestaetigen', methods=['POST'])
@login_required
def genehmigung_bestaetigen(uid):
    db = get_db()
    target = db.execute('SELECT * FROM users WHERE id=? AND active=0', (uid,)).fetchone()
    if not target:
        db.close()
        return redirect(url_for('genehmigungen_personal'))
    # Berechtigung: Admin oder Eltern-Kette
    descendants = [current_user.id] + get_all_descendants(current_user.id)
    if not (current_user.has_admin_access or target['parent_id'] in descendants or target['parent_id'] == current_user.id):
        db.close()
        flash('Keine Berechtigung', 'error')
        return redirect(url_for('genehmigungen_personal'))
    db.execute('UPDATE users SET active=1 WHERE id=?', (uid,))
    db.commit()
    db.close()
    log_activity(uid, 'partner_neu', f'{target["name"]} ist freigeschaltet als neuer Geschäftspartner', icon='●', color='green')
    # Push an den frisch freigeschalteten User
    try:
        send_push_to_user(uid,
            title=f'✓ Willkommen in der Struktur!',
            body='Du bist jetzt freigeschaltet. Log dich ein und leg los!',
            url='/dashboard', urgent=True, tag='aktiviert')
    except Exception:
        pass
    # Bestätigungs-Mail an den freigeschalteten User (jetzt erlaubt — bei Aktivierung)
    if target.get('email') and is_smtp_configured():
        try:
            base = (request.url_root or 'https://proacademy-business.de/').rstrip('/')
            text = (f"Hallo {target['name']},\n\nherzlich willkommen in unserer Struktur!\n"
                    f"Dein Account wurde gerade freigeschaltet — du kannst dich ab sofort einloggen:\n\n{base}/login\n\n"
                    f"Falls du dein Passwort nicht mehr weißt: {base}/passwort-vergessen\n\n"
                    f"Bis bald,\nDein Pro-Academy-Team")
            html = (f'<p>Hallo {target["name"]},</p><p>herzlich willkommen in unserer Struktur!</p>'
                    f'<p>Dein Account wurde freigeschaltet — du kannst dich ab sofort einloggen:</p>'
                    f'<p><a href="{base}/login" style="background:#d4a843;color:#0f1c3f;padding:12px 22px;'
                    f'border-radius:8px;text-decoration:none;font-weight:800;display:inline-block">→ Jetzt einloggen</a></p>'
                    f'<p style="color:#64748b;font-size:12px;margin-top:24px">Passwort vergessen? <a href="{base}/passwort-vergessen">Hier zurücksetzen</a></p>')
            send_email(target['email'], 'Willkommen bei Pro Academy — Account freigeschaltet',
                       text, body_html=html, sent_by=current_user.id, category='signup')
        except Exception as e:
            print(f'[bestaetigen-mail] {e}')
    flash(f'{target["name"]} freigeschaltet!', 'success')
    return redirect(url_for('genehmigungen_personal'))


@app.route('/genehmigungen/<int:uid>/ablehnen', methods=['POST'])
@login_required
def genehmigung_ablehnen(uid):
    db = get_db()
    target = db.execute('SELECT * FROM users WHERE id=? AND active=0', (uid,)).fetchone()
    if not target:
        db.close()
        return redirect(url_for('genehmigungen_personal'))
    descendants = [current_user.id] + get_all_descendants(current_user.id)
    if not (current_user.has_admin_access or target['parent_id'] in descendants or target['parent_id'] == current_user.id):
        db.close()
        flash('Keine Berechtigung', 'error')
        return redirect(url_for('genehmigungen_personal'))
    db.execute('DELETE FROM users WHERE id=?', (uid,))
    db.commit()
    db.close()
    flash(f'Anmeldung von {target["name"]} abgelehnt + gelöscht.', 'info')
    return redirect(url_for('genehmigungen_personal'))


@app.route('/onboarding/catchup', methods=['GET', 'POST'])
@login_required
def onboarding_catchup():
    """Catch-Up-Wizard: aktueller Einheitenstand + Platzhalter-Strukturen.
    Wird beim ersten Login allen Partnern gezeigt die catchup_done=0 haben."""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (current_user.id,)).fetchone()
    if request.method == 'POST':
        # Action: skip oder save
        action = request.form.get('action', 'save')
        if action == 'save':
            try:
                eh = float(request.form.get('current_eh', 0) or 0)
            except (ValueError, TypeError):
                eh = 0
            # Initial-EH setzen (überschreibt vorhandenen Wert)
            db.execute('UPDATE users SET initial_eh=?, catchup_done=1 WHERE id=?', (eh, current_user.id))
            # Alte Platzhalter-Strukturen löschen
            db.execute('DELETE FROM placeholder_structures WHERE owner_id=? AND linked_user_id IS NULL',
                       (current_user.id,))
            # Bis zu 6 Strukturen einlesen
            for i in range(1, 7):
                name = (request.form.get(f'struct_name_{i}') or '').strip()
                if not name:
                    continue
                try:
                    est_eh = float(request.form.get(f'struct_eh_{i}', 0) or 0)
                except (ValueError, TypeError):
                    est_eh = 0
                try:
                    p_cnt = int(request.form.get(f'struct_partner_count_{i}', 0) or 0)
                except (ValueError, TypeError):
                    p_cnt = 0
                notes = (request.form.get(f'struct_notes_{i}') or '').strip()
                db.execute('''INSERT INTO placeholder_structures
                              (owner_id, name, est_eh, partner_count, notes)
                              VALUES (?, ?, ?, ?, ?)''',
                           (current_user.id, name, est_eh, p_cnt, notes or None))
            db.commit()
            recalculate_all_commissions()
            cache_invalidate('ctx:'); cache_invalidate('news:'); cache_invalidate('coach_acts:'); cache_invalidate('forecast:'); cache_invalidate('strang:'); cache_invalidate('adm_pers:'); cache_invalidate('admin_dash:')
            log_activity(current_user.id, 'catchup_done',
                f'{current_user.name} hat seinen Stand eingetragen ({eh:.0f} EH + Strukturen)',
                icon='📊', color='gold')
            flash(f'Top — {eh:.0f} EH gespeichert. Du kannst jederzeit unter Einstellungen anpassen.', 'success')
        else:
            # Skip — markiere done damit nicht mehr nervt, aber speichere nichts
            db.execute('UPDATE users SET catchup_done=1 WHERE id=?', (current_user.id,))
            db.commit()
            flash('Ok — kannst du jederzeit später unter "Mein Profil" eintragen.', 'info')
        db.close()
        return redirect(url_for('dashboard'))

    # GET — Wizard zeigen
    existing_structs = db.execute('SELECT * FROM placeholder_structures WHERE owner_id=? AND linked_user_id IS NULL ORDER BY id',
                                  (current_user.id,)).fetchall()
    db.close()
    return render_template('onboarding_catchup.html',
                           current_eh=user['initial_eh'] or 0,
                           existing_structs=existing_structs)


def needs_catchup(user_id):
    """User braucht Catch-Up wenn catchup_done=0 und nicht Admin."""
    db = get_db()
    try:
        row = db.execute('SELECT catchup_done, role FROM users WHERE id=?', (user_id,)).fetchone()
    except Exception:
        db.close()
        return False  # Falls Spalte fehlt → safe default
    db.close()
    if not row or row['catchup_done']:
        return False
    if row['role'] == 'admin':
        return False
    return True


@app.route('/tracking')
@login_required
def tracking():
    """Funnel-Tracking für Vertrieb (VK) und Recruiting (RK)."""
    db = get_db()
    scope = request.args.get('scope', 'me')
    if scope == 'team' and (current_user.has_admin_access or
                             db.execute('SELECT COUNT(*) as c FROM users WHERE parent_id=? AND active=1', (current_user.id,)).fetchone()['c'] > 0):
        ids = [current_user.id] + get_all_descendants(current_user.id)
    else:
        scope = 'me'
        ids = [current_user.id]
    placeholders = ','.join('?' * len(ids))

    def funnel_for(typ):
        if typ == 'rk':
            cond = "liste_typ='rk'"
        else:
            cond = "COALESCE(liste_typ,'vk')='vk'"
        total = db.execute(f"SELECT COUNT(*) as c FROM leads WHERE owner_id IN ({placeholders}) AND {cond}", ids).fetchone()['c']
        contacted = db.execute(f"SELECT COUNT(*) as c FROM leads WHERE owner_id IN ({placeholders}) AND {cond} AND status NOT IN ('neu')", ids).fetchone()['c']
        angebot = db.execute(f"SELECT COUNT(*) as c FROM leads WHERE owner_id IN ({placeholders}) AND {cond} AND status IN ('angebot','rekrutierung','gewonnen','abgeschlossen')", ids).fetchone()['c']
        won = db.execute(f"SELECT COUNT(*) as c FROM leads WHERE owner_id IN ({placeholders}) AND {cond} AND status IN ('gewonnen','abgeschlossen')", ids).fetchone()['c']
        lost = db.execute(f"SELECT COUNT(*) as c FROM leads WHERE owner_id IN ({placeholders}) AND {cond} AND status='verloren'", ids).fetchone()['c']
        new_30d = db.execute(f"SELECT COUNT(*) as c FROM leads WHERE owner_id IN ({placeholders}) AND {cond} AND date(created_at) >= date('now','-30 days')", ids).fetchone()['c']
        won_30d = db.execute(f"SELECT COUNT(*) as c FROM leads WHERE owner_id IN ({placeholders}) AND {cond} AND status IN ('gewonnen','abgeschlossen') AND date(updated_at) >= date('now','-30 days')", ids).fetchone()['c']
        return {
            'total': total, 'contacted': contacted, 'angebot': angebot, 'won': won, 'lost': lost,
            'new_30d': new_30d, 'won_30d': won_30d,
            'contact_pct': round(contacted / total * 100, 1) if total else 0,
            'angebot_pct': round(angebot / max(contacted, 1) * 100, 1),
            'won_pct': round(won / max(angebot, 1) * 100, 1),
            'overall_pct': round(won / total * 100, 1) if total else 0,
        }

    vk = funnel_for('vk')
    rk = funnel_for('rk')

    # Termine→Abschluss-Quote
    term_done_60 = db.execute(f"SELECT COUNT(*) as c FROM appointments WHERE owner_id IN ({placeholders}) AND status='erledigt' AND date(termin_date) >= date('now','-60 days')", ids).fetchone()['c']
    contracts_won_60 = db.execute(f"SELECT COUNT(*) as c FROM contracts WHERE owner_id IN ({placeholders}) AND status='abgeschlossen' AND date(abschluss_date) >= date('now','-60 days')", ids).fetchone()['c']
    termin_quote = round(term_done_60 / max(contracts_won_60, 1), 1)

    # Recruiting → echte Partner-Anlage (60 Tage)
    new_partners_60 = db.execute(f"SELECT COUNT(*) as c FROM users WHERE parent_id IN ({placeholders}) AND active=1 AND date(last_login) >= date('now','-60 days')", ids).fetchone()['c'] if False else 0
    # einfacher: alle direkten Partner zählen
    direct_total = db.execute(f"SELECT COUNT(*) as c FROM users WHERE parent_id IN ({placeholders}) AND active=1", ids).fetchone()['c']

    db.close()
    return render_template('tracking.html',
        vk=vk, rk=rk, scope=scope,
        termin_quote=termin_quote, term_done_60=term_done_60, contracts_won_60=contracts_won_60,
        direct_total=direct_total
    )


@app.route('/team-kalender')
@login_required
def team_kalender():
    """Team-Kalender mit Strang-Übersicht-Default.
    - Wenn user direkte Geschäftspartner UNTER sich hat (Admin/HREP+) → Strang-Kachel-Übersicht
    - ?root=<id>: Einzel-Kalender für einen Strang
    - ?partner=<id>: Filter auf einen Partner innerhalb eines Strangs"""
    root_param = request.args.get('root')

    # ─── Übersichts-Mode (Default für User mit direkten Partnern) ───
    if not root_param:
        db = get_db()
        # Direkte Geschäftspartner unter current_user (= ein "Strang" pro Partner)
        direct_partners = db.execute('''
            SELECT id, name, photo_path, manual_career_level
            FROM users WHERE parent_id = ? AND active = 1
            ORDER BY name
        ''', (current_user.id,)).fetchall()
        if not direct_partners:
            # Keine direkten Partner → klassischer Single-Kalender via Calendar-Root
            root = get_team_calendar_root(current_user.id)
            if not root:
                flash('Team-Kalender ist ab HREP-Stufe verfügbar — du oder ein Upline-Partner muss HREP oder höher sein.', 'info')
                db.close()
                return redirect(url_for('dashboard'))
            db.close()
            # Fall-through zu Single-Kalender mit eigenem root
            root_param = str(root['id'])
        else:
            # Übersicht: pro Rolle unterschiedlich
            today = date.today()
            in_14d = today + timedelta(days=14)
            strange = []
            def _slot_stats(ids_list):
                ph_l = ','.join('?' * len(ids_list))
                upc = db.execute(f'''
                    SELECT a.title, a.client_name, a.termin_date, a.termin_time, a.status, u.name as owner_name
                    FROM appointments a JOIN users u ON a.owner_id = u.id
                    WHERE a.owner_id IN ({ph_l}) AND date(a.termin_date) BETWEEN date(?) AND date(?)
                    ORDER BY a.termin_date, a.termin_time LIMIT 5
                ''', ids_list + [today.isoformat(), in_14d.isoformat()]).fetchall()
                t = db.execute(f"SELECT COUNT(*) c FROM appointments WHERE owner_id IN ({ph_l}) AND date(termin_date)=date(?)",
                               ids_list + [today.isoformat()]).fetchone()['c']
                w = db.execute(f"SELECT COUNT(*) c FROM appointments WHERE owner_id IN ({ph_l}) AND date(termin_date) BETWEEN date(?) AND date(?)",
                               ids_list + [today.isoformat(), in_14d.isoformat()]).fetchone()['c']
                return t, w, [dict(u) for u in upc]

            me_row = db.execute('SELECT parent_id, photo_path FROM users WHERE id=?', (current_user.id,)).fetchone()
            is_top_admin = current_user.has_admin_access

            if is_top_admin:
                # ════════ ADMIN-VIEW (Najib) ════════
                # Eine Kachel pro direktem Geschäftspartner — jede mit komplettem Strang-Kalender
                # Plus: Mein Kalender oben + Alle Termine global
                mt, mw, mu = _slot_stats([current_user.id])
                strange.append({
                    'id': current_user.id, 'name': f'Mein Kalender · {current_user.name}',
                    'photo_path': me_row['photo_path'] if me_row else None,
                    'level': 99, 'team_size': 1,
                    'count_today': mt, 'count_week': mw, 'upcoming': mu,
                    'is_mine': True,
                })
                all_ids = [current_user.id] + get_all_descendants(current_user.id)
                global_ids = [r['id'] for r in db.execute('SELECT id FROM users WHERE active=1').fetchall()]
                if set(global_ids) != set(all_ids):
                    gt, gw, gu = _slot_stats(global_ids)
                    strange.append({
                        'id': 'global', 'name': 'Alle Termine · gesamtes System',
                        'photo_path': None, 'level': 99, 'team_size': len(global_ids),
                        'count_today': gt, 'count_week': gw, 'upcoming': gu,
                        'is_global': True,
                    })
                # Pro direktem Partner ein Strang-Slot mit dessen kompletter Downline
                # Jeder Strang hat seine EIGENE Farbe (deterministisch via partner_id)
                for p in direct_partners:
                    p_ids = [p['id']] + get_all_descendants(p['id'])
                    pt, pw, pu = _slot_stats(p_ids)
                    strange.append({
                        'id': p['id'], 'name': p['name'],
                        'photo_path': p['photo_path'],
                        'level': p['manual_career_level'] or 1,
                        'team_size': len(p_ids),
                        'count_today': pt, 'count_week': pw, 'upcoming': pu,
                        'is_partner': True,
                        'strang_color': strang_color(p['id']),
                    })
            else:
                # ════════ DOWNLINE-VIEW (alle außer Najib) ════════
                # Genau EIN Kombi-Kalender: NUR eigene + eigene Downline
                # Upline (parent) wird NICHT mit dazu gezogen — Privacy-Schutz!
                # Strukturleiter-Slot unten zeigt 1:1-Termine mit Upline separat.
                team_ids = [current_user.id] + get_all_descendants(current_user.id)
                tt, tw, tu = _slot_stats(team_ids)
                strange.append({
                    'id': 'all', 'name': 'Team-Kalender · meine Struktur',
                    'photo_path': None, 'level': 99, 'team_size': len(team_ids),
                    'count_today': tt, 'count_week': tw, 'upcoming': tu,
                    'is_all': True,
                })
                # Strukturleiter-Slot (Upline) — UNTEN
                # Booking-View: zeigt eigene 1:1 mit Detail + fremde als anonymes "Belegt"
                # Downliner kann hier Termin mit Mentor anfragen
                if me_row and me_row['parent_id']:
                    mentor = db.execute('SELECT id, name, photo_path, manual_career_level FROM users WHERE id=? AND active=1',
                                       (me_row['parent_id'],)).fetchone()
                    if mentor:
                        # Eigene 1:1-Termine (mit Detail) zählen für upcoming-Preview
                        all_mentor = db.execute('''SELECT id, title, client_name, termin_date, termin_time, status, attendee_ids
                                                   FROM appointments WHERE owner_id=? AND date(termin_date) BETWEEN date(?) AND date(?)
                                                   ORDER BY termin_date, termin_time''',
                                              (mentor['id'], today.isoformat(), in_14d.isoformat())).fetchall()
                        viewer_name_lower = current_user.name.lower() if current_user.name else ''
                        viewer_first = (current_user.name.split()[0].lower() if current_user.name else '')
                        sl_u = []
                        sl_t_with_me = 0
                        sl_w_with_me = 0
                        today_iso = today.isoformat()
                        for a in all_mentor:
                            is_with_me = False
                            if a['attendee_ids']:
                                try:
                                    atts = [int(x) for x in json.loads(a['attendee_ids']) if str(x).isdigit()]
                                    if current_user.id in atts:
                                        is_with_me = True
                                except Exception:
                                    pass
                            if not is_with_me and a['client_name']:
                                cn = a['client_name'].lower()
                                if viewer_name_lower and viewer_name_lower in cn:
                                    is_with_me = True
                                elif viewer_first and len(viewer_first) > 2 and viewer_first in cn:
                                    is_with_me = True
                            if is_with_me:
                                d = dict(a); d['owner_name'] = mentor['name']
                                if len(sl_u) < 5:
                                    sl_u.append(d)
                                sl_w_with_me += 1
                                if a['termin_date'] == today_iso:
                                    sl_t_with_me += 1
                        strange.append({
                            'id': mentor['id'], 'name': f'Strukturleiter · {mentor["name"]}',
                            'photo_path': mentor['photo_path'],
                            'level': mentor['manual_career_level'] or 1,
                            'team_size': 1,
                            'count_today': sl_t_with_me, 'count_week': sl_w_with_me, 'upcoming': sl_u,
                            'is_mentor': True,
                            'mentor_id': mentor['id'],  # für Booking-Button
                            'total_in_period': len(all_mentor),  # gesamt-belegung im 14T-Fenster
                        })
            db.close()
            return render_template('team_kalender_uebersicht.html',
                strange=strange, today_iso=today.isoformat())

    # ─── Single-Strang-Mode (mit ?root=<id>) ───
    # Spezialfall: ?root=all → eigene Struktur (du + Downline)
    # Spezialfall: ?root=global → alle aktiven User (nur Admin)
    if root_param == 'all':
        root = {'id': current_user.id, 'name': '👥 Team-Kalender · alle Termine deiner Struktur',
                'level': 99, 'short': 'TEAM'}
    elif root_param == 'global':
        if not current_user.has_admin_access:
            flash('Globaler Kalender nur für Admins.', 'error')
            return redirect(url_for('team_kalender'))
        root = {'id': '__global__', 'name': '🌐 Alle Termine · gesamtes System',
                'level': 99, 'short': 'GLOBAL'}
    elif root_param and root_param.isdigit():
        target_id = int(root_param)
        # Berechtigung: Admin · selber · Downline · ODER direkter Mentor (Upline read-only)
        descendants = get_all_descendants(current_user.id)
        # Direkter Mentor (parent_id) — read-only Mentor-Slot-Lookup erlaubt
        db_p = get_db()
        me_row = db_p.execute('SELECT parent_id FROM users WHERE id=?', (current_user.id,)).fetchone()
        db_p.close()
        is_my_mentor = bool(me_row and me_row['parent_id'] == target_id)
        if not (current_user.has_admin_access or target_id == current_user.id or target_id in descendants or is_my_mentor):
            flash('Keine Berechtigung für diesen Sub-Kalender.', 'error')
            return redirect(url_for('team_kalender'))
        db = get_db()
        target_user = db.execute('SELECT * FROM users WHERE id=? AND active=1', (target_id,)).fetchone()
        db.close()
        if not target_user:
            return redirect(url_for('team_kalender'))
        root = {'id': target_id, 'name': target_user['name'], 'level': target_user['manual_career_level'] or 1, 'short': 'TEAM'}
    else:
        root = get_team_calendar_root(current_user.id)
        if not root:
            flash('Team-Kalender ist ab HREP-Stufe verfügbar — du oder ein Upline-Partner muss HREP oder höher sein.', 'info')
            return redirect(url_for('dashboard'))

    today = date.today()
    try:
        year = int(request.args.get('year', today.year))
        month = int(request.args.get('month', today.month))
    except (ValueError, TypeError):
        year, month = today.year, today.month
    if month < 1: month, year = 12, year - 1
    if month > 12: month, year = 1, year + 1

    # Mono-Color: wenn current_user die Upline ist (Najib öffnet Niesa's Strang),
    # bekommen alle Termine in dem Strang DIE EINE Farbe der direkten Struktur
    mono = None
    if isinstance(root['id'], int):
        db_chk = get_db()
        target_row = db_chk.execute('SELECT parent_id FROM users WHERE id=?', (root['id'],)).fetchone()
        db_chk.close()
        # current_user ist parent von root → Najib-Sicht auf eine direkte Struktur
        if target_row and target_row['parent_id'] == current_user.id and root['id'] != current_user.id:
            mono = strang_color(root['id'])
    data = get_team_calendar_data(root['id'], year, month, mono_color=mono)
    # Booking-View: wenn current_user den Strukturleiter (parent) anschaut UND kein Admin ist,
    # eigene 1:1-Termine voll sichtbar — fremde als anonymes „Belegt" maskieren
    # (Downliner sieht Verfügbarkeit, kann freie Slots zum Buchen erkennen)
    is_mentor_view = False
    if isinstance(root['id'], int) and not current_user.has_admin_access:
        db_p2 = get_db()
        me_check = db_p2.execute('SELECT parent_id FROM users WHERE id=?', (current_user.id,)).fetchone()
        db_p2.close()
        if me_check and me_check['parent_id'] == root['id'] and root['id'] != current_user.id:
            is_mentor_view = True
            viewer_name_l = (current_user.name or '').lower()
            viewer_first = ((current_user.name or '').split()[0].lower() if current_user.name else '')
            masked = []
            for a in data['appointments']:
                hit = False
                if a.get('attendee_ids'):
                    try:
                        atts = [int(x) for x in json.loads(a['attendee_ids']) if str(x).isdigit()]
                        if current_user.id in atts:
                            hit = True
                    except Exception:
                        pass
                if not hit and a.get('client_name'):
                    cn = a['client_name'].lower()
                    if viewer_name_l and viewer_name_l in cn:
                        hit = True
                    elif viewer_first and len(viewer_first) > 2 and viewer_first in cn:
                        hit = True
                if hit:
                    masked.append(a)  # eigener 1:1: voll sichtbar
                else:
                    # Maskiere: nur Zeit-Block, kein Detail
                    a2 = dict(a)
                    a2['title'] = 'Belegt'
                    a2['client_name'] = None
                    a2['notizen'] = None
                    a2['_masked'] = True
                    masked.append(a2)
            data['appointments'] = masked
    # Filter nach einem Partner
    partner_filter = request.args.get('partner')
    if partner_filter and partner_filter.isdigit():
        pid = int(partner_filter)
        data['appointments'] = [a for a in data['appointments'] if a['owner_id'] == pid]
    # Navigation
    prev_month, prev_year = (12, year - 1) if month == 1 else (month - 1, year)
    next_month, next_year = (1, year + 1) if month == 12 else (month + 1, year)
    monat_namen = ['', 'Januar', 'Februar', 'März', 'April', 'Mai', 'Juni',
                   'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember']
    return render_template('team_kalender.html',
        root=root, year=year, month=month, monat_label=monat_namen[month],
        appointments=data['appointments'], members=data['members'],
        prev_year=prev_year, prev_month=prev_month,
        next_year=next_year, next_month=next_month,
        today_iso=today.isoformat(),
        partner_filter=int(partner_filter) if partner_filter and partner_filter.isdigit() else None,
        is_sub_root=bool(root_param)
    )


@app.route('/team-kalender/tag/<datestr>')
@login_required
def team_kalender_tag(datestr):
    """Tages-Detail-Ansicht mit Stunden-Timeline."""
    try:
        d = datetime.strptime(datestr, '%Y-%m-%d').date()
    except ValueError:
        return redirect(url_for('team_kalender'))
    root_param = request.args.get('root')
    if root_param == 'global':
        if not current_user.has_admin_access:
            return redirect(url_for('team_kalender'))
        root = {'id': '__global__', 'name': '🌐 Alle Termine · gesamtes System', 'level': 99, 'short': 'GLOBAL'}
    elif root_param == 'all':
        root = {'id': current_user.id, 'name': '👥 Team-Kalender', 'level': 99, 'short': 'TEAM'}
    elif root_param and root_param.isdigit():
        target_id = int(root_param)
        descendants = get_all_descendants(current_user.id)
        if not (current_user.has_admin_access or target_id == current_user.id or target_id in descendants):
            return redirect(url_for('team_kalender'))
        db = get_db()
        target_user = db.execute('SELECT * FROM users WHERE id=? AND active=1', (target_id,)).fetchone()
        db.close()
        root = {'id': target_id, 'name': target_user['name'], 'level': 1, 'short': 'TEAM'} if target_user else None
    else:
        root = get_team_calendar_root(current_user.id)
    if not root:
        return redirect(url_for('dashboard'))

    db = get_db()
    if root['id'] == '__global__':
        ids = [r['id'] for r in db.execute('SELECT id FROM users WHERE active=1').fetchall()]
    else:
        ids = [root['id']] + get_all_descendants(root['id'])
    placeholders = ','.join('?' * len(ids))
    members = db.execute(f'SELECT id, name, photo_path FROM users WHERE id IN ({placeholders}) AND active=1 ORDER BY name', ids).fetchall()
    # Mono-Color für Tagesansicht: Najib öffnet Niesa-Strang → alle Termine in EINER Farbe
    mono_day = None
    if isinstance(root['id'], int):
        target_row = db.execute('SELECT parent_id FROM users WHERE id=?', (root['id'],)).fetchone()
        if target_row and target_row['parent_id'] == current_user.id and root['id'] != current_user.id:
            mono_day = strang_color(root['id'])
    if mono_day:
        color_map = {m['id']: mono_day for m in members}
    else:
        color_map = {m['id']: _CALENDAR_COLORS[i % len(_CALENDAR_COLORS)] for i, m in enumerate(members)}
    photo_map = {m['id']: m['photo_path'] for m in members}
    members_list = [{'id': m['id'], 'name': m['name'], 'color': color_map[m['id']], 'photo': photo_map.get(m['id'])} for m in members]

    appts = db.execute(f'''SELECT a.*, u.name as owner_name, u.photo_path as owner_photo
                            FROM appointments a JOIN users u ON a.owner_id=u.id
                            WHERE a.owner_id IN ({placeholders}) AND date(a.termin_date)=?
                            ORDER BY a.termin_time NULLS FIRST, a.id''', ids + [datestr]).fetchall()
    # Privacy-Check: Downliner schaut auf Mentor (parent_id) → nur 1:1 mit ihm sichtbar
    is_mentor_view_day = False
    if isinstance(root['id'], int) and not current_user.has_admin_access:
        me_check_d = db.execute('SELECT parent_id FROM users WHERE id=?', (current_user.id,)).fetchone()
        if me_check_d and me_check_d['parent_id'] == root['id'] and root['id'] != current_user.id:
            is_mentor_view_day = True
    viewer_name_lower = (current_user.name or '').lower() if is_mentor_view_day else ''
    viewer_first_name = ((current_user.name or '').split()[0].lower() if current_user.name else '') if is_mentor_view_day else ''

    appts_list = []
    for a in appts:
        d2 = dict(a)
        d2['color'] = color_map.get(a['owner_id'], '#94a3b8')
        # Mentor-Privacy: maskiere fremde Termine wenn Downliner den Mentor anschaut
        if is_mentor_view_day:
            is_with_me = False
            if a['attendee_ids']:
                try:
                    atts = [int(x) for x in json.loads(a['attendee_ids']) if str(x).isdigit()]
                    if current_user.id in atts:
                        is_with_me = True
                except Exception:
                    pass
            if not is_with_me and a['client_name']:
                cn = a['client_name'].lower()
                if viewer_name_lower and viewer_name_lower in cn:
                    is_with_me = True
                elif viewer_first_name and len(viewer_first_name) > 2 and viewer_first_name in cn:
                    is_with_me = True
            if not is_with_me:
                d2['title'] = 'Belegt'
                d2['client_name'] = None
                d2['notizen'] = None
                d2['_masked'] = True
        # Attendees auflösen (für maskierte Termine: nicht zeigen)
        d2['attendees'] = []
        if not d2.get('_masked'):
            try:
                if a['attendee_ids']:
                    att_ids = [int(x) for x in json.loads(a['attendee_ids']) if str(x).isdigit()]
                    if att_ids:
                        aph = ','.join('?' * len(att_ids))
                        arows = db.execute(f'SELECT id, name FROM users WHERE id IN ({aph})', att_ids).fetchall()
                        for ar in arows:
                            d2['attendees'].append({'id': ar['id'], 'name': ar['name'], 'color': color_map.get(ar['id'], '#94a3b8')})
            except Exception:
                pass
        appts_list.append(d2)
    db.close()

    # Vor/Zurück
    prev_d = (d - timedelta(days=1)).isoformat()
    next_d = (d + timedelta(days=1)).isoformat()
    return render_template('team_kalender_tag.html',
        root=root, the_date=d, datestr=datestr,
        appointments=appts_list, members=members_list,
        prev_d=prev_d, next_d=next_d,
        is_sub_root=bool(root_param),
        today_iso=date.today().isoformat()
    )


@app.route('/team-kalender/quick-add', methods=['POST'])
@login_required
def team_kalender_quick_add():
    """Quick-Add Termin direkt aus dem Kalender — mit Multi-Partner-Attendees."""
    title = (request.form.get('title') or '').strip()
    termin_date = (request.form.get('termin_date') or '').strip()
    termin_time = (request.form.get('termin_time') or '').strip()
    typ = (request.form.get('typ') or 'kundentermin').strip()
    client_name = (request.form.get('client_name') or '').strip()
    try:
        duration = int(request.form.get('duration_min', 60) or 60)
    except (ValueError, TypeError):
        duration = 60
    # Multi-Partner: form sendet attendee_ids als kommaseparierte Liste oder als getlist
    attendee_raw = request.form.getlist('attendee_ids') or []
    if not attendee_raw and request.form.get('attendee_ids_csv'):
        attendee_raw = [x.strip() for x in request.form.get('attendee_ids_csv', '').split(',') if x.strip()]
    attendee_ids_list = [int(x) for x in attendee_raw if str(x).isdigit()]
    attendee_json = json.dumps(attendee_ids_list) if attendee_ids_list else None

    if not title or not termin_date:
        flash('Titel und Datum sind Pflicht.', 'error')
        return redirect(url_for('team_kalender'))
    db = get_db()
    db.execute('''INSERT INTO appointments
                  (owner_id, title, client_name, termin_date, termin_time, typ, status, attendee_ids, duration_min)
                  VALUES (?, ?, ?, ?, ?, ?, 'geplant', ?, ?)''',
               (current_user.id, title, client_name or None, termin_date,
                termin_time or None, typ, attendee_json, duration))
    db.commit()
    db.close()
    log_activity(current_user.id, 'termin_neu', f'{current_user.name} hat Termin „{title}" angelegt',
                 icon='📅', color='blue')
    flash(f'Termin „{title}" am {termin_date} angelegt.', 'success')
    # Bei Tages-Ansicht zurück zum Tag, sonst zum Monat
    if request.form.get('return_to') == 'day':
        return redirect(url_for('team_kalender_tag', datestr=termin_date))
    try:
        d = datetime.strptime(termin_date, '%Y-%m-%d').date()
        return redirect(url_for('team_kalender', year=d.year, month=d.month))
    except Exception:
        return redirect(url_for('team_kalender'))


@app.route('/vorschlag', methods=['GET', 'POST'])
@login_required
def vorschlag():
    """Partner können Vorschläge an den Admin schicken."""
    if request.method == 'POST':
        kategorie = request.form.get('kategorie', 'sonstiges').strip()
        titel = (request.form.get('titel') or '').strip()
        details = (request.form.get('details') or '').strip()
        if not titel or len(titel) > 200:
            flash('Bitte einen Titel angeben.', 'error')
            return redirect(url_for('vorschlag'))
        db = get_db()
        db.execute('INSERT INTO partner_suggestions (user_id, kategorie, titel, details) VALUES (?, ?, ?, ?)',
                   (current_user.id, kategorie, titel, details))
        db.commit()
        db.close()
        log_activity(current_user.id, 'vorschlag_neu', f'{current_user.name} hat einen Vorschlag eingereicht: {titel[:60]}',
                     icon='💡', color='gold')
        # Push an alle Admins
        try:
            admin_rows = get_db().execute("SELECT id FROM users WHERE role='admin' OR is_co_admin=1").fetchall()
            for a in admin_rows:
                send_push_to_user(a['id'],
                    title=f'💡 Neuer Vorschlag von {current_user.name}',
                    body=f'{titel[:80]} · Kategorie: {kategorie}',
                    url='/admin/vorschlaege', tag='vorschlag', push_type='vorschlag')
        except Exception:
            pass
        flash('Danke! Dein Vorschlag ist beim Admin angekommen.', 'success')
        return redirect(url_for('vorschlag'))

    db = get_db()
    own = db.execute('SELECT * FROM partner_suggestions WHERE user_id=? ORDER BY created_at DESC', (current_user.id,)).fetchall()
    db.close()
    return render_template('vorschlag.html', own=own)


@app.route('/admin/push')
@login_required
def admin_push():
    """Admin-Page: Manuell Push-Notifications senden + Stats."""
    if not current_user.has_admin_access:
        flash('Nur Admin', 'error')
        return redirect(url_for('dashboard'))
    db = get_db()
    sub_count = db.execute('SELECT COUNT(*) as c FROM push_subscriptions').fetchone()['c']
    user_count = db.execute('SELECT COUNT(DISTINCT user_id) as c FROM push_subscriptions').fetchone()['c']
    recent_log = db.execute('SELECT * FROM push_log ORDER BY id DESC LIMIT 20').fetchall()
    levels_active = db.execute('SELECT COALESCE(manual_career_level,1) as lvl, COUNT(*) as c FROM users WHERE active=1 GROUP BY lvl ORDER BY lvl').fetchall()
    db.close()
    return render_template('admin_push.html',
        sub_count=sub_count, user_count=user_count,
        recent_log=recent_log, levels_active=levels_active)


@app.route('/admin/vorschlaege')
@login_required
def admin_vorschlaege():
    """Admin sieht alle Vorschläge der Partner."""
    if not current_user.has_admin_access:
        flash('Nur Admin', 'error')
        return redirect(url_for('dashboard'))
    status = request.args.get('status', 'offen')
    db = get_db()
    if status == 'all':
        rows = db.execute('''SELECT s.*, u.name as user_name FROM partner_suggestions s
                            JOIN users u ON s.user_id=u.id ORDER BY s.created_at DESC''').fetchall()
    else:
        rows = db.execute('''SELECT s.*, u.name as user_name FROM partner_suggestions s
                            JOIN users u ON s.user_id=u.id WHERE s.status=? ORDER BY s.created_at DESC''', (status,)).fetchall()
    counts = {
        'offen': db.execute("SELECT COUNT(*) as c FROM partner_suggestions WHERE status='offen'").fetchone()['c'],
        'in_arbeit': db.execute("SELECT COUNT(*) as c FROM partner_suggestions WHERE status='in_arbeit'").fetchone()['c'],
        'erledigt': db.execute("SELECT COUNT(*) as c FROM partner_suggestions WHERE status='erledigt'").fetchone()['c'],
    }
    db.close()
    return render_template('admin_vorschlaege.html', suggestions=rows, status=status, counts=counts)


@app.route('/admin/vorschlaege/<int:sid>/respond', methods=['POST'])
@login_required
def admin_vorschlag_respond(sid):
    if not current_user.has_admin_access:
        return redirect(url_for('dashboard'))
    new_status = request.form.get('status', 'offen')
    response = (request.form.get('admin_response') or '').strip()
    db = get_db()
    db.execute('UPDATE partner_suggestions SET status=?, admin_response=?, updated_at=datetime("now") WHERE id=?',
               (new_status, response, sid))
    db.commit()
    db.close()
    flash('Vorschlag aktualisiert.', 'success')
    return redirect(url_for('admin_vorschlaege'))


@app.route('/namensliste/neu', methods=['GET', 'POST'])
@login_required
def namensliste_neu():
    """Wrapper: leitet auf /leads/neu — gleicher Backend, aber URL gehört zur Namensliste."""
    return lead_neu()


@app.route('/leads/<int:lead_id>/typ', methods=['POST'])
@login_required
def lead_change_typ(lead_id):
    """Lead zwischen VK und RK verschieben (1-Klick)."""
    new_typ = (request.form.get('typ') or 'vk').lower()
    if new_typ not in ('vk', 'rk'): new_typ = 'vk'
    db = get_db()
    db.execute('UPDATE leads SET liste_typ=? WHERE id=? AND owner_id=?',
               (new_typ, lead_id, current_user.id))
    db.commit()
    db.close()
    return redirect(url_for('namensliste', typ=new_typ))


@app.route('/namensliste')
@login_required
def namensliste():
    """Namensliste mit 2 Tabs: VK (Vertrieb) und RK (Rekrutierung)"""
    typ = (request.args.get('typ') or 'vk').lower()
    if typ not in ('vk', 'rk', 'all'): typ = 'vk'
    status_filter = request.args.get('status', '')
    db = get_db()
    # Counts pro Liste für Tabs
    vk_count = db.execute("SELECT COUNT(*) as c FROM leads WHERE owner_id=? AND COALESCE(liste_typ,'vk')='vk'", (current_user.id,)).fetchone()['c']
    rk_count = db.execute("SELECT COUNT(*) as c FROM leads WHERE owner_id=? AND liste_typ='rk'", (current_user.id,)).fetchone()['c']

    # Liste filtern
    if typ == 'all':
        base_q = 'SELECT * FROM leads WHERE owner_id = ?'
        params = [current_user.id]
    elif typ == 'rk':
        base_q = "SELECT * FROM leads WHERE owner_id = ? AND liste_typ = 'rk'"
        params = [current_user.id]
    else:  # vk
        base_q = "SELECT * FROM leads WHERE owner_id = ? AND COALESCE(liste_typ, 'vk') = 'vk'"
        params = [current_user.id]
    if status_filter:
        base_q += ' AND status = ?'
        params.append(status_filter)
    base_q += ' ORDER BY created_at DESC'
    rows = db.execute(base_q, params).fetchall()

    # Status-Counts (für die aktive Liste)
    by_status = {}
    for r in rows:
        by_status[r['status']] = by_status.get(r['status'], 0) + 1

    # Quoten berechnen für die aktive Liste
    total = len(rows)
    won_status = ('gewonnen', 'abgeschlossen')
    won = sum(1 for r in rows if r['status'] in won_status)
    contacted = sum(1 for r in rows if r['status'] not in ('neu',))
    quote = round((won / total * 100), 1) if total > 0 else 0
    contact_quote = round((contacted / total * 100), 1) if total > 0 else 0

    db.close()
    return render_template('namensliste.html',
        leads=rows, total=total, by_status=by_status,
        typ=typ, status_filter=status_filter,
        vk_count=vk_count, rk_count=rk_count,
        quote=quote, contact_quote=contact_quote, won=won, contacted=contacted)


# === QUOTEN ===
@app.route('/quoten')
@login_required
def quoten():
    db = get_db()
    current_month = date.today().strftime('%Y-%m')
    if current_user.role == 'admin':
        rows = db.execute('SELECT q.*, u.name as user_name, u.level FROM quotas q JOIN users u ON q.user_id = u.id WHERE q.monat = ? ORDER BY u.level, u.name', (current_month,)).fetchall()
        all_users = db.execute('SELECT id, name, level FROM users WHERE active = 1 ORDER BY level, name').fetchall()
    else:
        ids = [current_user.id] + get_all_descendants(current_user.id)
        ph = ','.join('?' * len(ids))
        rows = db.execute(f'SELECT q.*, u.name as user_name, u.level FROM quotas q JOIN users u ON q.user_id = u.id WHERE q.monat = ? AND q.user_id IN ({ph}) ORDER BY u.level, u.name', [current_month] + ids).fetchall()
        all_users = db.execute(f'SELECT id, name, level FROM users WHERE id IN ({ph}) AND active = 1 ORDER BY level, name', ids).fetchall()

    # Ist-Werte berechnen
    quoten_list = []
    for q in rows:
        d = dict(q)
        ist_eh = db.execute('SELECT COALESCE(SUM(einheiten), 0) as s FROM contracts WHERE owner_id = ? AND status = "abgeschlossen" AND recherche_status = "freigegeben" AND strftime("%Y-%m", abschluss_date) = ?', (q['user_id'], current_month)).fetchone()['s']
        ist_vtr = db.execute('SELECT COUNT(*) as c FROM contracts WHERE owner_id = ? AND status = "abgeschlossen" AND recherche_status = "freigegeben" AND strftime("%Y-%m", abschluss_date) = ?', (q['user_id'], current_month)).fetchone()['c']
        ist_partner = db.execute('SELECT COUNT(*) as c FROM users WHERE parent_id = ? AND strftime("%Y-%m", joined_date) = ?', (q['user_id'], current_month)).fetchone()['c']
        d['ist_einheiten'] = ist_eh
        d['ist_vertraege'] = ist_vtr
        d['ist_partner'] = ist_partner
        quoten_list.append(d)

    db.close()
    return render_template('quoten.html', quoten=quoten_list, current_month=current_month, all_users=all_users)


@app.route('/quoten/setzen', methods=['POST'])
@login_required
def quota_setzen():
    db = get_db()
    user_id = int(request.form['user_id'])
    monat = request.form['monat']
    existing = db.execute('SELECT id FROM quotas WHERE user_id = ? AND monat = ?', (user_id, monat)).fetchone()
    if existing:
        db.execute('UPDATE quotas SET ziel_einheiten=?, ziel_vertraege=?, ziel_partner=? WHERE user_id=? AND monat=?',
            (float(request.form.get('ziel_einheiten', 0) or 0),
             int(request.form.get('ziel_vertraege', 0) or 0),
             int(request.form.get('ziel_partner', 0) or 0), user_id, monat))
    else:
        db.execute('INSERT INTO quotas (user_id, monat, ziel_einheiten, ziel_vertraege, ziel_partner) VALUES (?, ?, ?, ?, ?)',
            (user_id, monat, float(request.form.get('ziel_einheiten', 0) or 0),
             int(request.form.get('ziel_vertraege', 0) or 0),
             int(request.form.get('ziel_partner', 0) or 0)))
    db.commit()
    db.close()
    flash('Quote gesetzt!', 'success')
    return redirect(url_for('quoten'))


# ═══════════════════════════════════════════════════════════
# MODUL A: TAGESAKTION (3-Agenten Council)
# ═══════════════════════════════════════════════════════════

def _build_team_snapshot_compact(admin_user_id):
    """Baut Daten-Subset (Variante B): 5 inaktivste + 5 underperformer + 5 stars.
    Returns formatierter String für Claude-Prompts."""
    db = get_db()
    descendants = get_all_descendants(admin_user_id)
    if not descendants:
        db.close()
        return 'Keine Team-Mitglieder im Strang.'
    placeholders = ','.join('?' * len(descendants))
    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    month_ago = (date.today() - timedelta(days=30)).isoformat()

    # 5 inaktivste (längste keine Aktivität)
    inaktiv = db.execute(f'''
        SELECT u.id, u.name, u.manual_career_level,
               (SELECT MAX(created_at) FROM activity_log WHERE user_id=u.id) as last_activity
        FROM users u
        WHERE u.id IN ({placeholders}) AND u.active=1
        ORDER BY last_activity ASC NULLS FIRST
        LIMIT 5
    ''', descendants).fetchall()

    # 5 underperformer (wenig EH letzte 30 Tage trotz Aktivität)
    under = db.execute(f'''
        SELECT u.id, u.name, u.manual_career_level,
               COALESCE(SUM(c.einheiten), 0) as eh_30d
        FROM users u
        LEFT JOIN contracts c ON c.owner_id=u.id AND c.status='abgeschlossen'
            AND c.recherche_status='freigegeben' AND date(c.abschluss_date) >= date(?)
        WHERE u.id IN ({placeholders}) AND u.active=1
        GROUP BY u.id
        ORDER BY eh_30d ASC
        LIMIT 5
    ''', [month_ago] + descendants).fetchall()

    # 5 stars (höchste EH letzte 30 Tage)
    stars = db.execute(f'''
        SELECT u.id, u.name, u.manual_career_level,
               COALESCE(SUM(c.einheiten), 0) as eh_30d,
               COUNT(c.id) as vertraege_30d
        FROM users u
        LEFT JOIN contracts c ON c.owner_id=u.id AND c.status='abgeschlossen'
            AND c.recherche_status='freigegeben' AND date(c.abschluss_date) >= date(?)
        WHERE u.id IN ({placeholders}) AND u.active=1
        GROUP BY u.id
        ORDER BY eh_30d DESC
        LIMIT 5
    ''', [month_ago] + descendants).fetchall()
    db.close()

    def fmt(rows, label, cols):
        if not rows: return f'{label}: keine Daten'
        lines = [f'{label}:']
        for r in rows:
            line = f'  - {r["name"]} (Stufe {r["manual_career_level"] or 1})'
            for c in cols:
                v = r[c] if c in r.keys() else None
                if v is not None:
                    line += f' · {c}={v}'
            lines.append(line)
        return '\n'.join(lines)

    out = []
    out.append(fmt(inaktiv, '5 INAKTIVSTE (kein Activity-Log)', ['last_activity']))
    out.append(fmt(under, '5 UNDERPERFORMER (wenig EH letzte 30d)', ['eh_30d']))
    out.append(fmt(stars, '5 STARS (top EH letzte 30d)', ['eh_30d', 'vertraege_30d']))
    return '\n\n'.join(out)


def _get_recent_actions_anti_dup(user_id, limit=5):
    """Letzte 5 Tagesaktionen für Anti-Duplikat-Hinweis."""
    db = get_db()
    rows = db.execute('''SELECT chairman_output FROM daily_actions
                         WHERE user_id=? ORDER BY created_at DESC LIMIT ?''',
                      (user_id, limit)).fetchall()
    db.close()
    if not rows: return ''
    return '\n\nLETZTE AKTIONEN (NICHT WIEDERHOLEN, andere Person/Aktion wählen):\n' + \
           '\n---\n'.join(r['chairman_output'][:300] for r in rows if r['chairman_output'])


SYSTEM_PSYCHOLOGE = """Du bist Führungs-Psychologe für Vertriebsteams (80+ Partner, Finanzberatung).
Schaue auf die Team-Daten und den heutigen Kontext (User: Stufe 5, Ziel Stufe 6).
Identifiziere EINE Person und EINE emotional richtige Aktion für heute.
Was braucht diese Person psychologisch gerade.
Output kompakt, deutsch, kein Coach-Sprech.

Format:
PERSON: <Name>
PSYCHOLOGISCHE BEOBACHTUNG: <2-3 Sätze>
EMPFOHLENE AKTION: <1-2 konkrete Sätze>"""

SYSTEM_HARDCORE = """Du bist Performance-Operator. Zahlen, keine Gefühle.
Wer underperformt, wer wird zu lange geschont, wo gehört Druck hin?
Identifiziere EINE Person und EINE rationale Aktion (hartes Gespräch, Klartext, KPI-Reset).
Output kompakt, deutsch, kein Soft-Talk.

Format:
PERSON: <Name>
HARTE BEOBACHTUNG: <Zahlen-basiert, 2-3 Sätze>
EMPFOHLENE AKTION: <Klartext, 1-2 Sätze>"""

SYSTEM_CHAIRMAN = """Du bekommst Psychologe + Knallhart. Synthetisiere zu EINER konkreten Aktion.
Output:
HEUTE MACH: <1 Zeile, konkret>
WER: <Name>
WIE: <3-5 Sätze, taktisch>
WARUM DIESE BALANCE: <1-2 Sätze>

Umsetzbar in unter 30 Minuten. Kein Bullshit, kein Coach-Sprech."""


def generate_daily_action(user_id, kontext=''):
    """Generiert die heutige Tagesaktion via 3-Agenten-Council.
    Returns dict {success, psyche, hardcore, chairman, target_name, error}."""
    if not is_ai_configured():
        return {'success': False, 'error': 'Anthropic API-Key nicht konfiguriert'}
    team_data = _build_team_snapshot_compact(user_id)
    anti_dup = _get_recent_actions_anti_dup(user_id, limit=5)

    base_prompt = f'''TEAM-DATEN:
{team_data}

KONTEXT HEUTE:
{kontext or '(kein zusätzlicher Kontext)'}
{anti_dup}

Output strikt im geforderten Format.'''

    # Agent 1: Psychologe
    psyche, err1 = claude_chat(base_prompt, system_prompt=SYSTEM_PSYCHOLOGE, max_tokens=600)
    if err1: return {'success': False, 'error': f'Psychologe-Agent: {err1}'}

    # Agent 2: Hardcore
    hardcore, err2 = claude_chat(base_prompt, system_prompt=SYSTEM_HARDCORE, max_tokens=600)
    if err2: return {'success': False, 'error': f'Hardcore-Agent: {err2}'}

    # Agent 3: Chairman synthesizes
    synth_prompt = f'''PSYCHOLOGE SAGT:
{psyche}

KNALLHARTER STRATEGE SAGT:
{hardcore}

KONTEXT HEUTE:
{kontext or '(kein zusätzlicher Kontext)'}

Synthetisiere zu EINER konkreten Aktion (Format wie System-Prompt).'''
    chairman, err3 = claude_chat(synth_prompt, system_prompt=SYSTEM_CHAIRMAN, max_tokens=700)
    if err3: return {'success': False, 'error': f'Chairman-Agent: {err3}'}

    # Target-Name aus Chairman-Output extrahieren
    target_name = ''
    for line in chairman.split('\n'):
        if line.upper().startswith('WER:'):
            target_name = line.split(':', 1)[1].strip()[:120]
            break

    return {
        'success': True,
        'psyche': psyche,
        'hardcore': hardcore,
        'chairman': chairman,
        'target_name': target_name,
    }


@app.route('/admin/tagesaktion', methods=['GET', 'POST'])
@login_required
def admin_tagesaktion():
    """Tagesaktion-UI: 3-Agenten-Council generiert EINE konkrete Team-Aktion."""
    if not current_user.has_admin_access:
        flash('Nur für Admins.', 'error')
        return redirect(url_for('dashboard'))

    db = get_db()
    if request.method == 'POST':
        kontext = (request.form.get('kontext') or '').strip()
        result = generate_daily_action(current_user.id, kontext)
        if not result.get('success'):
            flash(f'Fehler: {result.get("error", "?")}', 'error')
            db.close()
            return redirect(url_for('admin_tagesaktion'))
        # Speichern
        db.execute('''INSERT INTO daily_actions (user_id, datum, kontext,
                      psyche_output, hardcore_output, chairman_output, target_partner_name)
                      VALUES (?, ?, ?, ?, ?, ?, ?)''',
                   (current_user.id, date.today().isoformat(), kontext,
                    result['psyche'], result['hardcore'], result['chairman'],
                    result['target_name']))
        db.commit()
        flash('Tagesaktion generiert ✓', 'success')
        db.close()
        return redirect(url_for('admin_tagesaktion'))

    # GET: heutige Aktion + History
    today = date.today().isoformat()
    today_action = db.execute('''SELECT * FROM daily_actions WHERE user_id=? AND datum=?
                                 ORDER BY created_at DESC LIMIT 1''',
                              (current_user.id, today)).fetchone()
    history = db.execute('''SELECT * FROM daily_actions WHERE user_id=?
                            ORDER BY created_at DESC LIMIT 30''',
                         (current_user.id,)).fetchall()
    db.close()
    return render_template('admin_tagesaktion.html',
                           today_action=today_action, history=history,
                           ai_configured=is_ai_configured())


@app.route('/admin/tagesaktion/<int:aid>/done', methods=['POST'])
@login_required
def admin_tagesaktion_done(aid):
    if not current_user.has_admin_access:
        return jsonify({'ok': False}), 403
    db = get_db()
    db.execute('UPDATE daily_actions SET done_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?',
               (aid, current_user.id))
    db.commit()
    db.close()
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════
# MODUL B: CONTENT ENGINE (Partner + Reichweite für JIBSON.TV)
# ═══════════════════════════════════════════════════════════

CONTENT_TYPES = [
    ('hot_take', 'Hot Take / Bold Opinion', 'Reichweite-Play durch kontroverse Meinung'),
    ('how_to', 'How-To Breakdown', 'Authority — beweise dass du es kannst'),
    ('bts', 'BTS / Lifestyle', 'Connection — niedrig-Energie, dein Alltag in Köln, Gym, Ergo-Insights'),
    ('trend', 'Trend-Hijack mit eigenem Take', 'Viral durch Trending-Audio + eigene Meinung'),
    ('mistake', 'Mistake / Lesson Learned', 'Vulnerability — was hast du falsch gemacht'),
    ('listicle', 'Listicle / Carousel', 'Lehrhaft — 3-7 Punkte zu einem Thema'),
    ('story', 'Persönliche Story', 'Community-Bindung — 12-Jahre-Aufbau seit 16'),
]
CONTENT_TYPE_KEYS = [t[0] for t in CONTENT_TYPES]


def _recent_content_types(user_id, limit=5):
    """Letzte 5 content_types für den Reichweite-Agent (Anti-Repeat)."""
    db = get_db()
    rows = db.execute('''SELECT content_type FROM content_ideas
                         WHERE user_id=? AND agent_typ='reichweite' AND content_type IS NOT NULL
                         ORDER BY created_at DESC LIMIT ?''', (user_id, limit)).fetchall()
    db.close()
    return [r['content_type'] for r in rows if r['content_type']]


def _recent_partner_hooks(user_id, limit=5):
    """Letzte Partner-Agent-Hooks für Anti-Duplikat."""
    db = get_db()
    rows = db.execute('''SELECT hook FROM content_ideas
                         WHERE user_id=? AND agent_typ='partner'
                         ORDER BY created_at DESC LIMIT ?''', (user_id, limit)).fetchall()
    db.close()
    return [r['hook'][:200] for r in rows if r['hook']]


SYSTEM_PARTNER = """Du bist Strategist für Recruiting-Content im Finanzberater-Business.
Najibs Ziel: ambitionierte Menschen (20-35) anziehen, die finanziell unzufrieden sind oder
mehr aus ihrem Leben rausholen wollen.
Brand-Stil: direkt, ehrlich, ohne Hochglanz, Werte > Geld-Porno.

Generiere EINE Video-Idee mit:
HOOK: <1 Zeile>
STORYLINE: <3-5 Sätze>
CTA: <konkret am Ende>
FORMAT: <Talking Head / BTS / Story>

Output deutsch, knapp, umsetzbar."""

SYSTEM_REICHWEITE = """Du bist Viral-Content-Stratege für JIBSON.TV (TikTok/Instagram, ~40k Follower).
Ziel: maximale Reichweite und Connection.

WICHTIG: Wähle EINEN dieser Content-Typen, NICHT die zuletzt verwendeten:
1. hot_take — Hot Take / Bold Opinion
2. how_to — How-To Breakdown
3. bts — BTS / Lifestyle (Köln, Gym, Ergo-Alltag)
4. trend — Trend-Hijack mit eigenem Take
5. mistake — Mistake / Lesson Learned
6. listicle — Listicle / Carousel
7. story — Persönliche Story (12-Jahre-Aufbau seit 16)

Generiere EINE Video-Idee mit:
TYP: <einer der 7 keys>
HOOK: <scroll-stop, 1 Zeile>
STORYLINE: <3-5 Sätze>
CAPTION: <Vorschlag>
MECHANIK: <warum das funktioniert>

Output deutsch, knapp, mutig — keine sicheren Ideen."""


def _parse_content_output(text, agent_typ):
    """Parst die strukturierten Outputs (HOOK/STORYLINE/etc.) in dict."""
    fields = {'hook': '', 'storyline': '', 'cta_or_caption': '', 'mechanik': '', 'content_type': ''}
    current = None
    buffer = []
    for line in text.split('\n'):
        line = line.strip()
        if not line: continue
        upper = line.upper()
        if upper.startswith('HOOK:'):
            if current: fields[current] = '\n'.join(buffer).strip()
            current, buffer = 'hook', [line.split(':', 1)[1].strip()]
        elif upper.startswith('STORYLINE:'):
            if current: fields[current] = '\n'.join(buffer).strip()
            current, buffer = 'storyline', [line.split(':', 1)[1].strip()]
        elif upper.startswith(('CTA:', 'CAPTION:')):
            if current: fields[current] = '\n'.join(buffer).strip()
            current, buffer = 'cta_or_caption', [line.split(':', 1)[1].strip()]
        elif upper.startswith('MECHANIK:'):
            if current: fields[current] = '\n'.join(buffer).strip()
            current, buffer = 'mechanik', [line.split(':', 1)[1].strip()]
        elif upper.startswith(('TYP:', 'FORMAT:')):
            if current: fields[current] = '\n'.join(buffer).strip()
            val = line.split(':', 1)[1].strip().lower()
            # Map zu unseren CONTENT_TYPE_KEYS
            for key in CONTENT_TYPE_KEYS:
                if key in val:
                    fields['content_type'] = key
                    break
            current, buffer = None, []
        elif current:
            buffer.append(line)
    if current: fields[current] = '\n'.join(buffer).strip()
    return fields


def generate_content_idea(user_id, agent_typ, kontext=''):
    """Generiert EINE Content-Idee via gewähltem Agent.
    agent_typ: 'partner' oder 'reichweite'."""
    if not is_ai_configured():
        return {'success': False, 'error': 'Anthropic API-Key nicht konfiguriert'}

    if agent_typ == 'partner':
        recent_hooks = _recent_partner_hooks(user_id, limit=5)
        anti_dup = ''
        if recent_hooks:
            anti_dup = '\n\nLETZTE 5 HOOKS (NICHT WIEDERHOLEN, gleiches Thema OK aber andere Hook):\n- ' + '\n- '.join(recent_hooks)
        prompt = f'''KONTEXT (was passiert gerade in Najib's Leben):
{kontext or '(kein zusätzlicher Kontext)'}{anti_dup}

Generiere EINE Recruiting-Content-Idee im geforderten Format.'''
        text, err = claude_chat(prompt, system_prompt=SYSTEM_PARTNER, max_tokens=800)
    elif agent_typ == 'reichweite':
        recent_types = _recent_content_types(user_id, limit=5)
        avoid_msg = ''
        if recent_types:
            avoid_msg = f'\n\nZULETZT VERWENDET ({len(recent_types)}× — NICHT WÄHLEN): {", ".join(recent_types)}'
        prompt = f'''KONTEXT (was passiert gerade in Najib's Leben):
{kontext or '(kein zusätzlicher Kontext)'}{avoid_msg}

Generiere EINE Viral-Content-Idee im geforderten Format. Wähle einen Typ den du NICHT zuletzt verwendet hast.'''
        text, err = claude_chat(prompt, system_prompt=SYSTEM_REICHWEITE, max_tokens=800)
    else:
        return {'success': False, 'error': f'Unbekannter Agent: {agent_typ}'}

    if err: return {'success': False, 'error': err}
    parsed = _parse_content_output(text, agent_typ)
    return {
        'success': True, 'agent_typ': agent_typ,
        'full_output': text,
        **parsed,
    }


@app.route('/admin/content', methods=['GET', 'POST'])
@login_required
def admin_content():
    """Content Engine: 2 Agenten generieren Video-Ideen für JIBSON.TV."""
    if not current_user.has_admin_access:
        flash('Nur für Admins.', 'error')
        return redirect(url_for('dashboard'))

    db = get_db()
    if request.method == 'POST':
        kontext = (request.form.get('kontext') or '').strip()
        agent = (request.form.get('agent') or 'partner').strip()
        if agent not in ('partner', 'reichweite'):
            flash('Ungültiger Agent.', 'error')
            db.close()
            return redirect(url_for('admin_content'))
        result = generate_content_idea(current_user.id, agent, kontext)
        if not result.get('success'):
            flash(f'Fehler: {result.get("error")}', 'error')
            db.close()
            return redirect(url_for('admin_content'))
        db.execute('''INSERT INTO content_ideas (user_id, datum, kontext, agent_typ,
                      content_type, hook, storyline, cta_or_caption, mechanik, full_output)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                   (current_user.id, date.today().isoformat(), kontext, result['agent_typ'],
                    result.get('content_type', ''), result.get('hook', ''),
                    result.get('storyline', ''), result.get('cta_or_caption', ''),
                    result.get('mechanik', ''), result['full_output']))
        db.commit()
        flash(f'{agent.title()}-Idee generiert ✓', 'success')
        db.close()
        return redirect(url_for('admin_content'))

    # GET: neueste pro Agent + History
    today_iso = date.today().isoformat()
    latest_partner = db.execute('''SELECT * FROM content_ideas WHERE user_id=? AND agent_typ='partner'
                                   ORDER BY created_at DESC LIMIT 1''', (current_user.id,)).fetchone()
    latest_reichweite = db.execute('''SELECT * FROM content_ideas WHERE user_id=? AND agent_typ='reichweite'
                                      ORDER BY created_at DESC LIMIT 1''', (current_user.id,)).fetchone()
    history_partner = db.execute('''SELECT * FROM content_ideas WHERE user_id=? AND agent_typ='partner'
                                    ORDER BY created_at DESC LIMIT 30''', (current_user.id,)).fetchall()
    history_reichweite = db.execute('''SELECT * FROM content_ideas WHERE user_id=? AND agent_typ='reichweite'
                                       ORDER BY created_at DESC LIMIT 30''', (current_user.id,)).fetchall()
    db.close()

    # Auto-Suggest: wenn heute noch keine Idee → Background-Generierung
    needs_partner = not latest_partner or (latest_partner['datum'] != today_iso)
    needs_reichweite = not latest_reichweite or (latest_reichweite['datum'] != today_iso)
    auto_generating = []

    def _bg_generate(uid, agent_typ):
        """Hintergrund-Generierung — Result landet in DB, User reloadet."""
        try:
            with app.app_context():
                result = generate_content_idea(uid, agent_typ, '')
                if result.get('success'):
                    db_bg = get_db()
                    db_bg.execute('''INSERT INTO content_ideas (user_id, datum, kontext, agent_typ,
                                  content_type, hook, storyline, cta_or_caption, mechanik, full_output)
                                  VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?)''',
                               (uid, date.today().isoformat(), result['agent_typ'],
                                result.get('content_type', ''), result.get('hook', ''),
                                result.get('storyline', ''), result.get('cta_or_caption', ''),
                                result.get('mechanik', ''), result['full_output']))
                    db_bg.commit()
                    db_bg.close()
        except Exception as e:
            print(f'[content_auto_suggest] {agent_typ}: {e}')

    if is_ai_configured():
        import threading as _t
        if needs_partner:
            _t.Thread(target=_bg_generate, args=(current_user.id, 'partner'), daemon=True).start()
            auto_generating.append('partner')
        if needs_reichweite:
            _t.Thread(target=_bg_generate, args=(current_user.id, 'reichweite'), daemon=True).start()
            auto_generating.append('reichweite')

    return render_template('admin_content.html',
                           latest_partner=latest_partner, latest_reichweite=latest_reichweite,
                           history_partner=history_partner, history_reichweite=history_reichweite,
                           ai_configured=is_ai_configured(),
                           auto_generating=auto_generating, today_iso=today_iso)


@app.route('/admin/content/<int:cid>/used', methods=['POST'])
@login_required
def admin_content_used(cid):
    if not current_user.has_admin_access:
        return jsonify({'ok': False}), 403
    db = get_db()
    db.execute('UPDATE content_ideas SET used_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?',
               (cid, current_user.id))
    db.commit()
    db.close()
    return jsonify({'ok': True})


# DB-Initialisierung läuft IMMER beim Modul-Laden (auch bei gunicorn in Production)
init_db()


def _warm_cache_background():
    """Pre-warmt die teuren Cache-Wrapper für die wichtigsten User direkt nach dem Boot.
    Eliminiert den 6-Sekunden-Cold-Cache-Hit beim ersten Dashboard-Aufruf.
    Läuft in Daemon-Thread → Boot wird NICHT blockiert."""
    import time as _t
    _t.sleep(1.5)  # init_db beenden lassen
    print('[warm] Background-Cache-Warmer gestartet ...', flush=True)
    t_start = _t.time()
    try:
        with app.app_context():
            db = get_db()
            ids = [r['id'] for r in db.execute(
                "SELECT id FROM users WHERE active = 1 AND (role = 'admin' OR manual_career_level >= 3) LIMIT 5"
            ).fetchall()]
            db.close()
            # Globale Helper (1×, nicht pro User)
            try:
                get_smart_insights(scope_user_id=None)
                print('[warm] ✓ smart_insights(global)', flush=True)
            except Exception as e:
                print(f'[warm] ✗ smart_insights(global): {e}', flush=True)
            # Pro-User-Helper (alles was die Dashboards aufrufen)
            for uid in ids:
                t_user = _t.time()
                helpers = [
                    # ★ Mega-Cache für Admin-Dashboard (19 Queries → 1 Cache)
                    ('admin_dash', lambda u=uid: get_admin_dashboard_stats(u)),
                    ('strang', lambda u=uid: get_strang_status(u)),
                    ('forecast30', lambda u=uid: get_quoten_forecast(u, days=30)),
                    ('forecast', lambda u=uid: get_forecast(u)),
                    ('recent', lambda u=uid: get_recent_partner_views(u, limit=3)),
                    ('admin_pers', lambda u=uid: get_admin_personal_dashboard(u)),
                    ('career', lambda u=uid: get_career_level_for_user(u)),
                    ('insights_user', lambda u=uid: get_smart_insights(scope_user_id=u)),
                    ('inactive_dir', lambda u=uid: get_inactive_team_members(u, days=3, scope='direct')),
                    ('inactive_all', lambda u=uid: get_inactive_team_members(u, days=1, scope='all')),
                    # LLM-Helper — die teuersten, weil Claude-API-Calls (5-10s)
                    ('ai_briefing', lambda u=uid: ai_generate_weekly_briefing(u)),
                    ('ki_recs', lambda u=uid: get_ki_recommendations(u, scope_user_id=None)),
                    ('coach_actions', lambda u=uid: get_coach_actions(u, max_actions=5)),
                    ('struktur_news', lambda u=uid: get_struktur_news(u, days=7, limit=10)),
                ]
                for name, fn in helpers:
                    try: fn()
                    except Exception as e: print(f'[warm] ✗ {name} {uid}: {e}', flush=True)
                print(f'[warm] ✓ User {uid} gewarmt in {(_t.time()-t_user)*1000:.0f}ms', flush=True)
            print(f'[warm] FERTIG · {len(ids)} User · Total {(_t.time()-t_start)*1000:.0f}ms', flush=True)
    except Exception as e:
        print(f'[warm] Background-Warm fehlgeschlagen: {e}', flush=True)


# Background-Warmer starten (Daemon → stirbt mit dem Prozess, blockiert nichts)
import threading as _threading
_threading.Thread(target=_warm_cache_background, daemon=True, name='cache-warmer').start()


if __name__ == '__main__':
    print("\n" + "="*60)
    print("✅ Pro Academy – Control Hub gestartet!")
    print("="*60)
    print("🌐 Öffne: http://localhost:5001")
    print("📧 Admin Login: najib@ntpro.de")
    print("🔑 Passwort: admin123")
    print("="*60 + "\n")
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=debug, host='0.0.0.0', port=port)
