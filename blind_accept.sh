#!/bin/bash
# Try standard displays
export DISPLAY=:0

# Sometimes Xvfb uses :99
if [ ! -f /tmp/.X11-unix/X0 ]; then
    export DISPLAY=:1
    if [ ! -f /tmp/.X11-unix/X1 ]; then
         export DISPLAY=:99
    fi
fi

echo "Using DISPLAY=$DISPLAY"

echo "Starting Blind Accept Sequence..."

for i in {1..20}; do
    echo "Attempt $i..."
    
    # Send Enter to accept default "OK"
    xdotool key Return
    sleep 2
    
    # Send Tab+Enter to reach "I Understand"
    xdotool key Tab
    xdotool key Return
    sleep 2
    
    # Click center
    xdotool mousemove 512 384 click 1
    sleep 1

    if netstat -tuln | grep 4002; then
        echo "SUCCESS! Port 4002 is open!"
        exit 0
    fi
done
