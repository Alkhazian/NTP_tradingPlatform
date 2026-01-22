import logging
import time
import sys
import os

# Add app directory to path
sys.path.append("/app")

try:
    from app.logging.victorialogs_handler import VictoriaLogsHandler
except ImportError:
    # If running from /root/ntp-remote vs /app
    sys.path.append("/root/ntp-remote/backend")
    from app.logging.victorialogs_handler import VictoriaLogsHandler

# Configure logger
logger = logging.getLogger("strategy.test-strategy-id")
logger.setLevel(logging.INFO)

# Setup handler
handler = VictoriaLogsHandler(
    victorialogs_url="http://victorialogs:9428",
    stream_fields=("strategy_id", "source", "level"),
    flush_interval=0.1,
    batch_size=1
)
logger.addHandler(handler)

print("Sending test log...")
logger.info("This is a test log from debug script")

# Give time for async worker
time.sleep(2)
print("Done. Check VictoriaLogs.")
