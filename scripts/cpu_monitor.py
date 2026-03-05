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
TOP_PROCESSES = int(os.getenv("MONITOR_TOP_PROCESSES", "5"))          # how many top processes to show

# Telegram config — read from the same .env used by docker-compose
# Dynamically resolve root directory relative to this script (assumes script is in <root>/scripts/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.getenv("MONITOR_ENV_FILE", PROJECT_ROOT / ".env"))

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


# Global cache to keep psutil.Process objects alive for accurate CPU tracking across polls
_PROC_CACHE = {}

def get_all_processes_info() -> list[dict]:
    """
    Fetch info for all processes, reusing Process objects to get accurate CPU %.
    """
    global _PROC_CACHE
    current_procs = []
    new_cache = {}

    # Use process_iter to efficiently get basic info for all processes
    for p in psutil.process_iter(["pid", "name", "memory_percent", "create_time"]):
        try:
            pid = p.info["pid"]
            ctime = p.info["create_time"]
            
            # Reuse existing Process object if PID and start time match (prevents PID reuse issues)
            if pid in _PROC_CACHE and _PROC_CACHE[pid].create_time() == ctime:
                proc_obj = _PROC_CACHE[pid]
            else:
                proc_obj = p
                # Prime CPU calculation: the first call sets the point of reference and returns 0.0
                proc_obj.cpu_percent(None)
            
            # Get CPU % since the last call to cpu_percent(None) on this specific object
            cpu = proc_obj.cpu_percent(None)
            
            info = p.info.copy()
            info["cpu_percent"] = cpu
            current_procs.append(info)
            new_cache[pid] = proc_obj
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
            
    _PROC_CACHE = new_cache
    return current_procs


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


def format_alert(cpu_total: float, mem, swap, load_avg: tuple, top_cpu: list[dict], top_mem: list[dict]) -> str:
    """Format a rich Telegram alert message."""
    lines = [
        "🔴 <b>SERVER OVERLOAD ALERT</b>",
        "",
        f"⚡ <b>CPU:</b> {cpu_total:.0f}%  (threshold: {CPU_THRESHOLD:.0f}%)",
        f"📊 <b>Load Avg:</b> {load_avg[0]:.2f} / {load_avg[1]:.2f} / {load_avg[2]:.2f}",
        f"💾 <b>RAM:</b> {mem.percent:.0f}% ({mem.used // (1024**2)}MB / {mem.total // (1024**2)}MB)",
        f"💿 <b>Swap:</b> {swap.percent:.0f}% ({swap.used // (1024**2)}MB / {swap.total // (1024**2)}MB)",
        "",
        "<b>Top processes (CPU):</b>",
    ]

    for i, p in enumerate(top_cpu, 1):
        name = p.get("name", "?")
        cpu = p.get("cpu_percent", 0) or 0
        mem_pct = p.get("memory_percent", 0) or 0
        pid = p.get("pid", 0)

        container = get_docker_container_name(pid)
        container_tag = f" [{container}]" if container else ""
        lines.append(f"  {i}. <code>{name}</code>{container_tag} — CPU: {cpu:.0f}%, MEM: {mem_pct:.1f}%")

    lines.append("")
    lines.append("<b>Top processes (Memory):</b>")
    for i, p in enumerate(top_mem, 1):
        name = p.get("name", "?")
        cpu = p.get("cpu_percent", 0) or 0
        mem_pct = p.get("memory_percent", 0) or 0
        pid = p.get("pid", 0)

        container = get_docker_container_name(pid)
        container_tag = f" [{container}]" if container else ""
        lines.append(f"  {i}. <code>{name}</code>{container_tag} — MEM: {mem_pct:.1f}%, CPU: {cpu:.0f}%")

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

    # Prime psutil metrics (first call establishes the baseline for deltas)
    psutil.cpu_percent(interval=None)
    get_all_processes_info()

    time.sleep(POLL_INTERVAL)

    while running:
        try:
            # Collect metrics (total and per-process)
            per_cpu = psutil.cpu_percent(interval=None, percpu=True)
            cpu_total = sum(per_cpu)
            
            # Gather all process stats once per loop to keep CPU tracking consistent
            all_procs = get_all_processes_info()

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

                    # Sort for top lists using the pre-collected data
                    top_cpu = sorted(all_procs, key=lambda x: x.get("cpu_percent", 0) or 0, reverse=True)[:TOP_PROCESSES]
                    top_mem = sorted(all_procs, key=lambda x: x.get("memory_percent", 0) or 0, reverse=True)[:TOP_PROCESSES]

                    alert_text = format_alert(cpu_total, mem, swap, load_avg, top_cpu, top_mem)
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
