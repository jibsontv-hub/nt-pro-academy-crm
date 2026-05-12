"""
📰 NEWSLETTER-AGENT — Branchen-News-Crawler

Holt aktuelle Themen aus dem Netz die für Vertrieb (Versicherung/Rente/Finanzen)
relevant sind. Schreibt in newsletter_items-Tabelle. Idempotent: bestehende
URLs werden geskippt.

Quellen (RSS, kein API-Key nötig):
  - Bundesbank Pressemitteilungen (Leitzins, Geldpolitik)
  - DRV Bund (Renten-News)
  - Bundesagentur für Arbeit (Arbeitsmarkt)
  - Versicherungswirtschaft-heute / GDV
  - Tagesschau Wirtschaft (Querverweis)

Run:
  python3 scripts/newsletter_agent.py [--limit 30]

Setup als Cron (1× täglich):
  0 7 * * *  cd ~/nt-pro-academy-crm && python3 scripts/newsletter_agent.py
"""
import sys, os, sqlite3, re
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from xml.etree import ElementTree as ET
from datetime import datetime

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'vertrieb.db')
LIMIT_PER_FEED = 10

FEEDS = [
    # (kategorie, label, url)
    ('Leitzins',     'EZB Pressemitteilungen',          'https://www.ecb.europa.eu/rss/press.html'),
    ('Leitzins',     'Bundesbank Aktuelles',            'https://www.bundesbank.de/service/rss/de/aktuelles-rss-633286'),
    ('Rente',        'DRV Bund News',                   'https://www.deutsche-rentenversicherung.de/SharedDocs/RSSFeed/DE/Aktuelles/aktuelles_inhalt.html'),
    ('Arbeitsmarkt', 'Bundesagentur für Arbeit',        'https://www.arbeitsagentur.de/rss/presse'),
    ('Wirtschaft',   'Tagesschau Wirtschaft',           'https://www.tagesschau.de/wirtschaft/index~rss2.xml'),
    ('Versicherung', 'GDV (Versicherer)',               'https://www.gdv.de/gdv/medien/medieninformationen/feed.rss'),
]

# Keywords die einen Treffer als RELEVANT markieren (höhere relevanz)
RELEVANCE_KEYWORDS = [
    'leitzins', 'zinssenkung', 'zinserhöhung', 'inflation', 'rente', 'rentenversicherung',
    'arbeitslos', 'kurzarbeit', 'beschäftigung', 'lebensversicherung', 'altersvorsorge',
    'pensionskasse', 'riester', 'rürup', 'etf', 'pension', 'demographie', 'demografisch',
    'bav', 'betriebliche altersvorsorge', 'gesetzliche rente', 'steuer', 'mindestlohn',
]


def fetch_feed(url, timeout=15):
    """Robust RSS/Atom-Fetch — gibt list of dicts zurück."""
    try:
        req = Request(url, headers={'User-Agent': 'NTPro-Newsletter/1.0'})
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (URLError, HTTPError, TimeoutError, OSError) as e:
        print(f'  ⚠ {url}: {type(e).__name__}: {e}')
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        print(f'  ⚠ {url}: ParseError {e}')
        return []
    items = []
    # RSS 2.0
    for item in root.iter('item'):
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        desc = (item.findtext('description') or '').strip()
        pub = (item.findtext('pubDate') or '').strip()
        if title and link:
            items.append({'title': title, 'url': link, 'desc': clean_html(desc)[:400], 'published': pub})
    # Atom
    if not items:
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        for entry in root.iter('{http://www.w3.org/2005/Atom}entry'):
            title = (entry.findtext('atom:title', namespaces=ns) or '').strip()
            link_el = entry.find('atom:link', namespaces=ns)
            link = (link_el.get('href') if link_el is not None else '') or ''
            summary = (entry.findtext('atom:summary', namespaces=ns)
                       or entry.findtext('atom:content', namespaces=ns) or '').strip()
            pub = (entry.findtext('atom:updated', namespaces=ns)
                   or entry.findtext('atom:published', namespaces=ns) or '').strip()
            if title and link:
                items.append({'title': title, 'url': link, 'desc': clean_html(summary)[:400], 'published': pub})
    return items[:LIMIT_PER_FEED]


def clean_html(s):
    s = re.sub(r'<[^>]+>', '', s or '')
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def relevance_score(title, desc):
    text = (title + ' ' + (desc or '')).lower()
    hits = sum(1 for kw in RELEVANCE_KEYWORDS if kw in text)
    return min(10, 5 + hits)


def normalize_pub_date(s):
    if not s: return None
    for fmt in ('%a, %d %b %Y %H:%M:%S %z', '%a, %d %b %Y %H:%M:%S GMT', '%Y-%m-%dT%H:%M:%S%z',
                '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d'):
        try:
            return datetime.strptime(s.strip(), fmt).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass
    return None


def main():
    print('📰 NEWSLETTER-AGENT — fetch start')
    print('═' * 60)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    new_count = 0
    skip_count = 0
    fail_count = 0

    for kategorie, label, url in FEEDS:
        print(f'\n[{kategorie}] {label}')
        items = fetch_feed(url)
        if not items:
            fail_count += 1
            print(f'  ⚠ keine Items')
            continue
        for it in items:
            try:
                rel = relevance_score(it['title'], it['desc'])
                pub = normalize_pub_date(it['published'])
                cur = db.execute('''INSERT OR IGNORE INTO newsletter_items
                                    (kategorie, titel, zusammenfassung, quelle, quelle_url, relevanz, published_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                                (kategorie, it['title'][:300], it['desc'], label,
                                 it['url'][:500], rel, pub))
                if cur.rowcount > 0:
                    new_count += 1
                    print(f'  ✓ neu: {it["title"][:70]}')
                else:
                    skip_count += 1
            except Exception as e:
                print(f'  ✗ insert-fail: {e}')

    db.commit()

    # Optional: 1 Sammel-Push wenn neue Items kamen
    push_sent = 0
    if new_count > 0 and '--no-push' not in sys.argv:
        # Top-3 hochrelevante neue Items ziehen für Push-Body
        recent = db.execute('''SELECT titel, kategorie FROM newsletter_items
                               WHERE pushed=0 ORDER BY relevanz DESC, id DESC LIMIT 3''').fetchall()
        push_title = f'{new_count} neue Branchen-News'
        if recent:
            sample = ' · '.join(f'{r["titel"][:50]}' for r in recent[:2])
            push_body = sample[:160]
        else:
            push_body = 'Frische Updates aus Versicherung/Rente/Wirtschaft.'
        # Mark as pushed
        db.execute('UPDATE newsletter_items SET pushed=1 WHERE pushed=0')
        db.commit()
        # Send via app.send_push_to_user
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            from app import send_push_to_user
            users = db.execute('''SELECT DISTINCT u.id FROM users u
                                  JOIN push_subscriptions ps ON ps.user_id=u.id
                                  WHERE u.active=1''').fetchall()
            for u in users:
                try:
                    if send_push_to_user(u['id'], title=push_title, body=push_body,
                                         url='/newsletter', push_type='newsletter',
                                         tag=f'newsletter-{date.today().isoformat()}'):
                        push_sent += 1
                except Exception:
                    pass
        except Exception as e:
            print(f'  ⚠ Push-Import fail: {e}')
    db.close()

    print('\n' + '═' * 60)
    print(f'📰 FERTIG · {new_count} neu · {skip_count} schon da · {fail_count} feed-fail · {push_sent} Push gesendet')
    return 0 if new_count + skip_count > 0 else 1


if __name__ == '__main__':
    sys.exit(main())
