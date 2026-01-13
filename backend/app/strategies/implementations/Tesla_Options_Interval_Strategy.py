from decimal import Decimal
from datetime import datetime, timedelta
from typing import Optional, Set, Dict, Any

from nautilus_trader.model.enums import OrderSide, PriceType, TimeInForce
from nautilus_trader.core.message import Event
from nautilus_trader.model.data import TradeTick, QuoteTick
from nautilus_trader.model.identifiers import InstrumentId, Venue, AccountId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.events import OrderFilled, OrderRejected

from app.strategies.base import BaseStrategy
from app.strategies.config import StrategyConfig


class TslaOptionsIntervalStrategy(BaseStrategy):
    """
    Експертна стратегія для торгівлі опціонами на TSLA.
    Автоматично знаходить найближчу дату експірації через запит до IBKR.
    """

    def __init__(self, config: StrategyConfig, integration_manager=None, persistence_manager=None):
        super().__init__(config, integration_manager, persistence_manager)
        
        # Параметри з конфігурації
        self.tsla_id = InstrumentId.from_str(config.instrument_id)
        self.target_premium = Decimal(str(config.parameters.get("target_premium", 2.0)))
        self.call_profit_trigger = Decimal(str(config.parameters.get("call_profit_trigger", 4.0)))
        self.put_profit_trigger = Decimal(str(config.parameters.get("put_profit_trigger", 3.0)))
        self.timeout_seconds = config.parameters.get("timeout_seconds", 300)
        
        # Новий параметр: скільки мінімум днів має бути до експірації (щоб не брати 0DTE випадково)
        self.min_days_to_expiry = int(config.parameters.get("min_days_to_expiry", 1))

        # Стан стратегії
        self.last_trade_date: Optional[datetime.date] = None
        self.opening_price: Optional[Price] = None
        self.call_option_id: Optional[InstrumentId] = None
        self.put_option_id: Optional[InstrumentId] = None
        
        self._call_filled = False
        self._put_filled = False
        self.entry_orders_submitted = False
        self.is_active_today = False
        self.candidate_options: Set[InstrumentId] = set()
        self._discovery_in_progress = False

    def on_start_safe(self):
        self.log.info(f"Starting {self.__class__.__name__} with config ID: {self.tsla_id}")
        
        # Setup periodic check for instrument availability
        self.clock.set_timer(
            name="check_instrument",
            interval=timedelta(seconds=5), 
            callback=self._check_instrument_availability
        )

        instrument = self.cache.instrument(self.tsla_id)
        if instrument:
            self.log.info(f"Found instrument {self.tsla_id} in cache.")
            self._initialize_strategy_logic()
        else:
            self.log.info(f"Instrument {self.tsla_id} not in cache. Requesting...")
            self._request_base_instrument()

    def _request_base_instrument(self):
        self.request_instruments(
            venue=Venue("InteractiveBrokers"),
            params={"ib_contracts": [{"secType": "STK", "symbol": "TSLA", "exchange": "SMART", "currency": "USD"}]}
        )

    def _initialize_strategy_logic(self):
        # Subscribe to trade ticks with explicit client routing to InteractiveBrokers
        from nautilus_trader.model.identifiers import ClientId
        self.subscribe_trade_ticks(self.tsla_id, client_id=ClientId("InteractiveBrokers"))
        self.log.info(f"Subscribed to trade ticks for {self.tsla_id} via InteractiveBrokers. Waiting for incoming data...")

    def _check_instrument_availability(self, event):
        if self.is_active_today: return 

        instrument = self.cache.instrument(self.tsla_id)
        if instrument:
             self.log.info(f"[Timer] Found instrument {self.tsla_id}. Initializing...")
             self._initialize_strategy_logic()
        else:
             self.log.info(f"[Timer] Instrument {self.tsla_id} still not in cache.")
             if not self._discovery_in_progress: # Only re-request if we aren't already searching for options
                 self._request_base_instrument()
        
        # Schedule next check if not active
        if not self.is_active_today:
            try:
                self.clock.cancel_timer("check_instrument")
            except:
                pass  # Timer might not exist yet
            self.clock.set_timer(
                name="check_instrument",
                interval=timedelta(seconds=10),
                callback=self._check_instrument_availability
            )

    def on_instrument_added(self, instrument: Instrument):
        self.log.info(f"Instrument added: {instrument.id} (Symbol: {instrument.symbol})")
        
        # Check for direct match or symbol match (handling ID mismatch)
        is_match = instrument.id == self.tsla_id
        if not is_match and instrument.symbol == "TSLA" and instrument.id.venue.value == "InteractiveBrokers":
             self.log.warning(f"ID Mismatch detected! Config expects {self.tsla_id}, but received {instrument.id}. Updating to use {instrument.id}.")
             self.tsla_id = instrument.id
             is_match = True

        if is_match:
            self.log.info(f"Subscribing to {self.tsla_id}")
            self._initialize_strategy_logic()
            return

        if self._discovery_in_progress and hasattr(instrument, 'underlying_id'):
            if instrument.underlying_id == self.tsla_id:
                self.log.info(f"Candidate option found: {instrument.id}")
                self.subscribe_quotes(instrument.id)
                self.candidate_options.add(instrument.id)



    def on_trade_tick(self, tick: TradeTick):
        if tick.instrument_id != self.tsla_id: return
        self._handle_entry_logic(tick)
        self._check_exit_conditions(tick.price)

    def _handle_entry_logic(self, tick: TradeTick):
        current_date = self.clock.utc_now().date()
        
        # Reset daily flag if it's a new day
        if self.last_trade_date and self.last_trade_date != current_date:
            self.log.info(f"New trading day detected. Resetting daily state.")
            self.is_active_today = False
            self.entry_orders_submitted = False
            self._call_filled = False
            self._put_filled = False
            self.call_option_id = None
            self.put_option_id = None
            self.last_trade_date = None  # Reset so we can trade today
        
        # Check if we've already initiated trading today
        if self.is_active_today:
            return
        
        # Initiate trading for today
        self.log.info(f"Initiating entry logic for date: {current_date}")
        self.last_trade_date = current_date
        self.is_active_today = True
        self.save_state() # Persist immediately
        self._initiate_discovery(tick.price)

    def _initiate_discovery(self, current_price: Price):
        """Запит конкретних опціонів зі страйками навколо поточної ціни."""
        self.opening_price = current_price
        self._discovery_in_progress = True
        self.candidate_options.clear()
        
        # Calculate expiry date (next Friday or specific date logic)
        now_dt = self.clock.utc_now()
        days_ahead = (4 - now_dt.weekday()) % 7  # Days until Friday (0=Mon, 4=Fri)
        if days_ahead == 0:  # If today is Friday
            days_ahead = 7  # Use next Friday
        
        expiry_date = now_dt.date() + timedelta(days=days_ahead)
        expiry_str = expiry_date.strftime("%Y%m%d")
        
        # Calculate strikes around current price
        current_price_float = float(current_price)
        strike_interval = 5.0  # TSLA options usually have $5 strike intervals
        
        # Round to nearest strike interval
        base_strike = round(current_price_float / strike_interval) * strike_interval
        
        # Generate strikes: ATM, +/- 1, +/- 2 intervals
        strikes = [
            base_strike - 2 * strike_interval,
            base_strike - strike_interval,
            base_strike,
            base_strike + strike_interval,
            base_strike + 2 * strike_interval,
        ]
        
        self.log.info(f"Requesting TSLA options: expiry={expiry_date}, strikes={strikes}")
        
        # Request all combinations of strikes and rights (Call/Put)
        contracts = []
        for strike in strikes:
            for right in ["C", "P"]:
                contracts.append({
                    "secType": "OPT",
                    "symbol": "TSLA",
                    "exchange": "SMART",
                    "currency": "USD",
                    "lastTradeDateOrContractMonth": expiry_str,
                    "strike": strike,
                    "right": right
                })
        
        self.request_instruments(
            venue=Venue("InteractiveBrokers"),
            params={"ib_contracts": contracts}
        )

        # Wait for instruments to be discovered and quotes to arrive
        # Cancel any existing timer first to avoid duplicate timer error
        try:
            self.clock.cancel_timer("execute_entry")
        except:
            pass  # Timer might not exist yet
        
        self.clock.set_timer(
            name="execute_entry",
            interval=timedelta(seconds=10),
            callback=self._execute_entry_from_quotes
        )

    def _execute_entry_from_quotes(self, event: Event):
        """Вибір найкращих Call/Put опціонів за премією."""
        if self.entry_orders_submitted: return
        self._discovery_in_progress = False

        if not self.candidate_options:
            self.log.error("Не знайдено жодного опціону серед кандидатів.")
            self.is_active_today = False  # Reset so we can try again
            return

        # Find best Call and Put based on premium proximity to target
        best_call, best_put = None, None
        min_call_diff, min_put_diff = Decimal('inf'), Decimal('inf')

        from nautilus_trader.model.enums import OptionKind
        
        for opt_id in self.candidate_options:
            instr = self.cache.instrument(opt_id)
            if not instr or not hasattr(instr, 'option_kind'):
                continue
            
            quote = self.cache.last_quote(opt_id)
            if not quote or not quote.ask_price: 
                continue
            
            diff = abs(quote.ask_price.as_decimal() - self.target_premium)
            
            # Check option type using option_kind attribute
            if instr.option_kind == OptionKind.CALL and diff < min_call_diff:
                min_call_diff, best_call = diff, opt_id
                self.log.info(f"Found Call candidate: {opt_id}, premium={quote.ask_price}, diff={diff}")
            elif instr.option_kind == OptionKind.PUT and diff < min_put_diff:
                min_put_diff, best_put = diff, opt_id
                self.log.info(f"Found Put candidate: {opt_id}, premium={quote.ask_price}, diff={diff}")

        if best_call and best_put:
            self.call_option_id, self.put_option_id = best_call, best_put
            self._submit_entry_limit_order(best_call)
            self._submit_entry_limit_order(best_put)
            self.entry_orders_submitted = True
            
            # Unsubscribe from options we didn't select
            for opt_id in list(self.candidate_options):
                if opt_id not in [best_call, best_put]:
                    self.unsubscribe_quotes(opt_id)
                    self.candidate_options.remove(opt_id)
            
            self.clock.set_timer("timeout_exit", timedelta(seconds=self.timeout_seconds), self._handle_timeout_exit)
        else:
            self.log.warn(f"Не знайдено Call/Put з премією ~{self.target_premium}")
            self._unsubscribe_all_candidates()
            self.is_active_today = False  # Reset so we can try again

    def _submit_entry_limit_order(self, instrument_id: InstrumentId):
        quote = self.cache.last_quote(instrument_id)
        if not quote: return
        mid_price = quote.mid_price()
        order = self.order_factory.limit(instrument_id, OrderSide.BUY, mid_price, Quantity.from_int(1), TimeInForce.GTC)
        self.submit_order(order)

    def on_order_filled(self, event: OrderFilled):
        if event.instrument_id == self.call_option_id: self._call_filled = True
        elif event.instrument_id == self.put_option_id: self._put_filled = True

    def on_order_rejected(self, event: OrderRejected):
        if event.instrument_id in [self.call_option_id, self.put_option_id]:
            self.entry_orders_submitted = False

    def _check_exit_conditions(self, current_price: Price):
        if not self.opening_price or not self.entry_orders_submitted: return
        price_diff = current_price.as_decimal() - self.opening_price.as_decimal()

        if self._call_filled and self.call_option_id and price_diff >= self.call_profit_trigger:
            if self._is_position_active(self.call_option_id):
                self._close_position(self.call_option_id, "Call target reached")
                self._call_filled = False

        if self._put_filled and self.put_option_id and price_diff <= -self.put_profit_trigger:
            if self._is_position_active(self.put_option_id):
                self._close_position(self.put_option_id, "Put target reached")
                self._put_filled = False

    def _is_position_active(self, instrument_id: InstrumentId) -> bool:
        pos = self.cache.position(self.get_account_id(instrument_id), instrument_id)
        return pos is not None and not pos.is_flat

    def _close_position(self, instrument_id: InstrumentId, reason: str):
        quote = self.cache.last_quote(instrument_id)
        pos = self.cache.position(self.get_account_id(instrument_id), instrument_id)
        if not pos or pos.is_flat: return
        side = OrderSide.SELL if pos.is_long else OrderSide.BUY
        price = quote.mid_price() if quote else None
        order = self.order_factory.limit(instrument_id, side, price, pos.quantity.abs(), TimeInForce.GTC) if price else self.order_factory.market(instrument_id, side, pos.quantity.abs())
        self.submit_order(order)
        self.unsubscribe_quotes(instrument_id)

    def _handle_timeout_exit(self, event: Event):
        for opt_id in [self.call_option_id, self.put_option_id]:
            if opt_id and self._is_position_active(opt_id):
                self._close_position(opt_id, "Timeout")
        self._call_filled = self._put_filled = False

    def _unsubscribe_all_candidates(self):
        for opt_id in self.candidate_options:
            self.unsubscribe_quotes(opt_id)
        self.candidate_options.clear()

    def on_stop_safe(self):
        """Cleanup when stopping the strategy."""
        self._unsubscribe_all_candidates()
        # Cancel any pending orders for our option instruments
        if self.call_option_id:
            try:
                self.cancel_all_orders(self.call_option_id)
            except Exception as e:
                self.log.warning(f"Failed to cancel orders for {self.call_option_id}: {e}")
        if self.put_option_id:
            try:
                self.cancel_all_orders(self.put_option_id)
            except Exception as e:
                self.log.warning(f"Failed to cancel orders for {self.put_option_id}: {e}")

    def get_state(self) -> Dict[str, Any]:
        return {
            "last_trade_date": self.last_trade_date.isoformat() if self.last_trade_date else None,
            "opening_price": float(self.opening_price) if self.opening_price else None,
            "is_active_today": self.is_active_today,
        }

    def set_state(self, state: Dict[str, Any]):
        if state.get("last_trade_date"): self.last_trade_date = datetime.fromisoformat(state["last_trade_date"]).date()
        if state.get("opening_price"): self.opening_price = Price.from_str(str(state["opening_price"]))
        self.is_active_today = state.get("is_active_today", False)