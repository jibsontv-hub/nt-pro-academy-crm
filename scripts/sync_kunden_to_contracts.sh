#!/usr/bin/env bash
# SYNC-KUNDEN-TO-CONTRACTS — wenn nach einem Restore die Kundenliste (leads)
# noch da ist aber Verträge (contracts) weg sind, erzeugt dieses Script
# Vertrags-Stubs aus den Kunden-Leads damit Najib sie in /vertraege sieht.
#
# Filter: leads wo
#   - liste_typ='vk' (Kunden-Lead, nicht Bewerber)
#   - status IN ('abgeschlossen', 'gewonnen', 'kontakt', 'angebot')
#   - kein bestehender contract mit gleichem Namen + Owner
#
# Output: contract-Stub pro Lead mit:
#   - volumen=0, einheiten=0  (Najib ergänzt manuell via /vertraege/<id>/edit)
#   - status='offen'  (zählt NICHT in EH-Berechnung bis Najib editiert)
#   - recherche_status='ausstehend'
#   - lead_id verlinkt
#
# Aufruf:
#   bash scripts/sync_kunden_to_contracts.sh             # DRY-RUN: zeigt was passieren würde
#   bash scripts/sync_kunden_to_contracts.sh --do        # FÜHRT AUS
set -e
PROJECT=$(cd $(dirname $0)/.. && pwd)
DB=$PROJECT/vertrieb.db
DO_RUN=${1:-}

if [ ! -f "$DB" ]; then
    echo "❌ DB $DB nicht gefunden."
    exit 1
fi

# Backup VOR dem Sync (Sicherheit)
if [ "$DO_RUN" = "--do" ]; then
    TS=$(date +%Y%m%d-%H%M%S)
    cp "$DB" "$DB.before-sync-$TS"
    echo "✓ DB gesichert nach: $DB.before-sync-$TS"
    echo ""
fi

# Kandidaten-Query: Kunden-Leads ohne passenden contract
QUERY="
SELECT l.id, l.owner_id, l.name, l.email, l.phone, l.produkt, l.status, l.notizen
FROM leads l
WHERE COALESCE(l.liste_typ, 'vk') = 'vk'
  AND l.status IN ('abgeschlossen','gewonnen','kontakt','angebot')
  AND NOT EXISTS (
    SELECT 1 FROM contracts c
    WHERE c.owner_id = l.owner_id
      AND LOWER(c.client_name) = LOWER(l.name)
  )
ORDER BY l.created_at;
"

CANDIDATES=$(sqlite3 -separator '|' "$DB" "$QUERY")
if [ -z "$CANDIDATES" ]; then
    COUNT=0
else
    COUNT=$(echo "$CANDIDATES" | grep -c '^' | tr -d ' \n')
fi

echo "═════════════════════════════════════════════════════════════════════════"
echo "  KUNDEN → VERTRÄGE SYNC"
echo "═════════════════════════════════════════════════════════════════════════"
echo ""
echo "Filter: leads mit liste_typ='vk' UND status in (abgeschlossen/gewonnen/kontakt/angebot)"
echo "        UND kein contract mit gleichem Name + Owner"
echo ""
echo "Gefunden: $COUNT Kandidaten"
echo ""

if [ "$COUNT" -eq 0 ]; then
    echo "✓ Nichts zu tun. Alle Kunden-Leads haben schon passende Verträge."
    exit 0
fi

echo "─────────────────────────────────────────────────────────────────────────"
printf "%-5s %-30s %-25s %-15s\n" "ID" "NAME" "STATUS" "PRODUKT"
echo "─────────────────────────────────────────────────────────────────────────"
echo "$CANDIDATES" | while IFS='|' read id owner name email phone produkt status notizen; do
    printf "%-5s %-30s %-25s %-15s\n" "$id" "${name:0:30}" "$status" "${produkt:0:15}"
done
echo "─────────────────────────────────────────────────────────────────────────"
echo ""

if [ "$DO_RUN" != "--do" ]; then
    echo "💡 Das war ein DRY-RUN. Zum tatsächlichen Anlegen:"
    echo "   bash scripts/sync_kunden_to_contracts.sh --do"
    echo ""
    echo "📌 Was passiert dann pro Lead:"
    echo "   - Neuer Vertrag mit Status 'offen' (zählt NICHT in EH bis du editierst)"
    echo "   - volumen=0, einheiten=0 → ergänze pro Vertrag via /vertraege"
    echo "   - lead_id wird verlinkt"
    exit 0
fi

# AUSFÜHREN
echo "🔄 Erzeuge $COUNT Vertrags-Stubs..."
INSERT_QUERY="
INSERT INTO contracts (owner_id, client_name, produkt, volumen, einheiten, provision,
                       status, recherche_status, notizen, lead_id, created_at)
SELECT l.owner_id, l.name, COALESCE(l.produkt, ''), 0, 0, 0,
       'offen', 'ausstehend',
       COALESCE(l.notizen, '') || ' [Auto-Sync aus Kundenliste — Volumen/EH bitte ergänzen]',
       l.id, l.created_at
FROM leads l
WHERE COALESCE(l.liste_typ, 'vk') = 'vk'
  AND l.status IN ('abgeschlossen','gewonnen','kontakt','angebot')
  AND NOT EXISTS (
    SELECT 1 FROM contracts c
    WHERE c.owner_id = l.owner_id
      AND LOWER(c.client_name) = LOWER(l.name)
  );
"
sqlite3 "$DB" "$INSERT_QUERY"
NEW_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM contracts WHERE notizen LIKE '%Auto-Sync aus Kundenliste%'")
echo "✓ $NEW_COUNT Vertrags-Stubs erzeugt."

# WSGI touch für Reload (Production)
WSGI=/var/www/proacademy-business_de_wsgi.py
if [ -f "$WSGI" ]; then
    touch "$WSGI"
    echo "✓ WSGI touched → Flask reload"
fi

echo ""
echo "🟢 SYNC FERTIG. Was du jetzt tun solltest:"
echo "   1. Geh auf /vertraege im Browser"
echo "   2. Du siehst alle Stubs als Status 'offen'"
echo "   3. Pro Vertrag: ✎ Bearbeiten → Volumen + EH eingeben → Status auf 'abgeschlossen'"
echo "   4. Recherche-Status auf 'freigegeben' wenn Recherche durch ist"
echo ""
echo "💡 Falls schief: alte DB ist hier:"
echo "   $DB.before-sync-$TS"
