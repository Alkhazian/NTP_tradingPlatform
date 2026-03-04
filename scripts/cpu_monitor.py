#!/usr/bin/env python3
"""
CPU & Memory Monitor with Telegram Alerts.

Runs as a systemd service on the host OS.
Checks system load every POLL_INTERVAL seconds and sends
a Telegram notification when thresholds are exceeded.

Alerts include:
  - Which process/container is consuming the most CPU
  - RAM and Swap usage
  - Load average

Cooldown prevents alert spam: after firing, the next alert
is suppressed for ALERT_COOLDOWN seconds unless load drops
below the threshold and rises again.
"""

import os
import sys
import time
import signal
import logging
import urllib.request
import urllib.parse
import json
from pathlib import Path

import psutil

# ──────────────────────────────────────────────
#  Configuration (override via env vars)
# ──────────────────────────────────────────────
POLL_INTERVAL = int(os.getenv("MONITOR_POLL_INTERVAL", "5"))          # seconds between checks
CPU_THRESHOLD = float(os.getenv("MONITOR_CPU_THRESHOLD", "150"))      # total CPU % (across all cores)
MEM_THRESHOLD = float(os.getenv("MONITOR_MEM_THRESHOLD", "90"))       # RAM usage %
ALERT_COOLDOWN = int(os.getenv("MONITOR_ALERT_COOLDOWN", "300"))      # seconds between repeat alerts
TOP_PROCESSES = int(os.getenv("MONITOR_TOP_PROCESSES", "3"))          # how many top processes to show

# Telegram config — read from the same .env used by docker-compose
ENV_FILE = Path(os.getenv("MONITOR_ENV_FILE", "/root/ntd_trader_dashboard/.env"))

# ──────────────────────────────────────────────
#  Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("cpu_monitor")

# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────

def load_env_file(path: Path) -> dict:
    """Parse a simple KEY=VALUE .env file."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def send_telegram(token: str, chat_id: str, text: str):
    """Send a Telegram message using only stdlib (no external deps)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log.info("Telegram alert sent successfully")
            else:
                log.warning(f"Telegram returned status {resp.status}")
    except Exception as e:
        log.error(f"Failed to send Telegram alert: {e}")


def get_top_cpu_processes(n: int = 3) -> list[dict]:
    """Return top-N processes by CPU usage."""
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "cmdline"]):
        try:
            info = p.info
            procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: x.get("cpu_percent", 0) or 0, reverse=True)
    return procs[:n]


def get_docker_container_name(pid: int) -> str | None:
    """Try to resolve a PID to a Docker container name via cgroup."""
    try:
        cgroup_path = Path(f"/proc/{pid}/cgroup")
        if not cgroup_path.exists():
            return None
        text = cgroup_path.read_text()
        # Look for docker container ID in cgroup path
        for line in text.splitlines():
            if "docker" in line or "containerd" in line:
                # Extract container ID (last 12+ chars of the hash)
                parts = line.split("/")
                for part in reversed(parts):
                    if len(part) >= 12 and all(c in "0123456789abcdef" for c in part[:12]):
                        container_id = part[:12]
                        # Try to get container name via docker inspect
                        try:
                            import subprocess
                            result = subprocess.run(
                                ["docker", "inspect", "--format", "{{.Name}}", container_id],
                                capture_output=True, text=True, timeout=3
                            )
                            if result.returncode == 0:
                                name = result.stdout.strip().lstrip("/")
                                return name
                        except Exception:
                            return f"container:{container_id}"
        return None
    except Exception:
        return None


def format_alert(cpu_total: float, mem, swap, load_avg: tuple, top_procs: list[dict]) -> str:
    """Format a rich Telegram alert message."""
    lines = [
        "🔴 <b>SERVER OVERLOAD ALERT</b>",
        "",
        f"⚡ <b>CPU:</b> {cpu_total:.0f}%  (threshold: {CPU_THRESHOLD:.0f}%)",
        f"📊 <b>Load Avg:</b> {load_avg[0]:.2f} / {load_avg[1]:.2f} / {load_avg[2]:.2f}",
        f"💾 <b>RAM:</b> {mem.percent:.0f}% ({mem.used // (1024**2)}MB / {mem.total // (1024**2)}MB)",
        f"💿 <b>Swap:</b> {swap.percent:.0f}% ({swap.used // (1024**2)}MB / {swap.total // (1024**2)}MB)",
        "",
        "<b>Top processes:</b>",
    ]

    for i, p in enumerate(top_procs, 1):
        name = p.get("name", "?")
        cpu = p.get("cpu_percent", 0) or 0
        mem_pct = p.get("memory_percent", 0) or 0
        pid = p.get("pid", 0)

        # Try to identify Docker container
        container = get_docker_container_name(pid)
        container_tag = f" [{container}]" if container else ""

        lines.append(f"  {i}. <code>{name}</code>{container_tag} — CPU: {cpu:.0f}%, MEM: {mem_pct:.1f}%")

    lines.append("")
    lines.append("⚠️ Consider restarting the heavy container or the server.")

    return "\n".join(lines)


