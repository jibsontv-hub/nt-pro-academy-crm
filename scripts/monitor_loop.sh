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
LAST_DAILY_PUSH_DATE=""    # ISO-Datum des letzten Daily-Push (1× pro Tag)
LAST_MIDDAY_PUSH_DATE=""   # ISO-Datum des letzten Mittag-Re-Push (1× pro Tag, 13-15 Uhr)
LAST_BACKUP_DATE=""        # ISO-Datum des letzten DB-Backups (1× pro Tag)
LAST_STAGNATION_DATE=""    # ISO-Datum letzter Stagnations-Mail-Run (1× pro Tag, 9-10 Uhr)
LAST_STREAK_WARN_DATE=""   # ISO-Datum letzter Streak-Warning-Push (1× pro Tag, 18-20 Uhr)
LAST_OWNER_AUDIT_DATE=""   # ISO-Datum letzter Owner-Audit-Mail (1× pro Tag, 21-22 Uhr)

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

    # ─── 1b3. Auto-Approve-Beförderungen (TIER B.2) — alle Loop-Iterationen (15 Min) ───
    # Risikofreie Beförderungen automatisch durchwinken (1-Stufe-hoch + EH-Schwelle
    # + keine Diversifikations-Verletzung). Spart Najib das manuelle Klicken.
    python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from app import try_auto_approve_pending
stats = try_auto_approve_pending()
if stats['auto_approved_count']:
    print('auto-approve:', stats)
" 2>&1 | grep -v '^$' >> $HEALTH_LOG

    # ─── 1b4. Auto-Mail-Sequenz für Bewerber (TIER E.2) — alle 15 Min ───
    # Confirmation 24h, Termin-Vorschlag 72h, Last-Chance 5T.
    # Idempotent — Sequenz pro Lead nur 1× je Stage.
    python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from app import run_lead_followup_sequence
stats = run_lead_followup_sequence()
if any(stats.values()):
    print('lead-followup:', stats)
" 2>&1 | grep -v '^$' >> $HEALTH_LOG

    # ─── 1b5. Stagnations-Sequenz (1× pro Tag, 9-10 Uhr) — Auto-Mail Tag 3 ───
    # BJ Fogg / Eric Worre Pattern: empath. Mail an Partner die genau 3T inaktiv sind.
    # Höher als generischer daily-push damit nicht doppelt mit anderen Reminders.
    if [ "$CURRENT_HOUR" -ge 9 ] && [ "$CURRENT_HOUR" -lt 10 ] && [ "$LAST_STAGNATION_DATE" != "$CURRENT_DATE" ]; then
        echo "[$(date)] Stagnation-Sequenz wird ausgelöst" | tee -a $HEALTH_LOG
        python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from app import run_stagnation_sequence
stats = run_stagnation_sequence()
print('stagnation:', stats)
" 2>&1 | tail -3 >> $HEALTH_LOG
        LAST_STAGNATION_DATE=$CURRENT_DATE
    fi

    # ─── 1b6. Streak-Warning-Push (1× pro Tag, 18-20 Uhr) — Duolingo-Pattern ───
    # User mit Streak ≥3 die heute noch nicht alle DMO-Targets erfüllt haben →
    # Push „Streak in Gefahr". Verlustaversion > Belohnung.
    if [ "$CURRENT_HOUR" -ge 18 ] && [ "$CURRENT_HOUR" -lt 20 ] && [ "$LAST_STREAK_WARN_DATE" != "$CURRENT_DATE" ]; then
        echo "[$(date)] Streak-Warning-Push wird ausgelöst" | tee -a $HEALTH_LOG
        python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from app import run_streak_warning_push
stats = run_streak_warning_push()
print('streak-warn:', stats)
" 2>&1 | tail -3 >> $HEALTH_LOG
        LAST_STREAK_WARN_DATE=$CURRENT_DATE
    fi

    # ─── 1b7. Backoffice Daily-Owner-Audit (1× pro Tag, 21-22 Uhr) ───
    # Mailt Najib einen Tages-Report: DMO heute, Stack-Quote, Stagnations-
    # Stats, Pipeline-Bewegung diese Woche, Auffälligkeiten (Approval-Stau).
    # Damit weiß Najib morgens schon was wichtig ist ohne ins System zu gehen.
    if [ "$CURRENT_HOUR" -ge 21 ] && [ "$CURRENT_HOUR" -lt 22 ] && [ "$LAST_OWNER_AUDIT_DATE" != "$CURRENT_DATE" ]; then
        echo "[$(date)] Daily-Owner-Audit wird ausgelöst" | tee -a $HEALTH_LOG
        python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from app import run_daily_owner_audit
stats = run_daily_owner_audit()
print('owner-audit:', stats)
" 2>&1 | tail -3 >> $HEALTH_LOG
        LAST_OWNER_AUDIT_DATE=$CURRENT_DATE
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
