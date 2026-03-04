#!/usr/bin/env bash
# Easy install script for CPU Monitor
# This script dynamically updates the systemd service file with the current absolute path
# and installs/restarts the service.

set -e

# Get the absolute path of the project root
PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SERVICE_FILE="$PROJECT_ROOT/deploy/services/ntd-cpu-monitor.service"

echo "Installing CPU Monitor using project root: $PROJECT_ROOT"

# Ensure psutil is installed
sudo apt update && sudo apt install -y python3-psutil

# Generate the service file dynamically with correct absolute paths
cat << EOF > "$SERVICE_FILE"
[Unit]
Description=NTD CPU/Memory Monitor with Telegram Alerts
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $PROJECT_ROOT/scripts/cpu_monitor.py
Restart=always
RestartSec=10

# Environment overrides (uncomment to customize)
# Environment=MONITOR_POLL_INTERVAL=5
# Environment=MONITOR_CPU_THRESHOLD=150
# Environment=MONITOR_MEM_THRESHOLD=90
# Environment=MONITOR_ALERT_COOLDOWN=300

# Hardening
ProtectSystem=strict
ReadWritePaths=$PROJECT_ROOT/logs
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# Copy to systemd, reload, and start
sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ntd-cpu-monitor
sudo systemctl restart ntd-cpu-monitor

echo "CPU Monitor service installed and started successfully."
echo "You can check the status with: systemctl status ntd-cpu-monitor"
