#!/usr/bin/env bash
# FIND-BEST-BACKUP — listet alle Backups MIT Counts pro Tabelle.
# Damit Najib das richtige Backup zum Restore findet (z.B. das mit den
# meisten Verträgen).
#
# Aufruf: bash scripts/find_best_backup.sh
#
# Output (Beispiel):
#   FILE                                    SIZE   USERS  CONTRACTS  LEADS  TEILNEHMER
#   vertrieb-2026-05-14.db                  604K   42     0          50     3
#   pre-deploy-2026-05-14-150002-abc.db     598K   42     127        48     3   ← BESTES (127 Verträge!)
#   vertrieb-2026-05-13.db                  444K   42     127        48     3
#   vertrieb-2026-05-12.db                  312K   41     115        45     0
set -e
PROJECT=$(cd $(dirname $0)/.. && pwd)
BKP_DIR=$PROJECT/backups
DB=$PROJECT/vertrieb.db

if [ ! -d "$BKP_DIR" ]; then
    echo "❌ Backup-Verzeichnis $BKP_DIR existiert nicht."
    exit 1
fi

echo "═════════════════════════════════════════════════════════════════════════"
echo "  BACKUP-VERGLEICH — finde das mit den meisten Daten"
echo "═════════════════════════════════════════════════════════════════════════"
echo ""
printf "%-50s %6s %6s %10s %6s %6s %10s\n" "FILE" "SIZE" "USERS" "CONTRACTS" "LEADS" "APPTS" "TEILNEHMER"
echo "─────────────────────────────────────────────────────────────────────────"

# Aktuelle DB als Vergleich
if [ -f "$DB" ]; then
    USERS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM users" 2>/dev/null || echo "?")
    CONTRACTS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM contracts" 2>/dev/null || echo "?")
    LEADS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM leads" 2>/dev/null || echo "?")
    APPTS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM appointments" 2>/dev/null || echo "?")
    TEILN=$(sqlite3 "$DB" "SELECT COUNT(*) FROM grundseminar_teilnehmer" 2>/dev/null || echo "?")
    SIZE=$(du -h "$DB" | cut -f1)
    printf "\033[1;33m%-50s %6s %6s %10s %6s %6s %10s\033[0m  \033[33m← AKTUELL\033[0m\n" \
        "vertrieb.db (LIVE)" "$SIZE" "$USERS" "$CONTRACTS" "$LEADS" "$APPTS" "$TEILN"
fi
echo "─────────────────────────────────────────────────────────────────────────"

# Alle Backups durchgehen, nach contracts-count sortieren (höchste zuerst)
declare -a results
for f in "$BKP_DIR"/*.db; do
    [ -f "$f" ] || continue
    [[ "$f" == *.CORRUPT ]] && continue
    fname=$(basename "$f")
    SIZE=$(du -h "$f" | cut -f1)
    USERS=$(sqlite3 "$f" "SELECT COUNT(*) FROM users" 2>/dev/null || echo "?")
    CONTRACTS=$(sqlite3 "$f" "SELECT COUNT(*) FROM contracts" 2>/dev/null || echo "?")
    LEADS=$(sqlite3 "$f" "SELECT COUNT(*) FROM leads" 2>/dev/null || echo "?")
    APPTS=$(sqlite3 "$f" "SELECT COUNT(*) FROM appointments" 2>/dev/null || echo "?")
    TEILN=$(sqlite3 "$f" "SELECT COUNT(*) FROM grundseminar_teilnehmer" 2>/dev/null || echo "0")
    # Sort-Key: contracts (numerisch, padded)
    sort_key=$(printf "%010d" "${CONTRACTS//[!0-9]/0}")
    results+=("$sort_key|$fname|$SIZE|$USERS|$CONTRACTS|$LEADS|$APPTS|$TEILN")
done

# Sortiert nach CONTRACTS absteigend, MAX wird grün markiert
sorted=$(printf '%s\n' "${results[@]}" | sort -r)
max_contracts=$(printf '%s\n' "${results[@]}" | sort -rn -t'|' -k5 | head -1 | cut -d'|' -f5)

echo "$sorted" | while IFS='|' read sk fname size users contracts leads appts teiln; do
    if [ "$contracts" = "$max_contracts" ] && [ "$contracts" != "0" ]; then
        printf "\033[1;32m%-50s %6s %6s %10s %6s %6s %10s\033[0m  \033[32m← MEISTE VERTRÄGE\033[0m\n" \
            "$fname" "$size" "$users" "$contracts" "$leads" "$appts" "$teiln"
    else
        printf "%-50s %6s %6s %10s %6s %6s %10s\n" \
            "$fname" "$size" "$users" "$contracts" "$leads" "$appts" "$teiln"
    fi
done

echo "─────────────────────────────────────────────────────────────────────────"
echo ""
echo "📋 ZUM RESTORE des grünen Files:"
best=$(printf '%s\n' "${results[@]}" | sort -rn -t'|' -k5 | head -1 | cut -d'|' -f2)
echo "   bash scripts/restore_backup.sh $best"
echo ""
echo "💡 ODER via Web-UI: /admin/agents → 🗄 Backups → ↩ Restore-Button"
