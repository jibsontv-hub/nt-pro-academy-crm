#!/usr/bin/env bash
# Tägliches SQLite-Backup mit Integrity-Check + 30-Tage-Rotation.
# Wird auf PythonAnywhere als "Daily Scheduled Task" um 03:00 UTC ausgeführt.
#
# Setup auf PA:
#   Tasks-Tab → Daily um 03:00 UTC → Command:
#   bash /home/ProAcademy/nt-pro-academy-crm/scripts/db_backup.sh
#
# Backup nutzt SQLite's `.backup` API (atomic, sicher bei aktiven Writes).
# Integrity-Check verifiziert dass das Backup sauber ist — corrupt → renamed
# auf .CORRUPT damit Rotation es nicht überschreibt + exit 1 (PA notified).
set -e
DB=$HOME/nt-pro-academy-crm/vertrieb.db
BKP_DIR=$HOME/nt-pro-academy-crm/backups
DATE=$(date +%Y-%m-%d)
TARGET=$BKP_DIR/vertrieb-$DATE.db
mkdir -p $BKP_DIR
# 1. Backup mit echtem SQLite-API (sicher bei aktiven Writes — kein File-Copy-Race)
sqlite3 $DB ".backup '$TARGET'"
# 2. Integrity-Check des frischen Backups
INT=$(sqlite3 $TARGET 'PRAGMA integrity_check' | head -1)
if [ "$INT" != "ok" ]; then
    echo "BACKUP CORRUPT: $INT" >&2
    mv $TARGET $TARGET.CORRUPT
    exit 1
fi
echo "Backup OK: $TARGET ($(du -h $TARGET | cut -f1))"
# 3. Rotation: ältere als 30 Tage löschen
find $BKP_DIR -name 'vertrieb-*.db' -mtime +30 -delete
