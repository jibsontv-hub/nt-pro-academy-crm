# NT Pro Academy CRM — Claude Code Guide

Diese Datei lädt jede Claude-Code-Session automatisch. Sie ist die persistente Erinnerung an das **JIBSON BUILD SYSTEM** für dieses Repo.

---

## 🎯 Repo-Profil

**Was:** Strukturvertriebs-CRM für Ergo Pro — multi-tier (Stufe 1 REP / Stufe 2 LREP / Stufe 3 HREP+).
**Owner:** Najib „Jibson" Tchatikpi · Org-Ziel: 5000 EH/Monat persönlich, Skalierung auf ~80 Partner.
**Stack:** Flask 3.1.3 + Flask-Login + SQLite + Vanilla JS (kein Build-Step). PWA + Web Push (VAPID via pywebpush). Hosted: PythonAnywhere.
**Brand:** Premium **Navy/Gold** (Apple/Linear/Stripe-Stil). KEIN reines Grau-Theme — das ist bewusst, nicht weghauen.
**Mobile-first:** PWA mit Service-Worker (`/sw.js`), iOS-Safe-Area überall, Touch-Targets ≥44px.

---

## 🤖 Das Sub-Agent-System (9 Agenten)

Volle Doku: `docs/SUBAGENTS.md`. Kurzfassung:

**Original 5** — werden bei größeren Features durchgegangen:
1. 🎤 Discovery · 2. 🔍 Research · 3. 💬 Positioning · 4. 🎨 Design · 5. ⚙ Engineering

**Spezialisten** — laufen automatisch vor jedem Push:
6. 🛡 **QA-Agent** → `scripts/qa_audit.py` (alle Routes mit Status-Code)
7. 📱 **Mobile-Agent** → CSS-Audit Touch-Targets + Breakpoints
8. 👤 **Human-Walkthrough** → `scripts/journey_test.py` (Anonymous + Admin + API)
9. 🎯 **Vertriebs-Agent** → `scripts/vertrieb_test.py` (Lead → Termin → Vertrag → EH → Provision · 27 Schritte)

---

## ✅ Pre-Push-Regel (HART)

**Vor jedem `git push`** — vor allem wenn Sales/Lead/Vertrag/Webhook/Sidebar/Dashboard berührt wurde:

```bash
bash scripts/pre_push.sh                          # gegen frisch gestarteten lokal-Server
bash scripts/pre_push.sh https://prod-url         # gegen LIVE
```

Das Script fährt Flask hoch, lässt alle 3 Spezialisten-Agenten durchlaufen, räumt auf. Exit 0 = grün.

**Wenn rot:** NICHT pushen, Issue oben fixen, neu testen.

Wenn die Änderung nur Text/Doku/CSS-Catch-Alls war, reicht: `python3 scripts/qa_audit.py http://localhost:5050` (Agent 1 allein).

---

## 🚀 Standard-Deploy auf PythonAnywhere

```bash
cd ~/nt-pro-academy-crm && git pull && touch /var/www/proacademy_pythonanywhere_com_wsgi.py
```

Wird mehrfach pro Session erwähnt — kennt der User auswendig, aber lass es im Done-Summary mitlaufen wenn relevant.

---

## 🧠 Architektur-Quirks (NICHT vergessen)

