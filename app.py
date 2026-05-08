from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session, send_file, Response
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import hashlib
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

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
def send_email(to, subject, body_text, body_html=None, sent_by=None):
    """Sendet E-Mail über konfigurierten SMTP. Returns (ok, error_msg)."""
    smtp_host = get_setting('smtp_host')
    smtp_port = int(get_setting('smtp_port', '587'))
    smtp_user = get_setting('smtp_user')
    smtp_password = get_setting('smtp_password')
    sender_name = get_setting('smtp_from_name', 'NT Pro Academy')
    sender_email = get_setting('smtp_from_email', smtp_user)

    if not all([smtp_host, smtp_user, smtp_password]):
        return False, 'SMTP nicht konfiguriert. Geh zu Einstellungen → E-Mail-Versand.'

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = formataddr((sender_name, sender_email))
    msg['To'] = to
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


def send_bulk_emails(recipients, subject, body_text, body_html=None, sent_by=None):
    """Sendet E-Mail an mehrere Empfänger. Returns (success_count, fail_list)."""
    success = 0
    fails = []
    for r in recipients:
        ok, err = send_email(r, subject, body_text, body_html, sent_by)
        if ok:
            success += 1
        else:
            fails.append({'email': r, 'error': err})
    return success, fails


def get_period_stats(scope_user_id=None):
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
]

ACHIEVEMENT_TIER_COLORS = {
    'bronze': '#cd7f32', 'silver': '#94a3b8', 'gold': '#d4a843', 'platinum': '#a78bfa'
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


@app.context_processor
def inject_career():
    """Stellt aktuelle Karriere-Stufe + Pending-Anzahl + Coach-Anzahl in allen Templates bereit."""
    if current_user.is_authenticated:
        ctx = {'my_career': get_career_level_for_user(current_user.id)}
        if current_user.role == 'admin':
            db = get_db()
            cnt = db.execute('SELECT COUNT(*) as c FROM users WHERE pending_career_level IS NOT NULL AND active = 1').fetchone()['c']
            db.close()
            ctx['pending_count'] = cnt
        # Coach-Insights für Bell-Badge — nur leichtgewichtig zählen
        try:
            scope = None if current_user.role == 'admin' else current_user.id
            insights = get_smart_insights(scope_user_id=scope)
            ctx['coach_alerts'] = insights['urgent_count']
        except Exception:
            ctx['coach_alerts'] = 0
        return ctx
    return {}


# === ADMIN: PASSWORT-RESET ===
@app.route('/admin/team/<int:uid>/reset-password', methods=['POST'])
@login_required
def admin_reset_password(uid):
    if current_user.role != 'admin':
        return redirect(url_for('dashboard'))
    new_pw = generate_random_password()
    db = get_db()
    db.execute('UPDATE users SET password = ? WHERE id = ?', (hash_password(new_pw), uid))
    user = db.execute('SELECT name, email FROM users WHERE id = ?', (uid,)).fetchone()
    db.commit()
    db.close()
    flash(f'Passwort zurückgesetzt für {user["name"]} ({user["email"]}). Neues Passwort: {new_pw}', 'success')
    return redirect(url_for('team'))


# === ADMIN: SMTP + E-MAIL ===
@app.route('/admin/email-settings', methods=['GET', 'POST'])
@login_required
def admin_email_settings():
    if current_user.role != 'admin':
        flash('Keine Berechtigung', 'error')
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
        'smtp_from_name': get_setting('smtp_from_name', 'NT Pro Academy'),
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
                         '✅ Test-E-Mail von NT Pro Academy',
                         f'Hallo!\n\nDies ist eine Test-E-Mail von deinem Control Hub.\nWenn du das siehst, ist alles richtig konfiguriert! 🎉\n\nGesendet: {datetime.now().strftime("%d.%m.%Y %H:%M")}',
                         body_html=f'<h2 style="color:#0f1c3f">✅ Test erfolgreich!</h2><p>Dies ist eine Test-E-Mail von deinem <strong>NT Pro Academy Control Hub</strong>.</p><p>Wenn du das siehst, ist alles richtig konfiguriert! 🎉</p><p style="color:#94a3b8;font-size:12px">Gesendet: {datetime.now().strftime("%d.%m.%Y %H:%M")}</p>',
                         sent_by=current_user.id)
    if ok:
        flash(f'✅ Test-E-Mail erfolgreich an {to_email} gesendet!', 'success')
    else:
        flash(f'❌ Versand fehlgeschlagen: {err}', 'error')
    return redirect(url_for('admin_email_settings'))


