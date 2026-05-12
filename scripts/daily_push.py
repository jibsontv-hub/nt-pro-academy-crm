"""
🔔 DAILY-PUSH-AGENT — täglicher motivierender Push pro User

Schickt jedem aktiven User mit Push-Subscription eine personalisierte
Nachricht mit:
  · KPIs (own EH, fehlend zur nächsten Stufe)
  · Anzahl Grundseminar-Teilnehmer in eigener Struktur (RK-Leads gewonnen/angemeldet)
  · Zufälligem Motivations-Spruch
  · Frage nach Anrufen + Terminen (Link zu /daily-checkin)

Idempotent: pro Tag max 1 Push pro User (push_log mit ref_key=daily-checkin-YYYY-MM-DD).

Run:
  python3 scripts/daily_push.py [--dry-run] [--force]

Auto: monitor_loop.sh ruft täglich zwischen 8-10 Uhr auf.
"""
import sys, os, sqlite3, random
from datetime import date

# Ensure we can import from app.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DRY_RUN = '--dry-run' in sys.argv
FORCE = '--force' in sys.argv  # ignore push_log dedup

SPRUECHE = [
    "Jeden Tag ein Schritt — die nächste Stufe kommt nicht aus dem Nichts.",
    "5 Anrufe = 1 Termin. 3 Termine = 1 Vertrag. Mathematik des Erfolgs.",
    "Wer heute nicht startet, verschiebt nur was morgen sowieso passieren muss.",
    "Du bist 3 Anrufe entfernt von einer guten Geschichte.",
    "Niemand wurde reich vom Aufschieben — heute zählt, nicht morgen.",
    "Konstanz schlägt Talent. Jeden Tag dran sein.",
    "Erfolg ist die Summe kleiner Anstrengungen, täglich wiederholt.",
    "Heute ist der beste Tag um den ersten Anruf zu machen.",
    "Termine sind die Währung des Vertriebs — sammel sie.",
    "Deine Downline schaut dir zu. Sei das Beispiel.",
    "Disziplin ist die Brücke zwischen Zielen und Ergebnissen.",
    "Nicht die Stärksten setzen sich durch — die Konstantesten.",
    "Heute Nein zu hören kostet 0. Nicht zu fragen kostet alles.",
    "Action beats anxiety. Mach den Anruf.",
    "Jeder Termin den du nicht legst ist Geld das jemand anderes verdient.",
    "Klein anfangen, groß werden. Heute ein Anruf mehr.",
    "Wer heute zögert, ist morgen frustriert.",
    "Du bist auf dem Weg zur nächsten Stufe — wenn du dich bewegst.",
    "Vertrieb ist Disziplin in Aktion. Nicht warten — anrufen.",
    "Zwei Anrufe vor dem Frühstück — und der Tag gehört dir.",
]


def get_db():
    db = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'vertrieb.db'))
    db.row_factory = sqlite3.Row
    return db


def get_descendants(db, user_id):
    """Rekursiv alle Downline-IDs."""
    out = []
    queue = [user_id]
    seen = {user_id}
    while queue:
        nxt = []
        ph = ','.join('?' * len(queue))
        rows = db.execute(f'SELECT id FROM users WHERE parent_id IN ({ph}) AND active=1', queue).fetchall()
        for r in rows:
            if r['id'] not in seen:
                seen.add(r['id']); out.append(r['id']); nxt.append(r['id'])
        queue = nxt
    return out


