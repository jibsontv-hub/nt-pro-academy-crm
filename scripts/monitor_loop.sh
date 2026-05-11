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

echo "[$(date)] Monitor-Loop gestartet — health 15min · full-audit 6h" | tee -a $HEALTH_LOG

while true; do
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
