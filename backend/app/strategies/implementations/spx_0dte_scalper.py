import pandas as pd
from decimal import Decimal
from typing import Dict, Any
import asyncio
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce, PriceType
from nautilus_trader.model.identifiers import InstrumentId, Venue, Symbol
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.instruments import Instrument
from app.strategies.base import BaseStrategy
from app.strategies.config import StrategyConfig

class SPXZeroDTEScalper(BaseStrategy):
    def __init__(self, config: StrategyConfig, integration_manager=None):
        super().__init__(config, integration_manager)
        
        # Extract parameters from config
        self.spx_instrument_id = InstrumentId.from_str(config.instrument_id)
        self.amount = config.parameters.get("amount", 1)
        
        self.entry_done = False  # Прапор, щоб увійти лише один раз (для тесту)
        self.generated_instruments = []  # Зберігаємо ID опціонів
        self.current_price = 0.0
        
        # Variables for option request tracking
        self.pending_strike = None
        self.pending_expiry = None
        self.options_requested = False

    def on_start_safe(self):
        # Підписуємось на quote ticks по SPX (як у SpxStreamer)
        # Спочатку перевіряємо, чи інструмент є в кеші
        if self.cache.instrument(self.spx_instrument_id):
            self.subscribe_quote_ticks(self.spx_instrument_id)
            self.logger.info(f"Strategy started. Subscribed to {self.spx_instrument_id}")
        else:
            self.logger.info(f"Instrument {self.spx_instrument_id} not found in cache, requesting from IB...")
            # Запитуємо інструмент з IB
            self.request_instruments(
                venue=Venue("InteractiveBrokers"),
                params={
                    "ib_contracts": [
                        {"secType": "IND", "symbol": "SPX", "exchange": "CBOE", "currency": "USD"}
                    ]
                }
            )
            # Запускаємо polling як fallback
            asyncio.create_task(self._wait_for_instrument_and_subscribe())
        
        self._functional_ready = True

    async def _wait_for_instrument_and_subscribe(self):
        """Fallback polling to ensure subscription if on_instrument_added doesn't fire immediately"""
        for i in range(30):  # Try for 30 seconds
            if self.cache.instrument(self.spx_instrument_id):
                self.logger.info(f"Instrument {self.spx_instrument_id} found via polling, subscribing...")
                self.subscribe_quote_ticks(self.spx_instrument_id)
                return
            await asyncio.sleep(1)
        
        self.logger.error(f"Timeout waiting for instrument {self.spx_instrument_id} definition from IB")

    def on_instrument_added(self, instrument: Instrument):
        """Called when instrument is added to cache"""
        if instrument.id == self.spx_instrument_id:
            self.logger.info(f"Instrument {instrument.id} added (event), subscribing...")
            self.subscribe_quote_ticks(self.spx_instrument_id)

    def on_stop_safe(self):
        # Відписуємось від quote ticks
        try:
            self.unsubscribe_quote_ticks(self.spx_instrument_id)
            self.logger.info(f"Unsubscribed from {self.spx_instrument_id}")
        except Exception as e:
            self.logger.error(f"Failed to unsubscribe: {e}")
        self.logger.info("Strategy stopped")

    def on_quote_tick(self, tick: QuoteTick):
        # Логіка виконується тільки якщо ми ще не входили в позицію сьогодні
        if self.entry_done:
            return

        # 1. Беремо поточну ціну SPX з QuoteTick
        bid = tick.bid_price.as_double()
        ask = tick.ask_price.as_double()
        
        if bid > 0 and ask > 0:
            current_price = (bid + ask) / 2
        elif bid > 0:
            current_price = bid
        elif ask > 0:
            current_price = ask
        else:
            return  # Немає валідної ціни
        
        self.current_price = current_price
        self.logger.info(f"Current SPX Price: {current_price:.2f}")

        # 2. Обраховуємо найближчий страйк (округлення до 5)
        strike_price = int(round(current_price / 5) * 5)
        self.logger.info(f"Target Strike: {strike_price}")

        # 3. Дата експірації - сьогодні (0DTE)
        now = self.clock.utc_now()
        expiry_date_occ = now.strftime("%y%m%d")  # YYMMDD for OCC symbols
        expiry_date_ib = now.strftime("%Y%m%d")   # YYYYMMDD for IB requests
        
        # Store for later use when options are added
        self.pending_strike = strike_price
        self.pending_expiry = expiry_date_occ
        
        # 4. Request options from IB instead of immediate trading
        self._request_option_contracts(strike_price, expiry_date_ib)
        
        # Mark that we've initiated the process (prevent multiple requests)
        self.entry_done = True
        self.options_requested = True
        
        # Start polling for options availability
        asyncio.create_task(self._wait_for_options_and_trade())

    def _execute_trade(self, instrument_id: InstrumentId, side: OrderSide):
        # Перевіряємо, чи є інструмент в кеші. Якщо ні - ми не можемо створити Order об'єкт.
        # Це обмеження Nautilus. Зазвичай тут треба робити self.instrument_provider.find(...)
        instrument = self.cache.instrument(instrument_id)
        
        if instrument is None:
            self.logger.error(f"Instrument {instrument_id} not found in cache! Cannot trade.")
            # Тут можна додати логіку запиту інструменту через IB API, 
            # але це асинхронна операція.
            return

        order = self.order_factory.market(
            instrument_id=instrument_id,
            order_side=side,
            quantity=Quantity.from_int(self.amount),
            time_in_force=TimeInForce.GTC,  # або DAY
        )
        self.submit_order(order)
        self.logger.info(f"Submitted {side} order for {instrument_id}")

    def _close_positions_logic(self, timestamp):
        self.logger.info("Timer triggered: Closing positions")
        for instrument_id in self.generated_instruments:
            # Закриваємо позиції (зворотня угода)
            # У продакшені краще використовувати self.close_position(position_id)
            # Але тут ми просто продаємо те, що купили.
            self._execute_trade(instrument_id, OrderSide.SELL)

    async def _wait_for_options_and_trade(self):
        """Poll for options availability and execute trades when ready"""
        if not self.pending_strike or not self.pending_expiry:
            self.logger.error("No pending strike/expiry set")
            return
        
        # Generate option IDs
        call_symbol_occ = self._generate_occ_symbol("SPXW", self.pending_expiry, "C", self.pending_strike)
        put_symbol_occ = self._generate_occ_symbol("SPXW", self.pending_expiry, "P", self.pending_strike)
        
        venue = Venue("CBOE")
        call_id = InstrumentId(Symbol(call_symbol_occ), venue)
        put_id = InstrumentId(Symbol(put_symbol_occ), venue)
        
        self.logger.info(f"Waiting for options: Call={call_id}, Put={put_id}")
        
        # Poll for up to 30 seconds
        for i in range(30):
            call_instrument = self.cache.instrument(call_id)
            put_instrument = self.cache.instrument(put_id)
            
            if call_instrument and put_instrument:
                self.logger.info("Both SPXW options found in cache, executing trades")
                
                # Execute trades
                self._execute_trade(call_id, OrderSide.BUY)
                self._execute_trade(put_id, OrderSide.BUY)
                
                # Store for later closing
                self.generated_instruments = [call_id, put_id]
                
                # Schedule exit in 1 minute
                timer_name = "exit_timer"
                delay_ns = 60_000_000_000  # 1 minute in nanoseconds
                self.clock.set_timer_ns(
                    name=timer_name,
                    interval_ns=delay_ns,
                    start_time_ns=self.clock.timestamp_ns() + delay_ns,
                    stop_time_ns=None,
                    callback=self._close_positions_logic
                )
                self.logger.info("Scheduled exit in 1 minute")
                return
            
            await asyncio.sleep(1)
        
        self.logger.error(f"Timeout waiting for SPXW option contracts: {call_id}, {put_id}")

    def _generate_occ_symbol(self, root: str, date_str: str, option_type: str, strike: int) -> str:
        """
        Генерує рядок у форматі OCC.
        root: до 6 символів (напр. SPXW)
        date_str: YYMMDD
        option_type: 'C' або 'P'
        strike: ціна * 1000 (ціле число), доповнене нулями до 8 символів
        """
        root_padded = root.ljust(6)  # SPXW + 2 пробіли
        strike_padded = f"{int(strike * 1000):08d}"  # 6975 -> 06975000
        return f"{root_padded}{date_str}{option_type}{strike_padded}"

    def _request_option_contracts(self, strike_price: int, expiry_date_ib: str):
        """
        Request Call and Put option contracts from IB
        
        Args:
            strike_price: Strike price (e.g., 6975)
            expiry_date_ib: Expiry date in YYYYMMDD format for IB
        
        Note: Using SPXW for daily (0DTE) options, not SPX
        SPXW = SPX Weeklys (daily expiring options)
        SPX = Monthly options only
        """
        self.logger.info(f"Requesting SPXW options: strike={strike_price}, expiry={expiry_date_ib}")
        
        self.request_instruments(
            venue=Venue("InteractiveBrokers"),
            params={
                "ib_contracts": [
                    {
                        "secType": "OPT",
                        "symbol": "SPXW",  # SPXW for daily options!
                        "exchange": "CBOE",  # CBOE is the primary exchange for SPX/SPXW options
                        "currency": "USD",
                        "lastTradeDateOrContractMonth": expiry_date_ib,
                        "strike": strike_price,
                        "right": "C"  # Call
                    },
                    {
                        "secType": "OPT",
                        "symbol": "SPXW",  # SPXW for daily options!
                        "exchange": "CBOE",  # CBOE is the primary exchange for SPX/SPXW options
                        "currency": "USD",
                        "lastTradeDateOrContractMonth": expiry_date_ib,
                        "strike": strike_price,
                        "right": "P"  # Put
                    }
                ]
            }
        )

    def get_state(self) -> Dict[str, Any]:
        """Return serializable state"""
        return {
            "entry_done": self.entry_done,
            "generated_instruments": [str(inst_id) for inst_id in self.generated_instruments]
        }

    def set_state(self, state: Dict[str, Any]):
        """Restore state from dictionary"""
        self.entry_done = state.get("entry_done", False)
        # Restore instrument IDs
        inst_strs = state.get("generated_instruments", [])
        self.generated_instruments = [InstrumentId.from_str(s) for s in inst_strs]