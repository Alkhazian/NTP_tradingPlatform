# AI-Ready Setup Guide: NTD CPU/Memory Monitor

This guide contains precise steps to deploy the host-level system monitor on a Linux server. It is designed for both humans and AI agents.

## Service Overview
- **Script**: `scripts/cpu_monitor.py`
- **Service Name**: `ntd-cpu-monitor`
- **Purpose**: Monitor host CPU (threshold 150% for 2 cores) and RAM (threshold 90%) and send alerts via Telegram.

## Step 1: Install Host Dependencies
The monitor runs on the host OS (outside Docker) and requires `psutil`.

```bash
sudo apt update && sudo apt install -y python3-psutil
```

## Step 2: Configuration (.env)
The monitor reads credentials from the project's root `.env` file. Ensure the following keys exist:

```env
TELEGRAM_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

## Step 3: Path Verification
The service and script assume the project is located at `/root/ntd_trader_dashboard/`. 

**IF the project path is different (e.g., `/home/ubuntu/...`):**
1.  **In `scripts/cpu_monitor.py`**: Update the `ENV_FILE` constant.
2.  **In `deploy/services/ntd-cpu-monitor.service`**: Update the `ExecStart` path.

## Step 4: Systemd Service Installation
Copy the service definition to the system directory and activate it.

```bash
# 1. Copy the unit file
sudo cp deploy/services/ntd-cpu-monitor.service /etc/systemd/system/

# 2. Reload systemd
sudo systemctl daemon-reload

# 3. Enable start on boot
sudo systemctl enable ntd-cpu-monitor

# 4. Start the monitor
sudo systemctl start ntd-cpu-monitor
```

## Step 5: Verification
Run the following commands to ensure the service is firing correctly:

```bash
# Check status
systemctl status ntd-cpu-monitor

# Review logs to confirm "CPU Monitor started" notification was sent
journalctl -u ntd-cpu-monitor -n 20 --no-pager
```

## Environment Variables (Optional)
You can customize thresholds by adding environment variables to the `[Service]` section of the `.service` file:
- `MONITOR_CPU_THRESHOLD` (Default: 150)
- `MONITOR_MEM_THRESHOLD` (Default: 90)
- `MONITOR_ALERT_COOLDOWN` (Default: 300)
- `MONITOR_POLL_INTERVAL` (Default: 5)
