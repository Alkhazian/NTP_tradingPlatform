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
        self.logger.info(f"Starting {self.__class__.__name__} with config ID: {self.tsla_id}")
        
        # Setup periodic check for instrument availability
        self.clock.set_timer(
            name="check_instrument",
            interval=timedelta(seconds=5), 
            callback=self._check_instrument_availability
        )

        instrument = self.cache.instrument(self.tsla_id)
        if instrument:
            self.logger.info(f"Found instrument {self.tsla_id} in cache.")
            self._initialize_strategy_logic()
        else:
            self.logger.info(f"Instrument {self.tsla_id} not in cache. Requesting...")
            self._request_base_instrument()

    def _request_base_instrument(self):
        self.request_instruments(
            venue=Venue("InteractiveBrokers"),
            params={"ib_contracts": [{"secType": "STK", "symbol": "TSLA", "exchange": "SMART", "currency": "USD"}]}
        )

    def _initialize_strategy_logic(self):
        self.subscribe_trade_ticks(self.tsla_id)
        self.logger.info(f"Subscribed to trade ticks for {self.tsla_id}. Waiting for incoming data...")

    def _check_instrument_availability(self, event):
        if self.is_active_today: return 

        instrument = self.cache.instrument(self.tsla_id)
        if instrument:
             self.logger.info(f"[Timer] Found instrument {self.tsla_id}. Initializing...")
             self._initialize_strategy_logic()
        else:
             self.logger.info(f"[Timer] Instrument {self.tsla_id} still not in cache.")
             if not self._discovery_in_progress: # Only re-request if we aren't already searching for options
                 self._request_base_instrument()
        
        # Schedule next check if not active
        if not self.is_active_today:
            self.clock.set_timer(
                name="check_instrument",
                interval=timedelta(seconds=10),
                callback=self._check_instrument_availability
            )

    def on_instrument_added(self, instrument: Instrument):
        self.logger.info(f"Instrument added: {instrument.id} (Symbol: {instrument.symbol})")
        
        # Check for direct match or symbol match (handling ID mismatch)
        is_match = instrument.id == self.tsla_id
        if not is_match and instrument.symbol == "TSLA" and instrument.id.venue.value == "InteractiveBrokers":
             self.logger.warning(f"ID Mismatch detected! Config expects {self.tsla_id}, but received {instrument.id}. Updating to use {instrument.id}.")
             self.tsla_id = instrument.id
             is_match = True

        if is_match:
            self.logger.info(f"Subscribing to {self.tsla_id}")
            self._initialize_strategy_logic()
            return

        if self._discovery_in_progress and hasattr(instrument, 'underlying_id'):
            if instrument.underlying_id == self.tsla_id:
                self.logger.info(f"Candidate option found: {instrument.id}")
                self.subscribe_quotes(instrument.id)
                self.candidate_options.add(instrument.id)

    def on_stop_safe(self):
        self.logger.info(f"Stopping {self.__class__.__name__}")
        self._unsubscribe_all_candidates()
        try:
            self.cancel_all_orders(self.tsla_id)
        except Exception as e:
            self.logger.warning(f"Error canceling orders for {self.tsla_id}: {e}")

    def on_trade_tick(self, tick: TradeTick):
        if tick.instrument_id != self.tsla_id: return
        self._handle_entry_logic(tick)
        self._check_exit_conditions(tick.price)

    def _handle_entry_logic(self, tick: TradeTick):
        current_date = self.clock.utc_now().date()
        if not self.is_active_today and (self.last_trade_date is None or self.last_trade_date != current_date):
            self.logger.info(f"Initiating entry logic for date: {current_date}")
            self.last_trade_date = current_date
            self.save_state() # Persist immediately
            self.is_active_today = True
            self._initiate_discovery(tick.price)
        else:
            self.logger.info(f"Skipping entry. Active today: {self.is_active_today}, Last trade: {self.last_trade_date}, Current: {current_date}")

    def _initiate_discovery(self, current_price: Price):
        """Запит опціонів без жорсткої дати — запитуємо поточний місяць."""
        self.opening_price = current_price
        self._discovery_in_progress = True
        self.candidate_options.clear()
        
        # Отримуємо поточний місяць у форматі YYYYMM
        current_month = self.clock.utc_now().strftime("%Y%m")
        
        self.logger.info(f"Запит опціонів на місяць {current_month} для пошуку найближчої експірації...")

        self.request_instruments(
            venue=Venue("InteractiveBrokers"),
            params={
                "ib_contracts": [
                    {
                        "secType": "OPT", 
                        "symbol": "TSLA", 
                        "exchange": "SMART", 
                        "currency": "USD", 
                        "lastTradeDateOrContractMonth": current_month, # Тільки місяць!
                        # Ми не вказуємо страйк тут, щоб отримати список доступних дат
                    }
                ]
            }
        )

        # Чекаємо трохи довше (15с), бо запит місяця повертає багато даних
        self.clock.set_timer(
            name="execute_entry",
            interval=timedelta(seconds=15),
            callback=self._execute_entry_from_quotes
        )

    def _execute_entry_from_quotes(self, event: Event):
        """Вибір найближчої дати експірації серед отриманих кандидатів."""
        if self.entry_orders_submitted: return
        self._discovery_in_progress = False

        now_dt = self.clock.utc_now()
        
        # 1. Знаходимо всі доступні дати експірації серед кандидатів
        available_expiries = []
        for opt_id in self.candidate_options:
            instr = self.cache.instrument(opt_id)
            if instr and instr.expiration_date:
                days_to_expiry = (instr.expiration_date - now_dt.date()).days
                if days_to_expiry >= self.min_days_to_expiry:
                    available_expiries.append(instr.expiration_date)
        
        if not available_expiries:
            self.logger.error("Не знайдено жодної доступної дати експірації.")
            self._unsubscribe_all_candidates()
            return

        # 2. Вибираємо найближчу дату
        nearest_expiry = min(available_expiries)
        self.logger.info(f"Найближча знайдена експірація: {nearest_expiry}")

        # 3. Тепер шукаємо найкращі Call/Put саме для цієї дати
        best_call, best_put = None, None
        min_call_diff, min_put_diff = Decimal('inf'), Decimal('inf')

        for opt_id in self.candidate_options:
            instr = self.cache.instrument(opt_id)
            if instr.expiration_date != nearest_expiry:
                continue
            
            quote = self.cache.last_quote(opt_id)
            if not quote or not quote.ask_price: continue
            
            diff = abs(quote.ask_price.as_decimal() - self.target_premium)
            if instr.is_call and diff < min_call_diff:
                min_call_diff, best_call = diff, opt_id
            elif instr.is_put and diff < min_put_diff:
                min_put_diff, best_put = diff, opt_id

        if best_call and best_put:
            self.call_option_id, self.put_option_id = best_call, best_put
            self._submit_entry_limit_order(best_call)
            self._submit_entry_limit_order(best_put)
            self.entry_orders_submitted = True
            
            # Відписуємось від усього, що не підійшло за датою або ціною
            for opt_id in list(self.candidate_options):
                if opt_id not in [best_call, best_put]:
                    self.unsubscribe_quotes(opt_id)
                    self.candidate_options.remove(opt_id)
            
            self.clock.set_timer("timeout_exit", timedelta(seconds=self.timeout_seconds), self._handle_timeout_exit)
        else:
            self.logger.warn(f"Не знайдено Call/Put для дати {nearest_expiry} з премією ~{self.target_premium}")
            self._unsubscribe_all_candidates()

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