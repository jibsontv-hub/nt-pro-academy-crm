from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import sqlite3
import hashlib
import os
from datetime import date, datetime
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
    {'level': 1, 'name': 'Repräsentant',           'short': 'REP',  'min_eh': 0,     'commission': 5.00,  'color': '#94a3b8'},
    {'level': 2, 'name': 'Leitender Repräsentant', 'short': 'LREP', 'min_eh': 1000,  'commission': 9.50,  'color': '#3b82f6'},
    {'level': 3, 'name': 'Hauptrepräsentant',      'short': 'HREP', 'min_eh': 3500,  'commission': 14.00, 'color': '#8b5cf6'},
    {'level': 4, 'name': 'Chefrepräsentant',       'short': 'CREP', 'min_eh': 9000,  'commission': 18.00, 'color': '#10b981'},
    {'level': 5, 'name': 'Direktionsrepräsentant', 'short': 'DREP', 'min_eh': 25000, 'commission': 20.70, 'color': '#c08a2e'},
    {'level': 6, 'name': 'Generalrepräsentant',    'short': 'GREP', 'min_eh': 60000, 'commission': 23.00, 'color': '#92400e'},
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
    return hashlib.sha256(pw.encode()).hexdigest()


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
    db.close()


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
    """Stellt aktuelle Karriere-Stufe + Pending-Anzahl in allen Templates bereit."""
    if current_user.is_authenticated:
        ctx = {'my_career': get_career_level_for_user(current_user.id)}
        if current_user.role == 'admin':
            db = get_db()
            cnt = db.execute('SELECT COUNT(*) as c FROM users WHERE pending_career_level IS NOT NULL AND active = 1').fetchone()['c']
            db.close()
            ctx['pending_count'] = cnt
        return ctx
    return {}


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
        db.execute('''UPDATE users SET manual_career_level = ?,
                      pending_career_level = NULL, pending_by_user_id = NULL, pending_at = NULL
                      WHERE id = ?''', (user['pending_career_level'], uid))
        db.commit()
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
            active INTEGER DEFAULT 1,
            FOREIGN KEY (parent_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
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
    ''')

    # Migration: Spalte 'vision' für bestehende DBs nachrüsten
    try:
        cols = db.execute("PRAGMA table_info(users)").fetchall()
        col_names = [c['name'] for c in cols]
        if 'vision' not in col_names:
            db.execute("ALTER TABLE users ADD COLUMN vision TEXT DEFAULT ''")
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
        'career': get_career_level(own_eh),
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
        db.close()
        if row and row['password'] == hash_password(password):
            login_user(User(row))
            session['show_vision'] = True  # Vision-Modal beim Dashboard anzeigen
            return redirect(url_for('dashboard'))
        flash('Falsche E-Mail oder Passwort', 'error')
    return render_template('login.html')


@app.route('/profil', methods=['GET', 'POST'])
@login_required
def profil():
    """Eigenes Profil — jeder darf seine Vision + Passwort selbst ändern."""
    db = get_db()
    if request.method == 'POST':
        vision = request.form.get('vision', '').strip()
        new_password = request.form.get('password', '').strip()
        if new_password:
            db.execute('UPDATE users SET vision=?, password=? WHERE id=?',
                       (vision, hash_password(new_password), current_user.id))
        else:
            db.execute('UPDATE users SET vision=? WHERE id=?',
                       (vision, current_user.id))
        db.commit()
        db.close()
        flash('Profil aktualisiert!', 'success')
        return redirect(url_for('profil'))
    user = db.execute('SELECT * FROM users WHERE id = ?', (current_user.id,)).fetchone()
    db.close()
    return render_template('profil.html', user=user)


@app.route('/api/vision-seen', methods=['POST'])
@login_required
def vision_seen():
    session.pop('show_vision', None)
    return jsonify({'ok': True})


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
        top_performer = []
        for r in top_rows:
            d = dict(r)
            d['career'] = get_career_level(r['einheiten'])
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
            d['career'] = get_career_level(r['einheiten'])
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
            vision_text=admin_vision, show_vision=admin_show_vision
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
            d['career'] = get_career_level(r['einheiten'])
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
        return render_template('dashboard_partner.html',
            stats=stats, my_leads=my_leads, my_appointments=my_appointments,
            direct_team=direct_team, quota=quota,
            own_eh=own_eh, team_eh=team_eh, career=career, next_level=next_level,
            progress_pct=progress_pct, eh_to_next=eh_to_next, all_levels=CAREER_LEVELS,
            conversion=conversion, termine_pro_abschluss=TERMINE_PRO_ABSCHLUSS,
            my_commissions=my_commissions, global_top=global_top,
            monthly_data=json.dumps([dict(r) for r in monthly_data]),
            vision_text=vision_text, show_vision=show_vision
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
        db.execute('INSERT INTO leads (owner_id, name, email, phone, produkt, status, notizen) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (current_user.id, request.form['name'], request.form.get('email', ''),
             request.form.get('phone', ''), request.form.get('produkt', ''),
             request.form.get('status', 'neu'), request.form.get('notizen', '')))
        db.commit()
        db.close()
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
        db.execute('UPDATE leads SET name=?, email=?, phone=?, produkt=?, status=?, notizen=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
            (request.form['name'], request.form.get('email', ''), request.form.get('phone', ''),
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
        d['career'] = get_career_level(r['einheiten'])
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
            db.execute('''INSERT INTO users (name, email, password, role, parent_id, level, phone,
                          manual_career_level, pending_career_level, pending_by_user_id, pending_at)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (request.form['name'], email, hash_password(request.form.get('password', 'start123')),
                 'partner', parent_id, new_level, request.form.get('phone', ''),
                 manual_level, pending_level, pending_by, pending_at))
            db.commit()
            db.close()
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
        d['career'] = get_career_level(own_eh)
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
