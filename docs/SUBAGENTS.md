# 🤖 SUB-AGENT-SYSTEM — NT Pro Academy CRM

Erweiterung zum JIBSON BUILD SYSTEM Master-Prompt.
**Scope:** ausschließlich CRM-App (alles andere → eigene Prompts).

## Die ursprünglichen 5 Subagents
1. 🎤 Discovery-Agent (Interviewer)
2. 🔍 Research-Agent
3. 💬 Positioning-Agent
4. 🎨 Design-Agent
5. ⚙ Engineering-Agent

## 4 zusätzliche Spezialisten (Sub-Sub-Agents)

### 🛡 6. QA-AGENT (Auto-Tester)
**Wann aktiv:** VOR jedem Push. Im Workflow nach Engineering, vor Final-Delivery.

**Aufgabe:** Alle Routes der App durchgehen. Listet:
- ✅ Routes die 200/302 liefern
- ⚠ Routes die 4xx liefern (Permission-Issues etc.)
- 🔴 Routes die 5xx liefern (Bugs!)
- Tote Links (Templates verweisen auf nicht-existente Routes)

**Tool:** `scripts/qa_audit.py` → curl + Status-Tabelle

### 📱 7. MOBILE-AGENT (Touch & Performance)
**Wann aktiv:** Bei jedem Frontend-Push. Parallel zu Design-Agent.

**Aufgabe:**
- Touch-Targets prüfen (≥44px auf Mobile)
- Breakpoints checken (320/375/414/768)
- Lighthouse-ähnlicher Performance-Score
- iOS Safari Quirks dokumentieren

### 👤 8. HUMAN-WALKTHROUGH-AGENT (Journey-Tester)
**Wann aktiv:** Nach Engineering, vor Final-Delivery. Simuliert echte User.

**Aufgabe:** Strukturierte User-Stories durchgehen wie ein menschlicher User. Pro Story:
- Login-Schritt
- Klick-Pfad-Schritte
- Erwartetes Resultat
- ✅ erreicht / ❌ blockiert (mit Grund)

**Standard-Journeys (immer):**
1. **Neuer Stufe-1-Partner:** Registrierung → Bestätigung → erster Login → Catch-Up → Dashboard → erster Lead → erster Termin
2. **HREP-Führungskraft:** Login → Dashboard → Inaktiv-Check → Partner-Profil → Sub-Kalender
3. **Admin:** Login → Genehmigung pending → Push-Broadcast → Vorschläge-Inbox
4. **Recruiting-Flow:** RK-Lead anlegen → kontaktieren → Partner einstellen → RK-Lead auto-gewonnen
5. **Vertriebs-Flow:** VK-Lead → Termin → Vertrag → EH zählt + Provision
   → ab jetzt vom **Vertriebs-Agent** (Punkt 9 unten) abgedeckt

**Tool:** `scripts/journey_test.py` — Schritt-für-Schritt-Simulation

### 🎯 9. VERTRIEBS-AGENT (Sales-Pipeline-Tester)
**Wann aktiv:** VOR jedem Push der Vertriebs-Logik berührt (Leads, Termine,
Verträge, Provisionen, Webhooks, Quoten). Liefert die Garantie: „der Verkauf
funktioniert end-to-end".

**Aufgabe:** Vollständigen Sales-Flow simulieren wie ein echter Berater am
Schreibtisch — und jeden Übergang validieren:

1. VK-Lead anlegen (Namensliste)
2. Lead-Status hochsetzen (neu → kontakt → angebot → gewonnen)
3. Termin koppeln (Kundentermin + Datum)
4. Vertrag mit Volumen anlegen + EH-Berechnung verifizieren
   (5000 € × 0,8 EH/€ = 4000 EH muss in der Liste stehen)
5. Tracking + Provisionen + Dashboard rendern (kein 500)
6. Webhook-Token erzeugen → externer POST → Lead landet in Liste
7. Parallele RK-Liste: Trennung VK ↔ RK sauber (kein Kreuz)
8. Quoten + Ziele + Ranking laden ohne Fehler
9. Cleanup: alle Test-Records (markiert mit `VAGENT-{timestamp}`) wieder weg

**Tool:** `scripts/vertrieb_test.py` — End-to-End mit `requests.Session`,
generiert eindeutige Marker pro Run und räumt selbst auf.

**Run-Befehl:**
```bash
QA_USER=najib@ntpro.de QA_PASS=admin123 python3 scripts/vertrieb_test.py http://localhost:5050
```

**Exit-Codes:** 0 = alles grün, 1 = mindestens 1 Schritt rot, 2 = Login fehlte.