def kpi_for_user(db, user_id):
    """Returns dict mit own_eh, next_lvl_short, eh_to_next, grund_count."""
    own_eh = db.execute("""SELECT COALESCE(SUM(einheiten),0) s FROM contracts
                           WHERE owner_id=? AND status='abgeschlossen' AND recherche_status='freigegeben'""",
                        (user_id,)).fetchone()['s'] or 0
    initial = db.execute('SELECT COALESCE(initial_eh,0) s FROM users WHERE id=?', (user_id,)).fetchone()['s']
    own_eh += (initial or 0)
    # Grundseminar-Teilnehmer in eigener Struktur (self + downline) als RK gewonnen/angemeldet
    ids = [user_id] + get_descendants(db, user_id)
    ph = ','.join('?' * len(ids))
    grund_count = db.execute(f"""SELECT COUNT(*) c FROM leads
                                 WHERE owner_id IN ({ph}) AND COALESCE(liste_typ,'vk')='rk'
                                 AND status IN ('gewonnen','angemeldet')""", ids).fetchone()['c']
    # Nächste Stufe: ich behalte simple — wenn EH < 3000 → "LREP", < 12500 → "HREP", sonst "DST"
    # (echte Logik ist in CAREER_LEVELS, aber hier reicht eine Heuristik für die Push-Message)
    targets = [(3000, 'LREP'), (12500, 'HREP'), (25000, 'DST'), (50000, 'GST')]
    next_lvl_short = None
    eh_to_next = 0
    for cap, lbl in targets:
        if own_eh < cap:
            next_lvl_short = lbl
            eh_to_next = cap - own_eh
            break
    return {
        'own_eh': int(own_eh),
        'next_lvl_short': next_lvl_short,
        'eh_to_next': int(eh_to_next),
        'grund_count': grund_count,
    }


def already_pushed_today(db, user_id, ref_key):
    row = db.execute('SELECT id FROM push_log WHERE user_id=? AND push_type=? AND ref_key=?',
                    (user_id, 'daily_motivate', ref_key)).fetchone()
    return row is not None


def main():
    print(f'\n🔔 DAILY-PUSH-AGENT {"(DRY-RUN)" if DRY_RUN else ""}')
    print('═' * 60)
    today_iso = date.today().isoformat()
    ref_key = f'daily-checkin-{today_iso}'

    db = get_db()
    # Nur User mit aktiver Push-Subscription
    users = db.execute('''SELECT DISTINCT u.id, u.name FROM users u
                          JOIN push_subscriptions ps ON ps.user_id=u.id
                          WHERE u.active=1''').fetchall()
    if not users:
        print('Keine User mit Push-Subscription.')
        db.close()
        return 0

    sent = 0
    skipped = 0
    failed = 0

    # send_push_to_user existiert in app.py — lazy import
    try:
        from app import send_push_to_user
    except Exception as e:
        print(f'⚠ Cannot import app.send_push_to_user: {e}')
        return 1

    for u in users:
        # Dedup: bereits heute gepusht?
        if not FORCE and already_pushed_today(db, u['id'], ref_key):
            skipped += 1
            continue
        kpi = kpi_for_user(db, u['id'])
        first_name = (u['name'] or '').split()[0] if u['name'] else 'Hallo'
        spruch = random.choice(SPRUECHE)

        if kpi['next_lvl_short']:
            title = f'{first_name}: noch {kpi["eh_to_next"]:,} EH zur {kpi["next_lvl_short"]}'.replace(',', '.')
        else:
            title = f'{first_name}: bereits Top-Stufe — bleib dran'

        body_parts = [spruch]
        if kpi['grund_count'] > 0:
            body_parts.append(f'{kpi["grund_count"]} Grundsem-Teilnehmer in deiner Struktur.')
        body_parts.append('Wie viele Anrufe + Termine heute?')
        body = ' '.join(body_parts)

        if DRY_RUN:
            print(f'[DRY] {u["name"]:25s} → {title}')
            print(f'      {body[:100]}…')
            sent += 1
            continue
        try:
            ok = send_push_to_user(
                u['id'], title=title, body=body,
                url='/daily-checkin',
                push_type='daily_motivate',
                tag=ref_key,
            )
            if ok:
                sent += 1
                print(f'  ✓ {u["name"]} → {title}')
            else:
                failed += 1
        except Exception as e:
            failed += 1
            print(f'  ✗ {u["name"]}: {e}')

    db.close()
    print('\n' + '═' * 60)
    print(f'🔔 FERTIG · {sent} gesendet · {skipped} schon heute · {failed} fail')
    return 0


if __name__ == '__main__':
    sys.exit(main())