- **`commissions.user_id`** (NICHT `earner_id`) — historische Bug-Quelle
- **`user_achievements.achievement_code`** (NICHT `code`)
- **`users.created_at` existiert NICHT** — bei neuem Code, der das brauchen würde, fallback auf `MIN(completed_at) FROM onboarding_roadmap` oder `today()`
- **`contracts`** (DB) ↔ `vertraege` (Route/UI) — Naming ist asymmetrisch
- **`appointments`** (DB) ↔ `termine` (Route/UI)
- **`leads.liste_typ`** wurde via `ALTER TABLE` nachgezogen — `vk` (Vertrieb) oder `rk` (Recruiting), Default `vk`
- **Cache-Helper** (`cache_get`/`cache_set`/`cache_invalidate`) — bei DB-Mutation in Sales-Flows IMMER `cache_invalidate()` für die betroffenen Prefixes (`ctx:`, `news:`, `coach_acts:`, `forecast:`, `strang:`, `adm_pers:`, `recent:`)
- **`feature_tier` context_processor** — 1=REP / 2=LREP / 3=HREP+ — Sidebar-Sichtbarkeit hängt dran
- **EH-Faktor:** 1 € Volumen = 0,8 EH (`EH_FAKTOR = 0.8`)
- **Push-Type-Filter:** `send_push_to_user(..., push_type='...')` — User können einzelne Kategorien abschalten via `/push-settings`. Neue Push-Calls IMMER mit `push_type` setzen, sonst rutscht's durch alle Filter
- **Self-Visit-Skip:** `record_partner_view(visitor_id, viewed_id)` — wenn `visitor == viewed`, keine Pin-Bar-Spur (sonst sieht der User sich selbst in „Zuletzt geöffnet")

---

## 🎨 UX-Prinzipien (vom User mehrfach bestätigt)

- **Stufe 1 (REP-Starter)** kriegt nur **8 flache Sidebar-Items** — keine `<details>`-Klapp-Sektionen. Lärm vermeiden.
- **Stufe 2+** kriegt CAPS-Sections (LearningSuite-Style) statt klickbaren Klapp-Headern.
- **Mobile:** Kein `backdrop-filter` <768px (Performance), Animationen ≤0.6s, alle Bilder mit `loading="lazy" decoding="async"`.
- **Theme:** Catch-All-CSS in `base.html` mappt hartcodierte `#fff`/`#faf6ec`-Inline-Styles auf `var(--surface)`/`var(--bg-2)` im Dark Mode. Wenn neuer Code Inline-Hex-Color einführt → vorher prüfen ob's Catch-All schon greift.

---

## 🔁 Häufige Befehle

```bash
# Server lokal starten
FLASK_DEBUG=1 python3 app.py
# → http://localhost:5050

# Smoke-Test-Subset (schnell)
python3 scripts/qa_audit.py http://localhost:5050

# Volle Verify (langsamer, vor Push)
bash scripts/pre_push.sh

# Live deployen
cd ~/nt-pro-academy-crm && git pull && touch /var/www/proacademy_pythonanywhere_com_wsgi.py

# Live-Verify gegen PA
bash scripts/pre_push.sh https://proacademy.pythonanywhere.com
```

---

## 🚫 Was NIE tun

- **Niemals** das Premium-Navy/Gold-Branding gegen reines Grau tauschen — das ist sein Differenzierungsmerkmal
- **Niemals** den `<details>`-Klapp-Sidebar wieder einführen für Stufe 2+ (User hat explizit flat verlangt)
- **Niemals** ohne `cache_invalidate()` ein DB-Write committen, wenn der Wert in einem gecachten Helper landen könnte
- **Niemals** ohne `push_type=` ein `send_push_to_user()` aufrufen (User-Präferenzen werden umgangen)
- **Niemals** Datenschutz/Permission-Checks in `/partner/<uid>/profil` o.ä. wegoptimieren — Berechtigung läuft über `current_user.has_admin_access OR uid == current_user.id OR uid in get_all_descendants(current_user.id)`

---

## 📝 Commit-Stil

Deutsch, beschreibend, keine emojis im Subject (im Body OK). Beispiele aus dem Repo:
- `Tempo-Sprint v2: Stufe-1-Sidebar minimal + Cache-Headers + Mobile-Polish`
- `9. Vertriebs-Agent: End-to-End Sales-Pipeline-Tester`
- `Robustness-Sweep: Sub-Team fängt jetzt alle internen Helper-Crashes ab`

Commit-Messages erklären **warum**, nicht nur was. Wenn Sub-Agenten gegrünt haben, das im Body erwähnen (`Local-Run: 27/27 grün`).
