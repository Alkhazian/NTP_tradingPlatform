import os
import httpx
import logging
import threading

logger = logging.getLogger(__name__)

class TelegramNotificationService:
    """
    Service to send notifications via Telegram Bot API.
    Uses a background thread to avoid blocking the main trading thread.
    """

    def __init__(self, token: str = None, chat_id: str = None):
        self.token = token or os.getenv("TELEGRAM_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.base_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        
        if not self.token or not self.chat_id:
            logger.warning("TelegramNotificationService initialized without credentials. Notifications disabled.")

    def send_message(self, text: str):
        """
        Send a text message in a background thread (fire and forget).
        """
        if not self.token or not self.chat_id:
            return

        threading.Thread(
            target=self._send_sync, 
            args=(text,), 
            name="TelegramNotificationThread",
            daemon=True
        ).start()

    def _send_sync(self, text: str):
        """
        Synchronous send method to be run in a thread.
        """
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
            }
            with httpx.Client(timeout=10.0) as client:
                response = client.post(self.base_url, json=payload)
                response.raise_for_status()
            
            logger.info(f"Telegram message sent: {text[:50]}...")
            
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    def close(self):
        pass
