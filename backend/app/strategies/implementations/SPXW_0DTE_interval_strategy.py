"""
SPXW 0DTE Interval Strategy
Купує найближчий ATM Call опціон SPXW 0DTE та продає його через 60 секунд.
Повторює цикл поки стратегія активна.
"""

from decimal import Decimal
from typing import Dict, Any
import asyncio

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Venue, Symbol
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.instruments import Instrument

from app.strategies.base import BaseStrategy
from app.strategies.config import StrategyConfig


class Spxw0DteIntervalStrategy(BaseStrategy):
    """
    Стратегія для торгівлі опціонами SPXW 0DTE.
    Купує найближчий ATM Call опціон та продає його через 60 секунд.
    Повторює цикл автоматично.
    """

    def __init__(self, config: StrategyConfig, integration_manager=None, persistence_manager=None):
        super().__init__(config, integration_manager, persistence_manager)
        
        # Параметри з конфігурації
        # Використовуємо instrument_id з конфігурації (наприклад ^SPX.CBOE)
        # BaseStrategy вже встановив self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.underlying_instrument_id = self.instrument_id  # ^SPX.CBOE
        self.option_symbol_prefix = "SPXW"
        self.interval_seconds = config.parameters.get("interval_seconds", 60)
        self.quantity = config.parameters.get("quantity", 1)
        
        # Стан стратегії
        self.active_option_id = None
        self.entry_time_ns = None
        self.is_waiting_for_exit = False
        self.current_spx_price = 0.0
        
        # Прапорці для запиту інструментів
        self.spx_subscribed = False
        self.options_requested = False

    def on_start_safe(self):
        """
        Викликається після того, як базова стратегія готова.
        """
        self.logger.info(f"SPXW 0DTE Strategy starting with {self.interval_seconds}s interval")
        
        # Явно підписуємось на котирування SPX
        self.logger.info(f"Subscribing to SPX quotes on {self.underlying_instrument_id}...")
        try:
            self.subscribe_quote_ticks(self.underlying_instrument_id)
            self.logger.info(f"✅ Successfully subscribed to {self.underlying_instrument_id}")
            self.spx_subscribed = True
        except Exception as e:
            self.logger.error(f"❌ Failed to subscribe: {e}", exc_info=True)

    def _subscribe_data(self):
        """
        Підписуємось на котирування SPX.
        Викликається автоматично BaseStrategy при старті.
        """
        self.logger.info(f"_subscribe_data() called! underlying_instrument_id={self.underlying_instrument_id}")
        try:
            self.subscribe_quote_ticks(self.underlying_instrument_id)
            self.logger.info(f"Successfully subscribed to quote ticks for {self.underlying_instrument_id}")
        except Exception as e:
            self.logger.error(f"Failed to subscribe to quote ticks: {e}", exc_info=True)
        # Підписуємось на котирування SPX.
        # Викликається автоматично BaseStrategy при старті.
        self.subscribe_quote_ticks(self.underlying_instrument_id)
        self.logger.info(f"Subscribed to quote ticks for {self.underlying_instrument_id}")

    def on_quote_tick(self, tick: QuoteTick):
        
        # Обробка котирувань SPX.
        
        # Оновлюємо поточну ціну SPX
        bid = tick.bid_price.as_double()
        ask = tick.ask_price.as_double()
        
        if bid > 0 and ask > 0:
            self.current_spx_price = (bid + ask) / 2
        elif bid > 0:
            self.current_spx_price = bid
        elif ask > 0:
            self.current_spx_price = ask
        else:
            return
        
        # Request specific SPXW options when we first get SPX price
        if not self.options_requested and self.current_spx_price > 0:
            self._request_spxw_options()
        
        # Логуємо кожні 10 секунд для діагностики
        if int(self.clock.timestamp_ns() / 1_000_000_000) % 10 == 0:
            self.logger.info(f"SPX Price: {self.current_spx_price:.2f}")
        
        # Якщо чекаємо на вихід, перевіряємо умову
        if self.is_waiting_for_exit:
            self._check_exit_condition()
            return
        
        # Якщо немає активної позиції, намагаємось увійти
        if self.active_option_id is None and not self._has_open_position():
            self._try_entry()
    
    def _request_spxw_options(self):
        """Request specific SPXW Call option around current SPX price"""
        if self.current_spx_price == 0:
            return
        
        # Get today's date in IB format (YYYYMMDD)
        today = self.clock.utc_now().date()
        expiry_date_ib = today.strftime("%Y%m%d")
        
        # Calculate ATM strike (round to nearest 5)
        atm_strike = round(self.current_spx_price / 5) * 5
        
        self.logger.info(f"Requesting SPXW Call option: ATM strike={atm_strike}, expiry={expiry_date_ib}")
        
        try:
            # Request only ATM Call (we only trade Calls)
            self.request_instruments(
                venue=Venue("InteractiveBrokers"),
                params={
                    "ib_contracts": [
                        {
                            "secType": "OPT",
                            "symbol": "SPXW",
                            "exchange": "CBOE",
                            "currency": "USD",
                            "lastTradeDateOrContractMonth": expiry_date_ib,
                            "strike": atm_strike,
                            "right": "C"  # Call only
                        }
                    ]
                }
            )
            self.logger.info(f"✅ Requested SPXW {expiry_date_ib} ATM {atm_strike} Call")
            self.options_requested = True
        except Exception as e:
            self.logger.error(f"❌ Failed to request Call option: {e}", exc_info=True)

    def _try_entry(self):
        """
        Намагаємось знайти та купити ATM 0DTE Call опціон.
        """
        if self.current_spx_price == 0:
            return
        
        # Шукаємо опціони в кеші
        today = self.clock.utc_now().date()
        instruments = list(self.cache.instruments())
        
        # Фільтруємо SPXW 0DTE Call опціони
        options = []
        for inst in instruments:
            # Перевіряємо чи це опціон (має атрибути option_kind та strike_price)
            if not (hasattr(inst, 'option_kind') and hasattr(inst, 'strike_price')):
                continue
            
            # Перевіряємо символ
            symbol_str = str(inst.id.symbol.value)
            if not symbol_str.startswith(self.option_symbol_prefix):
                continue
            
            # Перевіряємо тип (Call) та дату експірації
            if (hasattr(inst, 'option_kind') and str(inst.option_kind) == 'OptionKind.CALL' and
                hasattr(inst, 'activation_ns') and inst.activation_ns):
                
                # Перевіряємо дату експірації (0DTE)
                # Примітка: activation_ns це timestamp експірації
                from datetime import datetime, timezone
                expiry_dt = datetime.fromtimestamp(inst.activation_ns / 1_000_000_000, tz=timezone.utc).date()
                
                if expiry_dt == today and hasattr(inst, 'strike_price'):
                    options.append(inst)
        
        if not options:
            # Логуємо кожні 10 секунд
            if self.clock.utc_now().second % 10 == 0:
                self.logger.info(f"No SPXW 0DTE Call options found in cache. Total instruments: {len(instruments)}")
            return
        
        # Знаходимо найближчий до ATM
        best_option = None
        min_distance = float('inf')
        
        for opt in options:
            distance = abs(float(opt.strike_price.as_double()) - self.current_spx_price)
            if distance < min_distance:
                min_distance = distance
                best_option = opt
        
        if best_option:
            self.logger.info(
                f"Found ATM 0DTE Call: {best_option.id} "
                f"(Strike: {best_option.strike_price}, SPX: {self.current_spx_price:.2f})"
            )
            
            # Створюємо ордер
            order = self.order_factory.market(
                instrument_id=best_option.id,
                order_side=OrderSide.BUY,
                quantity=Quantity.from_int(self.quantity),
            )
            
            # Зберігаємо стан
            self.active_option_id = best_option.id
            self.entry_time_ns = self.clock.timestamp_ns()
            self.is_waiting_for_exit = True
            
            # Відправляємо ордер
            self.submit_entry_order(order)
            
            # Підписуємось на котирування опціону
            self.subscribe_quote_ticks(best_option.id)
            
            self.logger.info(f"Submitted BUY order for {best_option.id}")
            self.save_state()

    def _check_exit_condition(self):
        """
        Перевіряємо чи настав час продавати.
        """
        if not self.active_option_id or not self.entry_time_ns:
            return
        
        current_time_ns = self.clock.timestamp_ns()
        elapsed_ns = current_time_ns - self.entry_time_ns
        interval_ns = self.interval_seconds * 1_000_000_000
        
        if elapsed_ns >= interval_ns:
            self.logger.info(f"Interval reached ({self.interval_seconds}s). Selling {self.active_option_id}")
            
            # Закриваємо позицію через базовий метод
            self.close_strategy_position(reason="INTERVAL_EXIT")
            
            # Скидаємо стан
            self.active_option_id = None
            self.is_waiting_for_exit = False
            self.entry_time_ns = None
            
            self.save_state()
            
            # Через 1 секунду можемо знову входити
            self.logger.info("Ready for next entry in 1 second...")

    def on_stop_safe(self):
        """
        Викликається при зупинці стратегії.
        BaseStrategy сам відпишеться від self.instrument_id (^SPX.CBOE).
        """
        self.logger.info("Stopping SPXW 0DTE strategy")
        
        # Якщо є відкрита позиція, закриваємо її
        if self._has_open_position():
            self.close_strategy_position(reason="STRATEGY_STOP")
        
        # Відписуємось від котирувань опціону (якщо є активний)
        if self.active_option_id:
            try:
                self.unsubscribe_quote_ticks(self.active_option_id)
                self.logger.info(f"Unsubscribed from {self.active_option_id}")
            except Exception as e:
                self.logger.error(f"Failed to unsubscribe from option: {e}")

    def get_state(self) -> Dict[str, Any]:
        """Повертаємо стан для збереження"""
        return {
            "active_option_id": str(self.active_option_id) if self.active_option_id else None,
            "entry_time_ns": self.entry_time_ns,
            "is_waiting_for_exit": self.is_waiting_for_exit,
            "current_spx_price": self.current_spx_price,
        }

    def set_state(self, state: Dict[str, Any]):
        """Відновлюємо стан"""
        if state.get("active_option_id"):
            self.active_option_id = InstrumentId.from_str(state["active_option_id"])
        self.entry_time_ns = state.get("entry_time_ns")
        self.is_waiting_for_exit = state.get("is_waiting_for_exit", False)
        self.current_spx_price = state.get("current_spx_price", 0.0)