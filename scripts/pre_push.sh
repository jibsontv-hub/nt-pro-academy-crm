#!/usr/bin/env bash
# 🔬 PRE-PUSH-VERIFY — fährt lokalen Flask hoch, lässt alle 5 Sub-Agenten laufen,
# räumt am Ende auf. Exit 0 = grün, !=0 = mindestens 1 Agent rot.
#
# Use:
#   bash scripts/pre_push.sh                      # gegen frisch gestarteten lokal-Server
#   bash scripts/pre_push.sh https://prod-url     # gegen LIVE (kein Server-Start)
#
# Erforderliche env vars:
#   QA_USER  — Login-Email (default: najib@ntpro.de)
#   QA_PASS  — Login-Passwort (default: admin123)

set -e

# ── Config ─────────────────────────────────────────────────────────
QA_USER="${QA_USER:-najib@ntpro.de}"
QA_PASS="${QA_PASS:-admin123}"
PORT="${PORT:-5057}"
EXTERNAL_URL="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Logo ───────────────────────────────────────────────────────────
echo ""
echo "╔════════════════════════════════════════════════════╗"
echo "║  🔬 NT Pro Academy — Pre-Push-Verify               ║"
echo "║  5 Sub-Agents · QA · Journey · Vertrieb · Mail-E2E ║"
echo "║                · UI-Audit                          ║"
echo "╚════════════════════════════════════════════════════╝"
echo ""

# ── Mode: lokal-Server ODER External ───────────────────────────────
if [ -n "$EXTERNAL_URL" ]; then
    BASE_URL="$EXTERNAL_URL"
    SERVER_PID=""
    echo "🌐 Mode: EXTERNAL  ($BASE_URL)"
else
    BASE_URL="http://localhost:$PORT"
    echo "🏠 Mode: LOCAL  (Flask wird auf Port $PORT gestartet)"
    cd "$REPO_DIR"
    FLASK_DEBUG=1 python3 -c "
import app
app.app.run(port=$PORT, debug=False, use_reloader=False, threaded=True)
" > /tmp/pre_push_flask.log 2>&1 &
    SERVER_PID=$!
    echo "   Flask PID: $SERVER_PID  · Log: /tmp/pre_push_flask.log"
    # Warten bis HTTP up
    for i in 1 2 3 4 5 6 7 8; do
        sleep 1
        CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/login" 2>/dev/null || echo 000)
        if [ "$CODE" = "200" ]; then
            echo "   ✓ Flask up nach ${i}s"
            break
        fi
        if [ "$i" = "8" ]; then
            echo "   ✗ Flask kam nach 8s nicht hoch — letzte Log-Zeilen:"
            tail -20 /tmp/pre_push_flask.log
            kill $SERVER_PID 2>/dev/null
            exit 99
        fi
    done
fi

# Cleanup-Hook (auch bei Crash)
cleanup() {
    if [ -n "$SERVER_PID" ]; then
        kill $SERVER_PID 2>/dev/null || true
        wait 2>/dev/null || true
    fi
}
trap cleanup EXIT

cd "$REPO_DIR"

# ── AGENT 1 — QA ───────────────────────────────────────────────────
echo ""
echo "┌─────────────────────────────────────────┐"
echo "│  🛡  AGENT 1/5 — QA-Audit (alle Routes) │"
echo "└─────────────────────────────────────────┘"
QA_USER="$QA_USER" QA_PASS="$QA_PASS" python3 scripts/qa_audit.py "$BASE_URL"
QA_RC=$?

# ── AGENT 2 — Human Walkthrough ────────────────────────────────────
echo ""
echo "┌─────────────────────────────────────────┐"
echo "│  👤 AGENT 2/5 — Human-Walkthrough       │"
echo "└─────────────────────────────────────────┘"
QA_USER="$QA_USER" QA_PASS="$QA_PASS" python3 scripts/journey_test.py "$BASE_URL"
J_RC=$?

# ── AGENT 3 — Vertrieb ─────────────────────────────────────────────
echo ""
echo "┌─────────────────────────────────────────┐"
echo "│  🎯 AGENT 3/5 — Vertriebs-Pipeline      │"
echo "└─────────────────────────────────────────┘"
QA_USER="$QA_USER" QA_PASS="$QA_PASS" python3 scripts/vertrieb_test.py "$BASE_URL"
V_RC=$?

# ── AGENT 4 — Email-E2E (NEU) ──────────────────────────────────────
echo ""
echo "┌─────────────────────────────────────────┐"
echo "│  📧 AGENT 4/5 — Email-E2E (Real-Send)   │"
echo "└─────────────────────────────────────────┘"
QA_USER="$QA_USER" QA_PASS="$QA_PASS" python3 scripts/email_e2e_test.py "$BASE_URL"
E_RC=$?

# ── AGENT 5 — UI-Audit (NEU) ───────────────────────────────────────
echo ""
echo "┌─────────────────────────────────────────┐"
echo "│  🎨 AGENT 5/5 — UI-Audit (Buttons/Links)│"
echo "└─────────────────────────────────────────┘"
QA_USER="$QA_USER" QA_PASS="$QA_PASS" python3 scripts/ui_audit.py "$BASE_URL"
UI_RC=$?

# ── Report ─────────────────────────────────────────────────────────
echo ""
echo "╔════════════════════════════════════════════════════╗"
echo "║  📊 GESAMT-REPORT                                  ║"
echo "╠════════════════════════════════════════════════════╣"
printf "║  🛡  QA-Audit:           Exit %-3d                  ║\n" $QA_RC
printf "║  👤 Human-Walkthrough:  Exit %-3d                  ║\n" $J_RC
printf "║  🎯 Vertriebs-Agent:    Exit %-3d                  ║\n" $V_RC
printf "║  📧 Email-E2E-Agent:    Exit %-3d                  ║\n" $E_RC
printf "║  🎨 UI-Audit-Agent:     Exit %-3d                  ║\n" $UI_RC
echo "╚════════════════════════════════════════════════════╝"

TOTAL=$((QA_RC + J_RC + V_RC + E_RC + UI_RC))
if [ "$TOTAL" = "0" ]; then
    echo ""
    echo "🟢  ALLE 5 AGENTEN GRÜN — bereit zum Push."
    echo ""
    exit 0
else
    echo ""
    echo "🔴  Mindestens 1 Agent rot — siehe oben."
    echo ""
    exit 1
fi
