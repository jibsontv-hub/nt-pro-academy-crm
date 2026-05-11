"""
📧 EMAIL-E2E-AGENT — Real-Send-Verifier

Prüft ob Mails WIRKLICH rausgehen (nicht nur "ok im Log").
Triggert echte Aktionen (Reset-Anfrage, neue Public Lead) und checkt:
1. Reset-Token in DB angelegt
2. email_log status='ok' (kein 'fail', kein 'blocked')
3. Whitelist greift bei nicht-erlaubten Kategorien
4. SMTP-Settings konfiguriert

Run:
  QA_USER=mail QA_PASS=pw python3 scripts/email_e2e_test.py [base_url]

Exit 0 wenn alles OK, sonst 1 mit Error-Liste.
"""
import sys, os, time, sqlite3, requests
from urllib.parse import urljoin

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:5050'
QA_USER = os.environ.get('QA_USER', '')
QA_PASS = os.environ.get('QA_PASS', '')
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'vertrieb.db')

results = []  # (step, ok, detail)


def step(name, ok, detail=''):
    sym = '✅' if ok else '❌'
    results.append((name, ok, detail))
    print(f'  {sym} {name}{(" — " + detail) if detail else ""}')
    return ok


def get_db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def main():
    print(f'\n📧 EMAIL-E2E-AGENT gegen {BASE}')
    print('═' * 60)

    if not (QA_USER and QA_PASS):
        step('QA-Credentials', False, 'QA_USER/QA_PASS nicht gesetzt')
        return 1

    # ─── 1. SMTP-Konfig prüfen ───
    print('\n1️⃣  SMTP-KONFIGURATION')
    db = get_db()
    smtp_keys = ('smtp_host', 'smtp_port', 'smtp_user', 'smtp_password')
    smtp_ok = True
    for k in smtp_keys:
        r = db.execute("SELECT value FROM app_settings WHERE key=?", (k,)).fetchone()
        v = r['value'] if r else None
        ok = bool(v)
        smtp_ok = smtp_ok and ok
        step(f'{k} gesetzt', ok, '' if ok else 'fehlt!')

    if not smtp_ok:
        step('SMTP komplett', False, 'STOP — SMTP nicht konfiguriert')
        db.close()
        return 1

    # ─── 2. Mail-Whitelist prüfen ───
    print('\n2️⃣  MAIL-WHITELIST')
    r = db.execute("SELECT value FROM app_settings WHERE key='mail_categories_allowed'").fetchone()
    whitelist = (r['value'] if r else 'signup,password_init,password_reset,admin_test').split(',')
    whitelist = [c.strip() for c in whitelist]
    expected_min = {'password_reset', 'signup', 'password_init'}
    missing = expected_min - set(whitelist)
    step('Reset/Signup in Whitelist', not missing,
         f'fehlende: {",".join(missing)}' if missing else f'aktiv: {",".join(whitelist)}')

    # ─── 3. Echter Reset-Trigger ───
    print('\n3️⃣  RESET-FLOW (echter Send)')
    sess = requests.Session()
    log_count_before = db.execute("SELECT COUNT(*) c FROM email_log").fetchone()['c']

    r = sess.post(urljoin(BASE, '/passwort-vergessen'),
                  data={'method': 'email', 'email': QA_USER},
                  timeout=30, allow_redirects=False)
    step('POST /passwort-vergessen', r.status_code in (200, 302), f'HTTP {r.status_code}')

    # ─── 4. Token in DB? ───
    time.sleep(1)
    rec = db.execute("""SELECT pr.id, pr.token, pr.method, pr.created_at, u.email
                        FROM password_resets pr JOIN users u ON pr.user_id=u.id
                        WHERE LOWER(u.email)=LOWER(?)
                        ORDER BY pr.id DESC LIMIT 1""", (QA_USER,)).fetchone()
    step('Reset-Token in password_resets', rec is not None,
         f'Token={rec["token"][:18]}…' if rec else 'NICHT angelegt')

    # ─── 5. Email-Log-Eintrag ───
    log_count_after = db.execute("SELECT COUNT(*) c FROM email_log").fetchone()['c']
    new_logs = log_count_after - log_count_before
    step('email_log neuer Eintrag', new_logs >= 1, f'{new_logs} neue Logs')

    last_log = db.execute("""SELECT recipient, subject, status, error
                             FROM email_log
                             WHERE LOWER(recipient)=LOWER(?)
                             ORDER BY id DESC LIMIT 1""", (QA_USER,)).fetchone()
    if last_log:
        step('email_log status=ok', last_log['status'] == 'ok',
             f'status={last_log["status"]}, err={last_log["error"][:80] if last_log["error"] else "—"}')
        step('Subject ist Reset-Mail', 'asswort zur' in (last_log['subject'] or ''),
             f'subject="{last_log["subject"][:50]}"')

    # ─── 6. Whitelist enforcement: Reminder muss BLOCKED werden ───
    print('\n4️⃣  WHITELIST-ENFORCEMENT (Reminder blocked?)')
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from app import send_email
        ok_send, err_send = send_email('blackhole@invalid.de', 'TEST-REMINDER',
                                       'test', sent_by=None, category='reminder')
        step('Reminder-Mail wird blockiert', not ok_send and 'eaktiviert' in (err_send or ''),
             f'ok={ok_send}, err={err_send[:60] if err_send else "—"}')
    except Exception as e:
        step('Whitelist-Test', False, f'EXCEPTION: {e}')

    # ─── 7. Blocked-Eintrag im Log ───
    blocked_log = db.execute("""SELECT subject FROM email_log
                                WHERE status='blocked' ORDER BY id DESC LIMIT 1""").fetchone()
    step('Blocked-Mail in Log dokumentiert', blocked_log is not None,
         f'last blocked: {blocked_log["subject"][:50] if blocked_log else "—"}')

    db.close()

    # ─── REPORT ───
    print('\n' + '═' * 60)
    total = len(results)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f'📧 EMAIL-E2E-REPORT: {total-failed}/{total} OK')
    if failed:
        print('\n❌ FAILS:')
        for n, ok, d in results:
            if not ok:
                print(f'   • {n}: {d}')
        return 1
    print('🟢 ALLE EMAIL-CHECKS GRÜN')
    return 0


if __name__ == '__main__':
    sys.exit(main())
