#!/usr/bin/env bash
# 🩺 Pro Academy 24/7-Monitor — wird auf PA als Always-on-Task registriert.
#
# Loop:
#  - Alle 15 Min: health_monitor.py (Smoke-Test + Push-Alert bei FAIL)
#  - Alle 6 Std: voller Sub-Agent-Run (QA + Journey + Vertrieb)
#  - Logs: /tmp/proacademy-monitor.log + /tmp/proacademy-fullaudit.log
#
# Setup auf PA:
#  Tasks-Tab → "Add a new always-on task" → Run:
#  bash /home/ProAcademy/nt-pro-academy-crm/scripts/monitor_loop.sh
#
# Stoppen: Tasks-Tab → Always-on → "Pause"

set -u
PROJECT=$HOME/nt-pro-academy-crm
HEALTH_SCRIPT=$PROJECT/scripts/health_monitor.py
PRE_PUSH=$PROJECT/scripts/pre_push.sh
LIVE_URL=${LIVE_URL:-https://proacademy-business.de}
HEALTH_LOG=/tmp/proacademy-monitor.log
AUDIT_LOG=/tmp/proacademy-fullaudit.log

# QA-Credentials für Sub-Agents (lass leer wenn nur Smoke-Test gewollt)
export QA_USER=${QA_USER:-najib@ntpro.de}
export QA_PASS=${QA_PASS:-admin123}

cd $PROJECT

LAST_FULL_AUDIT=0
FULL_AUDIT_INTERVAL=$((6 * 3600))  # 6 Stunden
LAST_DAILY_PUSH_DATE=""  # ISO-Datum des letzten Daily-Push (1× pro Tag)
LAST_MIDDAY_PUSH_DATE="" # ISO-Datum des letzten Mittag-Re-Push (1× pro Tag, 13-15 Uhr)
LAST_BACKUP_DATE=""      # ISO-Datum des letzten DB-Backups (1× pro Tag)

echo "[$(date)] Monitor-Loop gestartet — auto-pull · daily-push 8-10 · midday-push 13-15 · health 15min · full-audit 6h" | tee -a $HEALTH_LOG

while true; do
    # ─── 0. Auto-Pull (Fallback wenn GitHub-Webhook fail) ───
    # Webhook in /api/deploy ist primär — dieser Pull catch-t falls webhook nicht ankam
    cd $PROJECT
    PULL_OUTPUT=$(git pull --ff-only 2>&1)
    if echo "$PULL_OUTPUT" | grep -q "Updating"; then
        echo "[$(date)] AUTO-PULL: neue Commits gepullt" | tee -a $HEALTH_LOG
        echo "$PULL_OUTPUT" | head -10 >> $HEALTH_LOG
        # WSGI touch für Reload
        if [ -f /var/www/proacademy-business_de_wsgi.py ]; then
            touch /var/www/proacademy-business_de_wsgi.py
            echo "[$(date)] WSGI touched → Reload triggered" >> $HEALTH_LOG
        fi
        # Newsletter-Agent triggern wenn neue commits kamen
        python3 $PROJECT/scripts/newsletter_agent.py 2>&1 | tail -3 >> $HEALTH_LOG
    fi

    # ─── 1. Cache-Refresh (Dashboard < 500ms) ─────────
    python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from app import _warm_cache_background
_warm_cache_background()
" 2>&1 | grep -E '(warm|fertig|FERTIG|FEHLER)' | head -3 >> $HEALTH_LOG

    # ─── 1b. Daily-Push (1× pro Tag, zwischen 8-10 Uhr) ─────────
    CURRENT_HOUR=$(date +%H)
    CURRENT_DATE=$(date +%Y-%m-%d)
    if [ "$CURRENT_HOUR" -ge 8 ] && [ "$CURRENT_HOUR" -lt 10 ] && [ "$LAST_DAILY_PUSH_DATE" != "$CURRENT_DATE" ]; then
        echo "[$(date)] Daily-Push wird ausgelöst (1× pro Tag)" | tee -a $HEALTH_LOG
        python3 $PROJECT/scripts/daily_push.py 2>&1 | tail -10 >> $HEALTH_LOG
        # Plus: Assistentin-Tagesbriefing für Admin (proaktive Reminder)
        echo "[$(date)] Assistentin-Proaktiv-Push wird ausgelöst" | tee -a $HEALTH_LOG
        python3 $PROJECT/scripts/assistentin_proactive.py 2>&1 | tail -8 >> $HEALTH_LOG
        LAST_DAILY_PUSH_DATE=$CURRENT_DATE
    fi

    # ─── 1b2. Mittag-Re-Push (TIER 1.7) — 1× pro Tag zwischen 13-15 Uhr ─────
    # Pusht alle REPs (ftier=1) die heute noch 0 Aktivität haben (keine Anrufe,
    # keine neuen Termine, keine neuen Leads). „Halbzeit · 0 Aktionen heute".
    if [ "$CURRENT_HOUR" -ge 13 ] && [ "$CURRENT_HOUR" -lt 15 ] && [ "$LAST_MIDDAY_PUSH_DATE" != "$CURRENT_DATE" ]; then
        echo "[$(date)] Mittag-Re-Push wird ausgelöst (REPs ohne Heute-Aktion)" | tee -a $HEALTH_LOG
        python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from app import run_midday_pushes
stats = run_midday_pushes(force=False)
print('midday-stats:', stats)
" 2>&1 | tail -5 >> $HEALTH_LOG
        LAST_MIDDAY_PUSH_DATE=$CURRENT_DATE
    fi

    # ─── 1c. DB-Backup (1× pro Tag, zwischen 3-5 Uhr nachts) ─────────
    if [ "$CURRENT_HOUR" -ge 3 ] && [ "$CURRENT_HOUR" -lt 5 ] && [ "$LAST_BACKUP_DATE" != "$CURRENT_DATE" ]; then
        echo "[$(date)] Daily-Backup wird ausgelöst" | tee -a $HEALTH_LOG
        bash $PROJECT/scripts/db_backup.sh 2>&1 | tail -3 >> $HEALTH_LOG
        LAST_BACKUP_DATE=$CURRENT_DATE
    fi

    # ─── 15-Min Health-Check ──────────────────────────────
    python3 $HEALTH_SCRIPT
    HEALTH_RC=$?

    # ─── 6-Std Full-Audit ──────────────────────────────────
    NOW=$(date +%s)
    if [ $((NOW - LAST_FULL_AUDIT)) -gt $FULL_AUDIT_INTERVAL ]; then
        echo "[$(date)] Starte Full-Audit (3 Sub-Agents) gegen $LIVE_URL" | tee -a $AUDIT_LOG
        bash $PRE_PUSH $LIVE_URL >> $AUDIT_LOG 2>&1
        AUDIT_RC=$?
        if [ $AUDIT_RC -ne 0 ]; then
            echo "[$(date)] FULL-AUDIT FAIL — sende Alert" | tee -a $AUDIT_LOG
            python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from app import send_push_to_user, get_db
db = get_db()
admins = db.execute(\"SELECT id FROM users WHERE role='admin' AND active=1\").fetchall()
db.close()
for a in admins:
    try:
        send_push_to_user(a['id'], '🚨 Sub-Agent FAIL',
                          'Full-Audit gegen Live-Domain ist rot. Check /tmp/proacademy-fullaudit.log',
                          url='/dashboard', urgent=True, tag='audit-fail', push_type='admin_alert')
    except Exception as e:
        print(f'Push fail {a[\"id\"]}: {e}')
" 2>&1 | tee -a $AUDIT_LOG
        else
            echo "[$(date)] FULL-AUDIT OK — alle 3 Sub-Agents grün" | tee -a $AUDIT_LOG
        fi
        LAST_FULL_AUDIT=$NOW
    fi

    # 15 Min schlafen
    sleep 900
done
