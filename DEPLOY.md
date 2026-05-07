# 🚀 Online-Setup mit Render.com

## Schritt 1 — GitHub-Account
Falls noch nicht vorhanden:
1. Gehe auf https://github.com/signup
2. E-Mail + Passwort + Username (z.B. `najib-tchatikpi`)
3. Bestätige deine E-Mail

## Schritt 2 — Code zu GitHub hochladen (über Browser, ohne Terminal)
1. Auf https://github.com → grüner Button **„New"** (Repository erstellen)
2. Repository name: `nt-pro-academy-crm`
3. Wähle **„Private"** (deine Daten/Code bleiben privat)
4. **NICHT** „Initialize with README" anhaken
5. Klick **„Create repository"**
6. Auf der nächsten Seite: Klick **„uploading an existing file"**
7. Im Finder: Öffne `~/ntpro-crm/` (Cmd+Shift+G → `~/ntpro-crm` eingeben)
8. **WICHTIG:** Lade **NICHT** `vertrieb.db`, `app.log`, `app-error.log` hoch (Daten bleiben lokal/auf Render)
9. Wähle ALLE anderen Dateien & Ordner (`app.py`, `templates/`, `static/`, `requirements.txt`, `Procfile`, `render.yaml`, `.gitignore`, `DEPLOY.md`) und ziehe sie auf die GitHub-Seite
10. Unten: „Commit changes" → **Commit directly to the `main` branch** → grüner Button

## Schritt 3 — Auf Render deployen
1. Gehe auf https://render.com → **„Get Started for Free"**
2. **„Sign up with GitHub"** (verbindet dein GitHub-Konto)
3. Bestätige Zugriff auf das Repository
4. Im Render-Dashboard: **„New +"** → **„Web Service"**
5. Wähle dein Repository `nt-pro-academy-crm`
6. **Render erkennt automatisch** die `render.yaml` — alle Einstellungen sind schon da!
7. Wähle Plan:
   - **„Free"** → kostenlos, App schläft nach 15 Min Inaktivität (5 Sek Wartezeit beim ersten Aufruf)
   - **„Starter"** → 7 USD/Monat, läuft 24/7
8. Klick **„Create Web Service"**

⏱️ Build dauert 2-3 Minuten. Dann bekommst du eine URL wie:
👉 **https://nt-pro-academy-control-hub.onrender.com**

## Schritt 4 — Erstes Login
1. URL öffnen
2. Login: `najib@ntpro.de` / `admin123`
3. **Sofort Passwort ändern!** → Team → eigener Eintrag → Bearbeiten → Neues Passwort
4. Auch SECRET_KEY ist auto-generiert (sicher).

## Schritt 5 — Custom Domain (optional)
Falls du eine eigene Domain hast (z.B. `ntpro-control.de`):
1. Im Render-Dashboard → dein Service → „Settings" → „Custom Domain"
2. Domain eingeben → CNAME-Record bei deinem Domain-Provider setzen
3. HTTPS-Zertifikat wird automatisch generiert

---

## 🔄 Code-Updates später
Wenn du etwas ändern willst:
1. Datei in `~/ntpro-crm/` lokal bearbeiten
2. Auf GitHub: Repository öffnen → „Add file" → „Upload files" → neue Version hochladen
3. Render deployt **automatisch** neu (innerhalb 1-2 Min)

## 💾 Daten-Backup (wichtig!)
Auf Render liegt die DB auf der „Persistent Disk" (1 GB). Backup erstellen:
1. Render-Dashboard → Web Service → „Shell" Tab
2. Befehl: `cp /opt/render/project/src/data/vertrieb.db /tmp/backup.db`
3. Kannst dir das File über den Render-Shell zur Sicherheit herunterladen

## 💰 Kostenüberblick
- **Free Plan**: 0 €/Monat (mit Auto-Sleep)
- **Starter Plan**: ~7 USD/Monat (24/7 verfügbar) — empfohlen für 80 Partner
- **Persistent Disk**: 0,25 USD/Monat pro GB

## 🆘 Probleme?
- Logs ansehen: Render-Dashboard → Service → „Logs" Tab
- Restart: Render-Dashboard → Service → „Manual Deploy" → „Clear build cache & deploy"