@app.route('/admin/mail', methods=['GET', 'POST'])
@login_required
def admin_mail():
    """Bulk-Mailer für Admin."""
    if current_user.role != 'admin':
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
<div style="font-size:20px;font-weight:800">NT Pro Academy</div>
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
    if current_user.role != 'admin':
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
    if current_user.role != 'admin':
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
    if current_user.role != 'admin':
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
    if current_user.role != 'admin':
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
    if current_user.role != 'admin':
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
    if current_user.role != 'admin':
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
    if current_user.role != 'admin':
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
        ]:
            if new_col not in col_names:
                db.execute(f"ALTER TABLE users ADD COLUMN {new_col} {sql_type}")

        # leads
        lead_cols = db.execute("PRAGMA table_info(leads)").fetchall()
        lead_col_names = [c['name'] for c in lead_cols]
        if 'birthday' not in lead_col_names:
            db.execute("ALTER TABLE leads ADD COLUMN birthday TEXT")
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


def get_user_total_eh(user_id, include_team=False):
    """EH eines Users (eigene oder mit Team)"""
    db = get_db()
    if include_team:
        ids = [user_id] + get_all_descendants(user_id)
    else:
        ids = [user_id]
    placeholders = ','.join('?' * len(ids))
    result = db.execute(
        f'SELECT COALESCE(SUM(einheiten), 0) as total FROM contracts WHERE owner_id IN ({placeholders}) AND status = "abgeschlossen" AND recherche_status = "freigegeben"',
        ids
    ).fetchone()
    db.close()
    return result['total']


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


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        db = get_db()
        row = db.execute('SELECT * FROM users WHERE email = ? AND active = 1', (email,)).fetchone()
        if row and verify_password(row['password'], password):
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
            login_user(User(row))
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
            # Erstes Login → Onboarding-Wizard zeigen (nur Nicht-Admins)
            if not onboarding_done and row['role'] != 'admin':
                return redirect(url_for('willkommen'))
            return redirect(url_for('dashboard'))
        db.close()
        flash('Falsche E-Mail oder Passwort', 'error')
    return render_template('login.html')


@app.route('/profil', methods=['GET', 'POST'])
@login_required
def profil():
    """Eigenes Profil — jeder darf Vision, Passwort, Telefon selbst ändern."""
    db = get_db()
    if request.method == 'POST':
        vision = request.form.get('vision', '').strip()
        phone = request.form.get('phone', '').strip()
        birthday = request.form.get('birthday', '').strip() or None
        new_password = request.form.get('password', '').strip()
        if new_password:
            db.execute('UPDATE users SET vision=?, phone=?, birthday=?, password=? WHERE id=?',
                       (vision, phone, birthday, hash_password(new_password), current_user.id))
        else:
            db.execute('UPDATE users SET vision=?, phone=?, birthday=? WHERE id=?',
                       (vision, phone, birthday, current_user.id))
        db.commit()
        db.close()
        flash('Profil aktualisiert!', 'success')
        return redirect(url_for('profil'))
    user = db.execute('SELECT * FROM users WHERE id = ?', (current_user.id,)).fetchone()
    db.close()
    return render_template('profil.html', user=user)


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
    db = get_db()
    if vision:
        db.execute('UPDATE users SET vision = ?, onboarding_done = 1 WHERE id = ?', (vision, current_user.id))
    else:
        db.execute('UPDATE users SET onboarding_done = 1 WHERE id = ?', (current_user.id,))
    db.commit()
    db.close()
    flash('Willkommen im Team! Los geht\'s 🚀', 'success')
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


