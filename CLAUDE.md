# Pro Academy CRM — Claude Code Guide

## 🎯 Repo-Profil
**Was:** Strukturvertriebs-CRM für Ergo Pro · multi-tier (REP / LREP / HREP+).
**Owner:** Najib „Jibson" Tchatikpi · Ziel: 5000 EH/Monat · Skalierung ~80 Partner.
**Stack:** Flask 3.1.3 + Flask-Login + SQLite (WAL-Mode) + Vanilla JS · PWA + Web Push.
**Hosted:** PythonAnywhere · Domain: `proacademy-business.de`.
**Brand:** Premium **Navy/Gold** (NICHT Grau-Theme tauschen).

---

## ⚙ WORKFLOW-REGELN (Boris Cherny / Anthropic Style)

1. **PLAN MODE FIRST** — vor jedem Code: vollständiger Plan, iterieren bis perfekt, dann ein sauberer Shot. Bei Fehler: zurück in Plan, neu planen, neu ausführen. Kein blindes Patchen.
2. **CLAUDE.md ist heilig** — max ~100 Zeilen. Nach JEDEM Fehler hier dokumentieren damit's nie wieder passiert.
3. **Verification Loop „BEWEISE ES"** — bei jeder Änderung: Tests laufen, Vorher/Nachher-Diff, Browser-Test wenn relevant. Erst „fertig" wenn nachweisbar funktioniert.
4. **Sub-Agent-Mindset** — Code Writer → Code Reviewer (Style, Bugs) → Deployer. Nie gleichzeitig.
5. **Simplify nach jedem Build** — doppelter Code? Anti-Patterns? Performance?
6. **Hooks/Auto-Format** — wo möglich automatisches Formatieren konfigurieren.
7. **Learning-Mode bei Unbekanntem** — bei Legacy/Architektur: WARUM erklären, nicht nur WAS.
8. **Direkte Kommunikation** — kein Filler. Zwischenfragen kurz, dann zurück in Flow.
9. **Parallel arbeiten** — Worktrees vorschlagen wenn unabhängige Tasks anstehen.
10. **Autonomie** — wiederkehrende Tasks → Loops vorschlagen.

---

## 🤖 Sub-Agenten (Vor jedem Push: `bash scripts/pre_push.sh`)
**Agenten 1-5** Discovery/Research/Positioning/Design/Engineering — bei Features.
**Agenten 6-10** automatisch via `pre_push.sh` (alle 5 müssen grün sein):
- **QA-Audit** (`scripts/qa_audit.py`) — alle Routes Status-Check
- **Human-Walkthrough** (`scripts/journey_test.py`) — Anonymous + Admin + API
- **Vertriebs-Agent** (`scripts/vertrieb_test.py`) — Lead→Termin→Vertrag→Provision (27 Schritte)
- **Email-E2E** (`scripts/email_e2e_test.py`) — echter Send-Verifier: SMTP-Konfig, Reset-Token, email_log status=ok, Whitelist-Enforcement
- **UI-Audit** (`scripts/ui_audit.py`) — broken buttons, dead links, Session-Expiry, Emoji-Density (>15 unique = Warning), Inline-Style-Density (>100 = Refactor-Hint)

---

## 🚀 Deploy-Befehl auf PythonAnywhere
```bash
cd ~/nt-pro-academy-crm && git pull && touch /var/www/proacademy-business_de_wsgi.py
```

---

## 🧠 Architektur-Quirks (NIE vergessen)
- `commissions.user_id` (nicht `earner_id`) · `user_achievements.achievement_code` (nicht `code`)
- `users.created_at` existiert NICHT — Fallback `MIN(completed_at) FROM onboarding_roadmap`
- `contracts` (DB) ↔ `vertraege` (Route) · `appointments` (DB) ↔ `termine` (Route)
- `leads.liste_typ`: `'vk'` (Vertrieb) oder `'rk'` (Recruiting)
- `EH_FAKTOR = 0.8` (1€ Volumen = 0.8 EH)
- **SQLite-Connections IMMER mit WAL + 30s busy_timeout** (siehe `get_db()`)
- Push-Calls IMMER mit `push_type=` setzen (User-Filter)
- Bei DB-Mutation IMMER `cache_invalidate()` für: `ctx:` `news:` `coach_acts:` `forecast:` `strang:` `adm_pers:` `recent:`
- `feature_tier`: 1=REP, 2=LREP, 3=HREP+ (Sidebar-Sichtbarkeit)
- `record_partner_view(visitor, viewed)` skipt visitor==viewed (kein Self-Pin)

---

## 🎨 UX-Prinzipien
- **Sidebar (alle Stufen + Admin)** = aufklappbare Hauptthemen via `<details class="nav-group" data-group="…">` + `<summary>` + `<div class="nav-group-items">`. Aktive Sektion ist SSR-`open` (Jinja prüft `request.path`), User-Toggle persistiert in `localStorage` (`pa_sidebar_groups_v1`) und überschreibt den Default. Stufe 1: 3 Gruppen (Start/Vertrieb/Lernen) + Profil flat. Stufe 2+: 5 Gruppen. Admin: zusätzlich Council + Administration.
- **Mobile**: kein `backdrop-filter` <768px, Animationen ≤0.6s, alle `<img>` mit `loading="lazy"`
- **Theme-Catch-Alls** in `base.html` mappen Hex-Inline-Styles auf CSS-Vars im Dark-Mode

---

## 🐛 Bug-Library (alle gefixt — NIE wiederholen)
- **UnboundLocalError `admin_personal`** im Dashboard — IMMER alle Variablen die im render_template referenziert werden auch im `else`-Block initialisieren (None ist OK)
- **`database is locked`** bei parallelen Writes — fix: WAL-Mode in `get_db()` + `PRAGMA busy_timeout=30000`
- **Service-Worker-Stau** auf iPhone-PWA → `clients.claim()` + `FORCE_RELOAD`-Message in SW activate
- **Hardcoded `proacademy.pythonanywhere.com`** in Templates → `{{ request.url_root }}` oder `CANONICAL_URL`-Konstante
- **Login-Persistenz** — `session.permanent = True` + `login_user(remember=True, duration=…)` + `REMEMBER_COOKIE_*` config

---

## 🚫 Was NIE tun
- Premium Navy/Gold gegen Grau tauschen
- Sidebar (egal welche Stufe oder Admin) wieder flach machen — User will überall Hauptthemen + Aufklappen
- DB-Write ohne `cache_invalidate()`
- `send_push_to_user()` ohne `push_type=`
- Permission-Checks in `/partner/<uid>/profil` wegoptimieren

---

## 📝 Commit-Stil
Deutsch, beschreibend, Body erklärt **warum** (nicht nur was). Sub-Agenten-Result im Body wenn relevant (`Pre-Push: 139/139 grün`).
