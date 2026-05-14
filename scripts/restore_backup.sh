#!/usr/bin/env bash
# RESTORE-BACKUP — sicher ein Backup zurückspielen.
#
# Macht: 1. aktuelles DB sichern als vertrieb.db.before-restore-{timestamp}
#         2. Backup-File über vertrieb.db kopieren
#         3. Integrity-Check
#         4. WSGI touch
#
# Aufruf:
#   bash scripts/restore_backup.sh                   # listet alle Backups
#   bash scripts/restore_backup.sh <datei>           # restored datei
#   bash scripts/restore_backup.sh latest            # nimmt neueste pre-deploy oder daily
set -e
PROJECT=$(cd $(dirname $0)/.. && pwd)
cd $PROJECT
BKP_DIR=$PROJECT/backups
DB=$PROJECT/vertrieb.db

if [ ! -d "$BKP_DIR" ]; then
    echo "❌ Backup-Verzeichnis $BKP_DIR existiert nicht."
    exit 1
fi

# Liste-Modus
if [ -z "$1" ]; then
    echo "═══ Verfügbare Backups in $BKP_DIR ═══"
    ls -lah "$BKP_DIR" | grep -E '\.db$' | awk '{print "  " $9 "  (" $5 ")  " $6 " " $7 " " $8}'
    echo ""
    echo "Aufruf: bash scripts/restore_backup.sh <datei>"
    echo "      oder: bash scripts/restore_backup.sh latest"
    exit 0
fi

# Latest-Modus: neueste .db ohne CORRUPT
if [ "$1" = "latest" ]; then
    SOURCE=$(ls -1t "$BKP_DIR" | grep -E '\.db$' | grep -v CORRUPT | head -1)
    if [ -z "$SOURCE" ]; then
        echo "❌ Keine Backups gefunden."
        exit 1
    fi
    SOURCE_PATH="$BKP_DIR/$SOURCE"
else
    # Explizite Datei
    SOURCE_PATH="$BKP_DIR/$1"
    if [ ! -f "$SOURCE_PATH" ]; then
        # Vielleicht hat User schon vollen Pfad
        if [ -f "$1" ]; then
            SOURCE_PATH="$1"
        else
            echo "❌ Datei nicht gefunden: $SOURCE_PATH oder $1"
            exit 1
        fi
    fi
fi

echo "═══ RESTORE ═══"
echo "Source: $SOURCE_PATH ($(du -h $SOURCE_PATH | cut -f1))"
echo "Target: $DB"

# 1. Integrity-Check der Source
INT=$(sqlite3 "$SOURCE_PATH" 'PRAGMA integrity_check' | head -1)
if [ "$INT" != "ok" ]; then
    echo "❌ ABBRUCH: Backup ist corrupt: $INT"
    exit 1
fi
echo "✓ Backup-Integrity: ok"

# 2. Aktuelle DB sichern (vor Überschreibung)
TS=$(date +%Y%m%d-%H%M%S)
BEFORE_RESTORE="$DB.before-restore-$TS"
if [ -f "$DB" ]; then
    cp "$DB" "$BEFORE_RESTORE"
    echo "✓ Aktuelle DB gesichert nach: $BEFORE_RESTORE"
fi

# 3. Restore via cp (atomic auf gleichen Filesystem)
cp "$SOURCE_PATH" "$DB"
# WAL/SHM aufräumen damit nicht noch alter State angehängt wird
[ -f "$DB-wal" ] && rm -f "$DB-wal"
[ -f "$DB-shm" ] && rm -f "$DB-shm"
echo "✓ Restore complete"

# 4. Integrity der restoreten DB
INT2=$(sqlite3 "$DB" 'PRAGMA integrity_check' | head -1)
if [ "$INT2" != "ok" ]; then
    echo "⚠ WARNUNG: Restored DB hat Integrity-Issue: $INT2"
fi

# 5. Counts vor/nach für Sanity-Check
echo "═══ Counts in restored DB ═══"
for tbl in users contracts leads grundseminar_teilnehmer manual_eh_entries; do
    cnt=$(sqlite3 "$DB" "SELECT COUNT(*) FROM $tbl" 2>/dev/null || echo "n/a")
    echo "  $tbl: $cnt"
done

# 6. WSGI touch für Reload (Production)
WSGI=/var/www/proacademy-business_de_wsgi.py
if [ -f "$WSGI" ]; then
    touch "$WSGI"
    echo "✓ WSGI touched → Flask reload triggered"
else
    echo "ⓘ WSGI-File nicht gefunden — Reload manuell falls nötig"
fi

echo ""
echo "🟢 RESTORE FERTIG. Falls was schief geht, ist die alte DB hier:"
echo "   $BEFORE_RESTORE"
