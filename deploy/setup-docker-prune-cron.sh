#!/usr/bin/env bash
# deploy/setup-docker-prune-cron.sh
# Installs a weekly cron job to prune dangling Docker images and build cache.
# Prevents disk exhaustion from accumulated build layers (22+ GB observed on 2026-03-05).
# Safe: only removes dangling/untagged images and build cache — running images and
# tagged images like ib-gateway are never touched.

set -euo pipefail

CRON_FILE="/etc/cron.weekly/docker-prune"
LOG_FILE="/var/log/docker-prune.log"

cat > "$CRON_FILE" <<'EOF'
#!/usr/bin/env bash
# Weekly Docker cleanup — installed by deploy/setup-docker-prune-cron.sh
LOG=/var/log/docker-prune.log

echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) docker prune start ---" >> "$LOG"
docker image prune -f   >> "$LOG" 2>&1
docker builder prune -f >> "$LOG" 2>&1
echo "--- done ---" >> "$LOG"
EOF

chmod +x "$CRON_FILE"

echo "✓ Cron job installed at $CRON_FILE"
echo "  Runs: weekly (Sunday ~03:00 on most distros via /etc/cron.weekly)"
echo "  Log:  $LOG_FILE"

# Show next scheduled run
echo ""
echo "Verifying:"
ls -lh "$CRON_FILE"
run-parts --test /etc/cron.weekly 2>/dev/null && echo "  run-parts test passed" || true
