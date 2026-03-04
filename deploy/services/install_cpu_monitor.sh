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

# Update the service file with the correct absolute paths
sed -i "s|ExecStart=/usr/bin/python3 .*|ExecStart=/usr/bin/python3 $PROJECT_ROOT/scripts/cpu_monitor.py|g" "$SERVICE_FILE"
sed -i "s|ReadWritePaths=.*|ReadWritePaths=$PROJECT_ROOT/logs|g" "$SERVICE_FILE"

# Copy to systemd, reload, and start
sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ntd-cpu-monitor
sudo systemctl restart ntd-cpu-monitor

echo "CPU Monitor service installed and started successfully."
echo "You can check the status with: systemctl status ntd-cpu-monitor"
