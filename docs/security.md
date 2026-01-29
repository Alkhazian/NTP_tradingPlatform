## Security Layer 1: Fail2Ban Installation

1. Install and configure fail2ban to automatically block IPs after failed login attempts.

```bash
apt-get update && apt-get install -y fail2ban
systemctl enable fail2ban
systemctl start fail2ban
fail2ban-client status sshd
```

2. Configure fail2ban to block IPs after 3 failed login attempts.

```bash
nano /etc/fail2ban/jail.local

[DEFAULT]
# Ban for 1 hour
bantime = 3600
# 5 attempts within 10 minutes triggers ban
findtime = 600
maxretry = 5
# Use iptables for banning
banaction = iptables-multiport

[sshd]
enabled = true
port = ssh
logpath = /var/log/auth.log
maxretry = 3
bantime = 7200
```

3. Restart fail2ban to apply changes.

```bash
systemctl restart fail2ban
fail2ban-client status sshd

tail -n 100  /var/log/auth.log 
tail -f /var/log/fail2ban.log
```

## Security Layer 2: SSH Hardening
[MODIFY] /etc/ssh/sshd_config
Apply production-grade SSH hardening:

```bash
# Disable password authentication (keys only)
-#PasswordAuthentication yes
+PasswordAuthentication no
# Restrict root login to keys only
-#PermitRootLogin prohibit-password
+PermitRootLogin prohibit-password
# Reduce authentication attempts
-#MaxAuthTries 6
+MaxAuthTries 3
# Limit concurrent sessions
-#MaxSessions 10
+MaxSessions 5
# Rate limit connection attempts
-#MaxStartups 10:30:100
+MaxStartups 3:50:10
# Disable unused authentication methods
-#ChallengeResponseAuthentication yes
+ChallengeResponseAuthentication no
# Disable empty passwords
-#PermitEmptyPasswords no
+PermitEmptyPasswords no
# Add login grace time limit
-#LoginGraceTime 2m
+LoginGraceTime 30s
# Disable DNS lookups (performance)
-#UseDNS no
+UseDNS no
```

Optional but recommended: Change SSH port from 22 to non-standard port (e.g., 2222) to avoid automated scanners.

## Security Layer 3: Firewall Rules
UFW Configuration
```bash
# Enable UFW if not already enabled
ufw --force enable
# Rate limit SSH connections (6 attempts per 30 seconds)
ufw limit ssh/tcp
# Allow HTTP (if needed)
ufw allow 80/tcp
# BLOCK DANGEROUS EXPOSED PORTS (Only allow from localhost)
# Deny external access to VNC
ufw deny 5900/tcp
# Deny external access to IB Gateway
ufw deny 4001/tcp
ufw deny 4002/tcp
# Deny external access to VictoriaMetrics
ufw deny 9428/tcp
# If changing SSH port:
# ufw delete limit ssh/tcp
# ufw limit 2222/tcp
```

## Security Layer 4: Monitoring
[NEW] /usr/local/bin/ssh-attack-monitor.sh
Simple monitoring script to track attack patterns:
```bash
#!/bin/bash
# Monitor SSH attacks and send alerts
LOGFILE="/var/log/ssh-attacks.log"
THRESHOLD=100
# Count failed attempts in last hour
FAILED=$(journalctl -u ssh --since "1 hour ago" | grep -c "Failed password")
if [ "$FAILED" -gt "$THRESHOLD" ]; then
    echo "$(date): High SSH attack volume - $FAILED failed attempts in last hour" >> "$LOGFILE"
fi
# Log top attacking IPs
echo "$(date): Top 10 attacking IPs:" >> "$LOGFILE"
journalctl -u ssh --since "1 hour ago" | grep "Failed password" | \
    awk '{print $(NF-3)}' | sort | uniq -c | sort -rn | head -10 >> "$LOGFILE"
```
Made it executable: 
```bash
chmod +x /usr/local/bin/ssh-attack-monitor.sh
```

Manual run:
```bash
/usr/local/bin/ssh-attack-monitor.sh
```

Automatic run:
```bash
echo "0 * * * * root /usr/local/bin/ssh-attack-monitor.sh" > /etc/cron.d/ssh-attack-monitor
```


## Security Layer 5: Stability (Swap File)
CRITICAL: The server has NO SWAP configured and is crashing (OOM) when RAM fills up. We must add swap to prevent crashes.

# Create 4GB swap file
```bash
fallocate -l 4G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
# Make permanent
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```