def format_recovery_alert(cpu_total: float, load_avg: tuple) -> str:
    """Format a recovery notification."""
    return (
        "🟢 <b>SERVER LOAD RECOVERED</b>\n"
        "\n"
        f"⚡ <b>CPU:</b> {cpu_total:.0f}%\n"
        f"📊 <b>Load Avg:</b> {load_avg[0]:.2f} / {load_avg[1]:.2f} / {load_avg[2]:.2f}\n"
        "\n"
        "System is back to normal."
    )


# ──────────────────────────────────────────────
#  Main loop
# ──────────────────────────────────────────────

def main():
    log.info("Starting CPU/Memory monitor")
    log.info(f"  Poll interval: {POLL_INTERVAL}s")
    log.info(f"  CPU threshold: {CPU_THRESHOLD}%")
    log.info(f"  Memory threshold: {MEM_THRESHOLD}%")
    log.info(f"  Alert cooldown: {ALERT_COOLDOWN}s")

    # Load Telegram credentials
    dotenv = load_env_file(ENV_FILE)
    tg_token = os.getenv("TELEGRAM_TOKEN") or dotenv.get("TELEGRAM_TOKEN", "")
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID") or dotenv.get("TELEGRAM_CHAT_ID", "")

    if not tg_token or not tg_chat_id:
        log.error("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not configured. Exiting.")
        sys.exit(1)

    log.info(f"  Telegram chat: {tg_chat_id}")
    log.info(f"  Env file: {ENV_FILE}")

    # Send startup notification
    send_telegram(tg_token, tg_chat_id,
                  "🟢 <b>CPU Monitor started</b>\n"
                  f"  Interval: {POLL_INTERVAL}s\n"
                  f"  CPU threshold: {CPU_THRESHOLD}%\n"
                  f"  MEM threshold: {MEM_THRESHOLD}%")

    # Graceful shutdown
    running = True
    def shutdown(signum, frame):
        nonlocal running
        log.info(f"Received signal {signum}, shutting down...")
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    last_alert_time = 0.0
    was_overloaded = False

    # Prime psutil cpu_percent (first call always returns 0)
    psutil.cpu_percent(interval=None)
    for p in psutil.process_iter(["cpu_percent"]):
        try:
            p.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    time.sleep(POLL_INTERVAL)

    while running:
        try:
            # Collect metrics
            # cpu_percent with percpu=False gives total % across all cores
            # On a 2-core machine: max is 200%
            per_cpu = psutil.cpu_percent(interval=None, percpu=True)
            cpu_total = sum(per_cpu)  # e.g. 200% on a 2-core box

            mem = psutil.virtual_memory()
            swap = psutil.swap_memory()
            load_avg = os.getloadavg()

            now = time.time()

            # Check CPU threshold
            cpu_exceeded = cpu_total >= CPU_THRESHOLD
            mem_exceeded = mem.percent >= MEM_THRESHOLD
            is_overloaded = cpu_exceeded or mem_exceeded

            if is_overloaded:
                if now - last_alert_time >= ALERT_COOLDOWN:
                    log.warning(
                        f"THRESHOLD EXCEEDED | CPU: {cpu_total:.0f}% | "
                        f"MEM: {mem.percent:.0f}% | Load: {load_avg[0]:.2f}"
                    )

                    # Collect top processes (need a short interval for accurate per-process CPU)
                    top_procs = get_top_cpu_processes(TOP_PROCESSES)

                    alert_text = format_alert(cpu_total, mem, swap, load_avg, top_procs)
                    send_telegram(tg_token, tg_chat_id, alert_text)
                    last_alert_time = now
                    was_overloaded = True
            else:
                # Send recovery notification once when load drops
                if was_overloaded:
                    log.info(f"Load recovered | CPU: {cpu_total:.0f}% | MEM: {mem.percent:.0f}%")
                    recovery_text = format_recovery_alert(cpu_total, load_avg)
                    send_telegram(tg_token, tg_chat_id, recovery_text)
                    was_overloaded = False

        except Exception as e:
            log.error(f"Monitor loop error: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL)

    # Shutdown
    send_telegram(tg_token, tg_chat_id, "⚪ <b>CPU Monitor stopped</b>")
    log.info("CPU Monitor stopped")


if __name__ == "__main__":
    main()
