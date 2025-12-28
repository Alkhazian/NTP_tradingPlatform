# 2FA Blind Accept Method (Last Resort)

If you cannot connect via VNC, you can try to blindly accept "Paper Trading" dialogs using `xdotool`.

## Prerequisites
1. Install `xdotool` inside the container:
   ```bash
   docker exec -u 0 ntd-ib-gateway apt-get update && apt-get install -y xdotool
   ```

## The Magic Command
The Gateway runs on `DISPLAY=:1` (usually). Run this loop to auto-accept dialogs:

```bash
for i in {1..20}; do 
    echo "Clicking..."; 
    docker exec -e DISPLAY=:1 ntd-ib-gateway xdotool key Return; 
    docker exec -e DISPLAY=:1 ntd-ib-gateway xdotool key Tab; 
    docker exec -e DISPLAY=:1 ntd-ib-gateway xdotool key Return; 
    sleep 2; 
done
```

## IMPORTANT: 2FA
If the gateway is waiting for **Two-Factor Authentication**, `xdotool` **CANNOT** help you.
You **MUST** approve the notification on your **IBKR Mobile App**.

Once approved on your phone, the Gateway will proceed automatically.
