import threading
import time
import logging
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.common import BarData

logger = logging.getLogger(__name__)

class IBApp(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.net_liquidation = "0.0"
        self.connected = False
        self.account_id = None

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson = ""):
        logger.error(f"IB Error: {reqId} {errorCode} {errorString}")
        # Error code 502 means connection failed
        if errorCode == 502:
            self.connected = False

    def connectionClosed(self):
        logger.warning("IB Gateway connection closed")
        self.connected = False

    def nextValidId(self, orderId):
        self.connected = True
        logger.info(f"Connected to IB Gateway. Next Valid Id: {orderId}")
        self.reqAccountSummary(9001, "All", "NetLiquidation")

    def accountSummary(self, reqId, account, tag, value, currency):
        if tag == "NetLiquidation":
            self.net_liquidation = f"{value} {currency}"
            self.account_id = account
            logger.info(f"Net Liquidation: {self.net_liquidation}")

    def accountSummaryEnd(self, reqId):
        pass

class IBConnector:
    def __init__(self, host="ib-gateway", port=4002, client_id=1):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.app = None
        self.thread = None
        self._stop_event = threading.Event()
        self._reconnect_thread = None

    def connect(self):
        """Start connection with auto-reconnection support"""
        self._stop_event.clear()
        self._reconnect_thread = threading.Thread(target=self._connection_manager, daemon=True)
        self._reconnect_thread.start()

    def _connection_manager(self):
        """Manages connection and automatic reconnection to IB Gateway"""
        while not self._stop_event.is_set():
            try:
                # Create fresh IBApp instance for each connection attempt
                self.app = IBApp()
                
                logger.info(f"Attempting to connect to IB Gateway at {self.host}:{self.port}")
                self.app.connect(self.host, self.port, self.client_id)
                
                if self.app.isConnected():
                    logger.info("Successfully connected to IB Gateway")
                    self.app.run()  # This blocks until disconnected
                    logger.warning("IB Gateway connection loop ended")
                else:
                    logger.warning("Failed to establish connection to IB Gateway")
                
            except Exception as e:
                logger.error(f"Connection error: {e}")
            
            # Reset connection state
            if self.app:
                self.app.connected = False
            
            if not self._stop_event.is_set():
                logger.info("Will retry connection in 5 seconds...")
                time.sleep(5)

    def disconnect(self):
        """Stop connection and reconnection attempts"""
        self._stop_event.set()
        if self.app and self.app.isConnected():
            self.app.disconnect()

    def get_status(self):
        if self.app is None:
            return {
                "connected": False,
                "net_liquidation": "0.0",
                "account_id": None
            }
        return {
            "connected": self.app.connected,
            "net_liquidation": self.app.net_liquidation,
            "account_id": self.app.account_id
        }
