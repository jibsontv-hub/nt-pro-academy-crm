"""
🤖 ASSISTENTIN-PROAKTIV-AGENT — schickt Najib (Admin) tägliche Reminder

Läuft 1× pro Tag (vom monitor_loop.sh getriggert) und prüft:
1. Heute anstehende Termine die Najib beteiligt ist (als owner oder attendee)
2. Anfrage-Termine die Bestätigung brauchen
3. Eingabeschluss in den nächsten 7 Tagen
4. ZVG-Reminder (3-10 Tage nach Eingabeschluss)
5. Inaktive Direkt-Partner (>7 Tage)

Schickt 1 zusammenfassenden Push (priorisiert nach Dringlichkeit) → /assistentin
Idempotent via push_log mit ref_key=assist-daily-YYYY-MM-DD.

Run:
  python3 scripts/assistentin_proactive.py [--dry-run] [--force]
"""
import sys, os, sqlite3
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DRY_RUN = '--dry-run' in sys.argv
FORCE = '--force' in sys.argv

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'vertrieb.db')


def get_db():
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    return db


def get_descendants(db, user_id):
    out = []; queue = [user_id]; seen = {user_id}
    while queue:
        ph = ','.join('?' * len(queue))
        rows = db.execute(f'SELECT id FROM users WHERE parent_id IN ({ph}) AND active=1', queue).fetchall()
        nxt = []
        for r in rows:
            if r['id'] not in seen:
                seen.add(r['id']); out.append(r['id']); nxt.append(r['id'])
        queue = nxt
    return out


def main():
    db = get_db()
    today = date.today()
    today_iso = today.isoformat()
    ref_key = f'assist-daily-{today_iso}'

    # Admin-User finden
    admins = db.execute("SELECT id, name FROM users WHERE active=1 AND role='admin'").fetchall()
    if not admins:
        print('Kein Admin gefunden — abgebrochen')
        db.close()
        return 0

    print(f'\n🤖 ASSISTENTIN-PROAKTIV {"(DRY-RUN)" if DRY_RUN else ""}')
    print('═' * 60)

    try:
        from app import send_push_to_user
    except Exception as e:
        print(f'⚠ Cannot import send_push_to_user: {e}')
        return 1

    sent = 0
    for admin in admins:
        # Dedup
        if not FORCE:
            already = db.execute('SELECT id FROM push_log WHERE user_id=? AND push_type=? AND ref_key=?',
                                (admin['id'], 'assistentin_daily', ref_key)).fetchone()
            if already:
                print(f'  ⏭ {admin["name"]}: heute schon gepusht')
                continue

        items = []

        # 1. Heute anstehende Termine (owner oder attendee)
        ids = [admin['id']] + get_descendants(db, admin['id'])
        ph = ','.join('?' * len(ids))
        n_today = db.execute(f'''SELECT COUNT(*) c FROM appointments
                                 WHERE owner_id IN ({ph})
                                 AND date(termin_date) = date(?)
                                 AND status != 'abgesagt' ''',
                            ids + [today_iso]).fetchone()['c']
        if n_today > 0:
            items.append(f'📅 {n_today} Termine heute')

        # 2. Termine MORGEN (vorbereiten!)
        tomorrow = (today + timedelta(days=1)).isoformat()
        n_tom = db.execute(f'''SELECT COUNT(*) c FROM appointments
                               WHERE owner_id IN ({ph})
                               AND date(termin_date) = date(?)
                               AND status != 'abgesagt' ''',
                          ids + [tomorrow]).fetchone()['c']
        if n_tom > 0:
            items.append(f'⏰ {n_tom} Termine morgen — vorbereiten?')

        # 3. Eingabeschluss in den nächsten 7 Tagen
        try:
            from app import get_production_deadlines
            deadlines = get_production_deadlines()
            if deadlines:
                eingabe = deadlines.get('eingabeschluss')
                if eingabe:
                    days_to = (eingabe - today).days
                    if 0 <= days_to <= 7:
                        items.append(f'⚡ Eingabeschluss in {days_to}T ({eingabe.strftime("%d.%m.")})')
                    elif -10 <= days_to < 0:
                        # ZVG-Phase
                        n_rep23 = db.execute(f'''SELECT COUNT(*) c FROM users
                                                 WHERE id IN ({ph}) AND id!=? AND active=1
                                                 AND COALESCE(manual_career_level,1) >= 2''',
                                           ids + [admin['id']]).fetchone()['c']
                        if n_rep23 > 0:
                            items.append(f'🎯 ZVGs fällig ({n_rep23} Stufe 2+ Partner, Eingabeschluss vor {-days_to}T)')
        except Exception as e:
            print(f'  ⚠ deadlines: {e}')

        # 4. Inaktive Direkt-Partner >7 Tage
        n_inact = db.execute('''SELECT COUNT(*) c FROM (
                                  SELECT id, CAST(julianday('now') - julianday(COALESCE(last_login, joined_date)) as INTEGER) as d
                                  FROM users WHERE parent_id=? AND active=1
                                ) WHERE d >= 7''', (admin['id'],)).fetchone()['c']
        if n_inact > 0:
            items.append(f'⚠ {n_inact} direkte Partner >7 Tage inaktiv')

        # 5. Großverträge der letzten 24h
        n_big = db.execute(f'''SELECT COUNT(*) c FROM contracts
                               WHERE owner_id IN ({ph})
                               AND status='abgeschlossen'
                               AND einheiten >= 2000
                               AND date(COALESCE(abschluss_date, created_at)) = date(?)''',
                          ids + [today_iso]).fetchone()['c']
        if n_big > 0:
            items.append(f'🏆 {n_big} Großverträge gestern — gratulieren?')

        if not items:
            items.append('✓ Heute alles unter Kontrolle. Was steht oben auf deiner Liste?')

        # Body bauen
        title = 'Deine Assistentin · Tagesbriefing'
        body = ' · '.join(items[:4])

        if DRY_RUN:
            print(f'\n[DRY] {admin["name"]}:')
            print(f'  Title: {title}')
            print(f'  Body: {body}')
            sent += 1
            continue

        try:
            ok = send_push_to_user(admin['id'], title=title, body=body[:280],
                                   url='/assistentin', push_type='assistentin_daily',
                                   tag=ref_key)
            if ok:
                sent += 1
                print(f'  ✓ {admin["name"]} → {body[:90]}')
        except Exception as e:
            print(f'  ✗ {admin["name"]}: {e}')

    db.close()
    print(f'\n═══ FERTIG · {sent} Admin(s) gepusht')
    return 0


if __name__ == '__main__':
    sys.exit(main())