@app.route('/dashboard')
@login_required
def dashboard():
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
        total_users = db.execute('SELECT COUNT(*) as c FROM users WHERE active = 1').fetchone()['c']
        total_leads = db.execute('SELECT COUNT(*) as c FROM leads').fetchone()['c']
        total_contracts = db.execute('SELECT COUNT(*) as c FROM contracts WHERE status = "abgeschlossen" AND recherche_status = "freigegeben"').fetchone()['c']
        total_volumen = db.execute('SELECT COALESCE(SUM(volumen), 0) as s FROM contracts WHERE status = "abgeschlossen" AND recherche_status = "freigegeben"').fetchone()['s']
        total_einheiten = db.execute('SELECT COALESCE(SUM(einheiten), 0) as s FROM contracts WHERE status = "abgeschlossen" AND recherche_status = "freigegeben"').fetchone()['s']
        open_appointments = db.execute('SELECT COUNT(*) as c FROM appointments WHERE status = "geplant"').fetchone()['c']

        # Top Performer mit EH
        top_rows = db.execute('''
            SELECT u.id, u.name, u.level, u.manual_career_level,
                   COALESCE(SUM(c.einheiten), 0) as einheiten,
                   COUNT(c.id) as vertraege
            FROM users u
            LEFT JOIN contracts c ON c.owner_id = u.id AND c.status = "abgeschlossen" AND recherche_status = "freigegeben"
            WHERE u.active = 1
            GROUP BY u.id
            ORDER BY einheiten DESC
            LIMIT 10
        ''').fetchall()
        top_performer = []
        for r in top_rows:
            d = dict(r)
            d['career'] = career_for_row(r['manual_career_level'], r['einheiten'])
            top_performer.append(d)

        direct_rows = db.execute('''
            SELECT u.*, COUNT(c.id) as vertraege,
                   COALESCE(SUM(c.einheiten), 0) as einheiten,
                   COALESCE(SUM(c.volumen), 0) as volumen
            FROM users u
            LEFT JOIN contracts c ON c.owner_id = u.id AND c.status = "abgeschlossen" AND recherche_status = "freigegeben"
            WHERE u.parent_id = ? AND u.active = 1
            GROUP BY u.id
        ''', (current_user.id,)).fetchall()
        direct_partners = []
        for r in direct_rows:
            d = dict(r)
            d['career'] = career_for_row(r['manual_career_level'], r['einheiten'])
            direct_partners.append(d)

        recent_contracts = db.execute('''
            SELECT c.*, u.name as berater_name FROM contracts c
            JOIN users u ON c.owner_id = u.id
            ORDER BY c.created_at DESC LIMIT 5
        ''').fetchall()

        monthly_data = db.execute('''
            SELECT strftime('%Y-%m', abschluss_date) as monat,
                   COUNT(*) as anzahl, SUM(einheiten) as einheiten
            FROM contracts
            WHERE status = "abgeschlossen" AND recherche_status = "freigegeben" AND abschluss_date >= date('now', '-6 months')
            GROUP BY monat ORDER BY monat
        ''').fetchall()

        # Vormonats-Vergleich
        cur_month = date.today().strftime('%Y-%m')
        if date.today().month == 1:
            prev_month = f'{date.today().year - 1}-12'
        else:
            prev_month = f'{date.today().year}-{date.today().month - 1:02d}'

        def stat_for_month(month):
            eh = db.execute('SELECT COALESCE(SUM(einheiten),0) as s FROM contracts WHERE status="abgeschlossen" AND recherche_status="freigegeben" AND strftime("%Y-%m", abschluss_date)=?', (month,)).fetchone()['s']
            vtr = db.execute('SELECT COUNT(*) as c FROM contracts WHERE status="abgeschlossen" AND recherche_status="freigegeben" AND strftime("%Y-%m", abschluss_date)=?', (month,)).fetchone()['c']
            new_partners = db.execute('SELECT COUNT(*) as c FROM users WHERE strftime("%Y-%m", joined_date)=? AND active=1', (month,)).fetchone()['c']
            volumen = db.execute('SELECT COALESCE(SUM(volumen),0) as s FROM contracts WHERE status="abgeschlossen" AND recherche_status="freigegeben" AND strftime("%Y-%m", abschluss_date)=?', (month,)).fetchone()['s']
            return {'eh': eh, 'vtr': vtr, 'partner': new_partners, 'volumen': volumen}

        cur_stats = stat_for_month(cur_month)
        prev_stats = stat_for_month(prev_month)

        def pct_change(cur, prev):
            if prev == 0:
                return 100 if cur > 0 else 0
            return ((cur - prev) / prev) * 100

        comparison = {
            'cur_month': cur_month, 'prev_month': prev_month,
            'cur': cur_stats, 'prev': prev_stats,
            'eh_pct': pct_change(cur_stats['eh'], prev_stats['eh']),
            'vtr_pct': pct_change(cur_stats['vtr'], prev_stats['vtr']),
            'partner_pct': pct_change(cur_stats['partner'], prev_stats['partner']),
            'volumen_pct': pct_change(cur_stats['volumen'], prev_stats['volumen']),
        }

        # Geschäftspartner-Entwicklung (12 Monate)
        partner_growth = db.execute('''
            SELECT strftime('%Y-%m', joined_date) as monat, COUNT(*) as neue_partner
            FROM users WHERE active = 1 AND joined_date >= date('now', '-12 months')
            GROUP BY monat ORDER BY monat
        ''').fetchall()

        admin_user = db.execute('SELECT vision FROM users WHERE id = ?', (current_user.id,)).fetchone()
        admin_vision = (admin_user['vision'] if admin_user else '') or ''
        admin_show_vision = session.pop('show_vision', False) and admin_vision.strip() != ''
        db.close()
        # KI-Coach: Top-3 Anrufe für Quick-Card
        coach_insights = get_smart_insights(scope_user_id=None)
        # Personalisierte Begrüßung
        greeting = get_greeting_for_user(current_user.name, career, next_level, own_eh, eh_to_next)
        # Monats- + Halbjahres-Daten + Karriere-Kriterien
        period_stats = get_period_stats(scope_user_id=None)
        career_criteria = get_career_criteria_status(current_user.id)
        # Power-KI-Empfehlungen
        ki_recs = get_ki_recommendations(current_user.id, scope_user_id=None)
        return render_template('dashboard_admin.html',
            total_users=total_users, total_leads=total_leads,
            total_contracts=total_contracts, total_volumen=total_volumen,
            total_einheiten=total_einheiten, open_appointments=open_appointments,
            top_performer=top_performer, direct_partners=direct_partners,
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
            ki_recs=ki_recs
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
            ki_recs=ki_recs
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
    if request.method == 'POST':
        db = get_db()
        db.execute('INSERT INTO leads (owner_id, name, email, phone, birthday, produkt, status, notizen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (current_user.id, request.form['name'], request.form.get('email', ''),
             request.form.get('phone', ''), request.form.get('birthday') or None,
             request.form.get('produkt', ''),
             request.form.get('status', 'neu'), request.form.get('notizen', '')))
        db.commit()
        db.close()
        log_activity(current_user.id, 'lead_neu',
            f'{current_user.name} hat „{request.form["name"]}" zur Namensliste hinzugefügt',
            icon='◇', color='purple')
        flash('Lead erfolgreich angelegt!', 'success')
        return redirect(url_for('leads'))
    return render_template('lead_form.html', lead=None)


@app.route('/leads/<int:lead_id>/edit', methods=['GET', 'POST'])
@login_required
def lead_edit(lead_id):
    db = get_db()
    lead = db.execute('SELECT * FROM leads WHERE id = ?', (lead_id,)).fetchone()
    if not lead:
        db.close()
        return redirect(url_for('leads'))
    if request.method == 'POST':
        db.execute('UPDATE leads SET name=?, email=?, phone=?, birthday=?, produkt=?, status=?, notizen=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
            (request.form['name'], request.form.get('email', ''), request.form.get('phone', ''),
             request.form.get('birthday') or None,
             request.form.get('produkt', ''), request.form.get('status', 'neu'),
             request.form.get('notizen', ''), lead_id))
        db.commit()
        db.close()
        flash('Lead aktualisiert!', 'success')
        return redirect(url_for('leads'))
    db.close()
    return render_template('lead_form.html', lead=lead)


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
        db = get_db()
        cur = db.execute('''INSERT INTO contracts
                (owner_id, client_name, produkt, volumen, einheiten, provision, status, abschluss_date, notizen,
                 recherche_done, telefonat_done, unterlagen_done, nachweise_done, unterschrieben, freizeichnung_done, recherche_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
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
             request.form.get('recherche_status', 'ausstehend')))
        new_id = cur.lastrowid
        db.commit()
        db.close()
        auto_promote_user(current_user.id)
        recalculate_all_commissions()
        if request.form.get('status') == 'abgeschlossen' and request.form.get('recherche_status') == 'freigegeben':
            log_activity(current_user.id, 'vertrag_abgeschlossen',
                f'{current_user.name} hat Vertrag „{request.form["client_name"]}" abgeschlossen ({einheiten:.0f} EH)',
                icon='🎉', color='green')
        else:
            log_activity(current_user.id, 'vertrag_neu',
                f'{current_user.name} hat neuen Vertrag „{request.form["client_name"]}" angelegt ({einheiten:.0f} EH)',
                icon='📄', color='gold')
        flash(f'Vertrag angelegt! ({einheiten:.0f} EH)', 'success')
        return redirect(url_for('vertraege'))
    return render_template('vertrag_form.html', vertrag=None, eh_faktor=EH_FAKTOR)


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
        db.execute('''UPDATE contracts SET client_name=?, produkt=?, volumen=?, einheiten=?, provision=?, status=?, abschluss_date=?, notizen=?,
                      recherche_done=?, telefonat_done=?, unterlagen_done=?, nachweise_done=?, unterschrieben=?, freizeichnung_done=?, recherche_status=?
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
             request.form.get('recherche_status', 'ausstehend'), vid))
        db.commit()
        db.close()
        auto_promote_user(owner_id)
        recalculate_all_commissions()
        flash(f'Vertrag aktualisiert! ({einheiten:.0f} EH)', 'success')
        return redirect(url_for('vertraege'))
    db.close()
    return render_template('vertrag_form.html', vertrag=vertrag, eh_faktor=EH_FAKTOR)


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
        flash('Termin angelegt!', 'success')
        return redirect(url_for('termine'))
    return render_template('termin_form.html', termin=None)


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
            cur = db.execute('''INSERT INTO users (name, email, password, role, parent_id, level, phone,
                          manual_career_level, pending_career_level, pending_by_user_id, pending_at)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (request.form['name'], email, hash_password(request.form.get('password', 'start123')),
                 'partner', parent_id, new_level, request.form.get('phone', ''),
                 manual_level, pending_level, pending_by, pending_at))
            new_user_id = cur.lastrowid
            db.commit()
            db.close()
            stufe_short = next((cl['short'] for cl in CAREER_LEVELS if cl['level'] == manual_level), 'REP')
            log_activity(new_user_id, 'partner_neu',
                f'{request.form["name"]} ist neuer Geschäftspartner ({stufe_short})',
                icon='👥', color='green')
            if pending_level:
                flash(f'Mitglied angelegt! Login: {email}. Stufe {pending_level} wartet auf Admin-Bestätigung.', 'success')
            else:
                flash(f'Mitglied angelegt! Login: {email} / Passwort: {request.form.get("password", "start123")}', 'success')
            return redirect(url_for('team'))
    db.close()
    return render_template('team_form.html', member=None, possible_parents=possible_parents, all_levels=CAREER_LEVELS)


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

        if new_password:
            db.execute('''UPDATE users SET name=?, email=?, phone=?, parent_id=?, level=?, password=?,
                          manual_career_level=?, pending_career_level=?, pending_by_user_id=?, pending_at=?,
                          onboarding_endgespraech=?, onboarding_einarbeitung_1=?, onboarding_einarbeitung_2=?,
                          onboarding_einarbeitung_3=?, onboarding_seminar_bezahlt=?
                          WHERE id=?''',
                (request.form['name'], request.form['email'], request.form.get('phone', ''),
                 parent_id, new_level, hash_password(new_password),
                 manual_level, pending_level, pending_by, pending_at,
                 ob_eg, ob_e1, ob_e2, ob_e3, ob_sb, uid))
        else:
            db.execute('''UPDATE users SET name=?, email=?, phone=?, parent_id=?, level=?,
                          manual_career_level=?, pending_career_level=?, pending_by_user_id=?, pending_at=?,
                          onboarding_endgespraech=?, onboarding_einarbeitung_1=?, onboarding_einarbeitung_2=?,
                          onboarding_einarbeitung_3=?, onboarding_seminar_bezahlt=?
                          WHERE id=?''',
                (request.form['name'], request.form['email'], request.form.get('phone', ''),
                 parent_id, new_level,
                 manual_level, pending_level, pending_by, pending_at,
                 ob_eg, ob_e1, ob_e2, ob_e3, ob_sb, uid))
        db.commit()
        db.close()
        recalculate_all_commissions()
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

    db.close()
    return render_template('aufgaben.html',
        tasks=tasks, user_status=user_status, today=today, career=user_career,
        history=history)


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
    if current_user.role != 'admin':
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
    """Live Feed aller Aktivitäten."""
    db = get_db()
    if current_user.role == 'admin':
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
    return render_template('coaching.html',
        member=dict(member), career=career, next_career=next_career,
        own_eh=own_eh,
        stats={'termine': total_termine, 'vertraege': total_vertraege, 'leads': total_leads,
               'pending_research': pending_research, 'avg_termine_per_close': avg_termine_per_close,
               'downline_count': downline_count, 'full_team': full_team, 'ob_score': ob_score},
        recent_activity=recent_activity, notes=notes, tipps=tipps,
        heatmap=heatmap, konv_starter=konv_starter)


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


@app.route('/namensliste')
@login_required
def namensliste():
    """Eigene Namensliste / Potenziale (nur die eigenen)"""
    db = get_db()
    rows = db.execute('SELECT * FROM leads WHERE owner_id = ? ORDER BY created_at DESC', (current_user.id,)).fetchall()

    # Stats
    total = len(rows)
    by_status = {}
    for r in rows:
        by_status[r['status']] = by_status.get(r['status'], 0) + 1

    db.close()
    return render_template('namensliste.html', leads=rows, total=total, by_status=by_status)


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


# DB-Initialisierung läuft IMMER beim Modul-Laden (auch bei gunicorn in Production)
init_db()


if __name__ == '__main__':
    print("\n" + "="*60)
    print("✅ NT Pro Academy – Control Hub gestartet!")
    print("="*60)
    print("🌐 Öffne: http://localhost:5001")
    print("📧 Admin Login: najib@ntpro.de")
    print("🔑 Passwort: admin123")
    print("="*60 + "\n")
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=debug, host='0.0.0.0', port=port)
