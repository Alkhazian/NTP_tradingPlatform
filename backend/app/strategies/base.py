"""
Phase 1: Enhanced Base Strategy - Maximum Nautilus Integration
- Uses Nautilus ComponentState for lifecycle
- Uses Nautilus OrderStatus for order states
- Uses self.logger for logging
- Uses cache for order tracking
- Proper instrument subscription
- Position tracking from portfolio
- Duplicate order prevention
"""

from abc import abstractmethod
from typing import Dict, Any, Optional, Set, List, Tuple
from datetime import datetime, timedelta
import traceback
import logging
from decimal import Decimal, ROUND_HALF_UP

from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Venue
from nautilus_trader.model.enums import OrderSide, PositionSide, OrderStatus, TimeInForce
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import Order, OrderList
from nautilus_trader.model.position import Position
from nautilus_trader.common.enums import ComponentState

from .config import StrategyConfig


class BaseStrategy(Strategy):
    """
    Enhanced base class for all strategies.
    
    Maximum Nautilus integration:
    - Uses ComponentState for lifecycle
    - Uses OrderStatus for order states
    - Uses cache for order tracking
    
    Handles:
    - Instrument subscription and lifecycle
    - Position tracking and reconciliation
    - Order management and duplicate prevention
    - State persistence integration
    - Error isolation
    """

    def __init__(
        self, 
        config: StrategyConfig, 
        integration_manager=None, 
        persistence_manager=None
    ):
        super().__init__(config=None)
        
        # Configuration
        self.strategy_config = config
        self.strategy_id = config.id
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        
        # Standard logging
        self.logger = logging.getLogger(f"strategy.{self.strategy_id}")
        
        # Managers
        self._integration_manager = integration_manager
        self.persistence = persistence_manager
        
        # Set account ID from manager if available
        if self._integration_manager and hasattr(self._integration_manager, 'nautilus_account_id'):
            self.account_id = self._integration_manager.nautilus_account_id
            self.logger.info(f"Strategy account ID set to: {self.account_id}")
        
        # Instrument state
        self.instrument: Optional[Instrument] = None
        self._instrument_ready = False
        
        # Functional readiness (separate from Nautilus ComponentState)
        self._functional_ready = False
        
        # Order tracking for duplicate prevention and categorization
        # We keep these sets to track order PURPOSE (entry/exit/sl/tp)
        # The actual order state comes from cache via order.status
        self._pending_entry_orders: Set[ClientOrderId] = set()
        self._pending_exit_orders: Set[ClientOrderId] = set()
        
        # Position tracking
        self._last_entry_price: Optional[float] = None
        self._last_entry_qty: Optional[float] = None
        self.signed_inventory: float = 0.0  # + for Long, - for Short, 0 for Flat
        
        # Spread support
        self.spread_id: Optional[InstrumentId] = None
        self.spread_instrument: Optional[Instrument] = None
        self._spread_legs: List[Tuple[InstrumentId, int]] = []
        self._waiting_for_spread: bool = False
        
        # Spread order tracking for logging
        self._pending_spread_orders: Set[ClientOrderId] = set()
        
        # Trade persistence
        self.active_trade_id: Optional[int] = None
        
        # Bracket order tracking (mapping ClientOrderId of exit legs to reason)
        self._bracket_exit_map: Dict[ClientOrderId, str] = {}
        
        # Sequential bracket tracking (intent to submit exits after entry fill)
        # Entry ClientOrderId -> {sl_price, tp_price, ...}
        self._pending_bracket_exits: Dict[ClientOrderId, Dict[str, Any]] = {}


    # =========================================================================
    # LIFECYCLE MANAGEMENT (Using Nautilus ComponentState)
    # =========================================================================

    def on_start(self):
        """
        Lifecycle hook: Called when strategy is started.
        Nautilus state: INITIALIZED -> STARTING -> RUNNING
        """
        self.logger.info(
            f"Strategy {self.strategy_id} starting...",
            extra={
                "extra": {
                    "event_type": "strategy_starting",
                    "strategy_id": self.strategy_id
                }
            }
        )
        
        # Check if strategy is enabled in configuration
        # If disabled, stop immediately to prevent execution
        if not self.strategy_config.enabled:
            self.logger.info(f"Strategy {self.strategy_id} is disabled (enabled=False), stopping...")
            self.stop()
            return
        
        try:
            self.load_state()
            self._request_instrument()
        except Exception as e:
            self.on_unexpected_error(e)

    def _request_instrument(self):
        """Request instrument and wait for it to be available."""
        # First check cache
        self.instrument = self.cache.instrument(self.instrument_id)
        
        if self.instrument is not None:
            self._on_instrument_ready()
        else:
            # Re-request if not in cache (may be missing from catalog)
            self.logger.info(
                f"Instrument {self.instrument_id} not found in cache | State: WAITING",
                extra={
                    "extra": {
                        "event_type": "instrument_not_cached",
                        "instrument_id": str(self.instrument_id),
                        "state": "WAITING"
                    }
                }
            )
            
            # Set timeout for instrument availability
            self.clock.set_time_alert(
                name=f"{self.id}.instrument_timeout",
                alert_time=self.clock.utc_now() + timedelta(seconds=60),
                callback=self._on_instrument_timeout
            )

    def on_instrument(self, instrument: Instrument):
        """Called when instrument data is received (including spreads)."""
        # Check if this is our primary instrument
        if instrument.id == self.instrument_id:
            self.logger.info(f"Instrument received: {instrument.id}")
            self.instrument = instrument
            self._on_instrument_ready()
        
        # Check if this is our spread instrument
        elif self._waiting_for_spread and self.spread_id and instrument.id == self.spread_id:
            self.spread_instrument = instrument
            self._waiting_for_spread = False
            self.logger.info(f"Spread instrument loaded successfully: {instrument.id}")
            
            # Subscribe to spread quotes (spreads often only have Bid/Ask, not trades)
            self.subscribe_quote_ticks(instrument.id)
            
            # Call hook for derived strategies
            self.on_spread_ready(instrument)

    def _on_instrument_ready(self):
        """Called when instrument is confirmed available."""
        if self._instrument_ready:
            return

        self._instrument_ready = True
        self.logger.info(
            f"Instrument {self.instrument_id} ready",
            extra={
                "extra": {
                    "event_type": "instrument_ready",
                    "instrument_id": str(self.instrument_id)
                }
            }
        )

        try:
            # Wrap *everything* in the start path so no exception is re-raised
            try:
                # Cancel timeout (ignore cancel errors)
                try:
                    self.clock.cancel_timer(f"{self.id}.instrument_timeout")
                except Exception:
                    pass

                # Reconcile positions with reality
                self._reconcile_positions()

                # Subscribe to data feeds
                self._subscribe_data()

                # Mark as functionally ready
                self._functional_ready = True

                # Call strategy-specific startup hook, but protect it as well
                try:
                    self.on_start_safe()
                except Exception as e:
                    # Strategy hook error should be logged but must not bubble
                    self.on_unexpected_error(e)

            except Exception as inner_exc:
                # Any unexpected error during initialization — log, do not re-raise
                self.on_unexpected_error(inner_exc)

        except Exception as top_exc:
            # Defensive: should never be here, but ensure nothing escapes
            self.on_unexpected_error(top_exc)


    def _on_instrument_timeout(self, alert):
        """Handle instrument request timeout."""
        if not self._instrument_ready:
            self.logger.error(
                f"Timeout waiting for instrument {self.instrument_id} | Strategy cannot start",
                extra={
                    "extra": {
                        "event_type": "instrument_timeout",
                        "instrument_id": str(self.instrument_id)
                    }
                }
            )

    def _subscribe_data(self):
        """Subscribe to required data feeds. Override in strategy if needed."""
        pass

    def on_stop(self):
        """
        Lifecycle hook: Called when strategy is stopped.
        Nautilus state: RUNNING -> STOPPING -> STOPPED
        """
        self.logger.info(f"Strategy {self.strategy_id} stopping...")
        try:
            self.on_stop_safe()
            self.save_state()
        except Exception as e:
            self.on_unexpected_error(e)

    def on_reset(self):
        """
        Lifecycle hook: Called when strategy is reset.
        Nautilus state: STOPPED -> RESETTING -> INITIALIZED
        """
        self.logger.info(f"Strategy {self.strategy_id} resetting...")
        try:
            self._functional_ready = False
            self._instrument_ready = False
            self.on_reset_safe()
        except Exception as e:
            self.on_unexpected_error(e)

    def on_resume(self):
        """
        Lifecycle hook: Called when strategy is resumed.
        Nautilus state: STOPPED -> RESUMING -> RUNNING
        """
        self.logger.info(f"Strategy {self.strategy_id} resuming...")
        try:
            self.on_resume_safe()
        except Exception as e:
            self.on_unexpected_error(e)

    # =========================================================================
    # SPREAD MANAGEMENT (Option Spreads / Combos)
    # =========================================================================
    #
    # USAGE GUIDE - Spread Trading Workflow
    # ======================================
    #
    # This section provides methods for trading option spreads (combos) with IBKR
    # and other brokers that support multi-leg instruments.
    #
    # LIFECYCLE:
    # ----------
    # 1. CREATE:    create_and_request_spread(legs_config) -> spread_id
    # 2. WAIT:      Override on_spread_ready(instrument) to be notified
    # 3. OPEN:      open_spread_position(quantity, is_buy, limit_price)
    # 4. MONITOR:   get_effective_spread_quantity() -> current position
    # 5. CLOSE:     close_spread_smart() OR close_spread_position()
    #
    # KEY METHODS:
    # ------------
    # create_and_request_spread() - Creates spread ID and requests from broker
    # on_spread_ready()           - Override this callback in your strategy
    # open_spread_position()      - Opens position (prefer LIMIT orders!)
    # get_spread_position()       - Gets native combo position only
    # get_effective_spread_quantity() - Gets position including legged-out!
    # close_spread_position()     - Closes native combo only
    # close_spread_smart()        - Closes BOTH native and legged-out positions
    #
    # IMPORTANT - TWO POSITION SCENARIOS:
    # ------------------------------------
    # 1. NATIVE COMBO (Atomic):
    #    Broker holds spread as single instrument (best for margin).
    #    Use: get_spread_position() or close_spread_position()
    #
    # 2. LEGGED OUT (Scattered):
    #    Broker executed spread but reports individual legs in portfolio.
    #    DANGER: get_spread_position() returns None, but you have risk!
    #    Use: get_effective_spread_quantity() and close_spread_smart()
    #
    # EXAMPLE USAGE:
    # --------------
    # class MySpreadStrategy(BaseStrategy):
    #     def on_start_safe(self):
    #         # Create a Call Spread: Buy lower strike, Sell higher strike
    #         self.create_and_request_spread([
    #             (call_4000_id, 1),   # Buy leg (ratio +1)
    #             (call_4050_id, -1),  # Sell leg (ratio -1)
    #         ])
    #
    #     def on_spread_ready(self, instrument):
    #         # Now safe to trade - open 5 spreads
    #         self.open_spread_position(5, is_buy=True, limit_price=2.50)
    #
    #     def check_position(self):
    #         # ALWAYS use get_effective_spread_quantity() for safety!
    #         qty = self.get_effective_spread_quantity()
    #         if abs(qty) > 0:
    #             self.logger.info(f"Position: {qty} spreads (may be legged)")
    #
    #     def close_all(self):
    #         # ALWAYS use close_spread_smart() for safety!
    #         self.close_spread_smart()  # Handles both native and legged
    #
    # =========================================================================

    def create_and_request_spread(
        self, 
        legs_config: List[Tuple["str | InstrumentId", int]], 
        timeout_seconds: int = 30
    ) -> Optional[InstrumentId]:
        """
        Create a spread identifier and request it from the data provider/broker.
        
        This method generates a deterministic spread ID from the leg configuration
        and requests the spread instrument from the broker (e.g., IBKR will create
        a SecurityType.BAG combo contract).
        
        Args:
            legs_config: List of tuples (InstrumentId or string, ratio).
                         Can pass either InstrumentId objects or string IDs.
                         Positive ratio = buy leg on spread buy
                         Negative ratio = sell leg on spread buy
                         Example with strings: [("SPY C400.SMART", 1), ("SPY P390.SMART", -1)]
                         Example with IDs: [(call_option_id, 1), (put_option_id, -1)]
            timeout_seconds: Timeout for waiting for the spread instrument.
            
        Returns:
            The generated spread InstrumentId, or None if creation failed.
            
        Note:
            The spread instrument is not immediately available. Listen for
            `on_spread_ready(instrument)` callback to know when it's ready.
        """
        try:
            # Guard against duplicate spread creation requests
            if self._waiting_for_spread:
                self.logger.warning(
                    f"Spread creation already in progress | Existing spread_id: {self.spread_id}",
                    extra={
                        "extra": {
                            "event_type": "spread_creation_skipped",
                            "reason": "already_in_progress",
                            "existing_spread_id": str(self.spread_id) if self.spread_id else None
                        }
                    }
                )
                return self.spread_id
            
            # Import the spread ID generator
            from nautilus_trader.model.identifiers import new_generic_spread_id
            
            # Convert leg IDs to InstrumentId objects (handles both str and InstrumentId)
            self._spread_legs = []
            for leg_id, ratio in legs_config:
                if isinstance(leg_id, str):
                    instrument_id = InstrumentId.from_str(leg_id)
                else:
                    instrument_id = leg_id  # Already an InstrumentId
                self._spread_legs.append((instrument_id, ratio))
            
            # Generate deterministic spread ID from legs
            self.spread_id = new_generic_spread_id(self._spread_legs)
            self._waiting_for_spread = True
            
            self.logger.info(
                f"Generated Spread ID: {self.spread_id} | Legs: {len(self._spread_legs)}",
                extra={
                    "extra": {
                        "event_type": "spread_id_generated",
                        "spread_id": str(self.spread_id),
                        "leg_count": len(self._spread_legs)
                    }
                }
            )
            
            # Log leg details
            # Log leg details
            legs_details = []
            for leg_id, ratio in self._spread_legs:
                action = "BUY" if ratio > 0 else "SELL"
                legs_details.append(f"{leg_id}:{action} x{abs(ratio)}")
                
            self.logger.info(
                f"Spread legs definition: {', '.join(legs_details)}",
                extra={
                    "extra": {
                        "event_type": "spread_legs_definition",
                        "legs": [
                            {
                                "instrument_id": str(leg_id),
                                "ratio": ratio,
                                "action": "BUY" if ratio > 0 else "SELL"
                            }
                            for leg_id, ratio in self._spread_legs
                        ]
                    }
                }
            )
            
            # Request the spread instrument from the broker (async)
            # For IBKR, this triggers creation of a BAG (combo) contract
            self.request_instrument(self.spread_id)
            
            # Set timeout for spread availability
            self.clock.set_time_alert(
                name=f"{self.id}.spread_timeout",
                alert_time=self.clock.utc_now() + timedelta(seconds=timeout_seconds),
                callback=self._on_spread_timeout
            )
            
            return self.spread_id
            
        except Exception as e:
            self.logger.error(f"Error creating spread: {e}")
            self._waiting_for_spread = False
            return None

    def _on_spread_timeout(self, alert):
        """Handle spread instrument request timeout."""
        if self._waiting_for_spread and self.spread_id:
            self._waiting_for_spread = False
            self.logger.error(
                f"Timeout waiting for spread instrument {self.spread_id} | Spread trading unavailable",
                extra={
                    "extra": {
                        "event_type": "spread_instrument_timeout",
                        "spread_id": str(self.spread_id)
                    }
                }
            )

    def on_spread_ready(self, instrument: Instrument):
        """
        Called when the spread instrument is confirmed available.
        Override in derived strategies to implement spread trading logic.
        
        Args:
            instrument: The loaded spread instrument with all contract details.
        """
        pass

    def open_spread_position(
        self, 
        quantity: float, 
        is_buy: bool = True,
        limit_price: Optional[float] = None,
        time_in_force: TimeInForce = TimeInForce.DAY
    ) -> bool:
        """
        Open a position on the spread as a single atomic trade.
        
        This submits an order for the entire spread, not individual legs.
        The broker (e.g., IBKR) guarantees atomic execution - all legs fill
        together or none do, eliminating leg risk.
        
        Args:
            quantity: Number of spread units to trade.
            is_buy: True for buy spread, False for sell spread.
            limit_price: Optional limit price. Recommended for spreads
                        as market orders may get poor fills.
            time_in_force: Order time in force (default: DAY).
            
        Returns:
            True if order was submitted, False otherwise.
            
        Note:
            For spreads, LIMIT orders are strongly recommended over MARKET
            orders to avoid poor execution on illiquid combinations.
        """
        if not self.spread_instrument:
            self.logger.error(
                f"❌ SPREAD ORDER FAILED | Instrument Not Loaded | ID: {self.spread_id}",
                extra={
                    "extra": {
                        "event_type": "spread_order_failed",
                        "spread_id": str(self.spread_id),
                        "reason": "spread_instrument is None",
                        "action": "Wait for on_spread_ready() callback"
                    }
                }
            )
            return False
            
        if not self._functional_ready:
            self.logger.error(
                f"❌ SPREAD ORDER FAILED | Strategy Not Ready | ID: {self.spread_id}",
                extra={
                    "extra": {
                        "event_type": "spread_order_failed",
                        "spread_id": str(self.spread_id),
                        "reason": "strategy not functional ready"
                    }
                }
            )
            return False

        side = OrderSide.BUY if is_buy else OrderSide.SELL
        
        # Create proper Quantity using instrument specs (precision, lot size)
        qty = self.spread_instrument.make_qty(quantity)

        # Create order - prefer LIMIT for spreads
        if limit_price is not None:
            # Automatically round to nearest valid tick
            rounded_price = self.round_to_tick(limit_price, self.spread_instrument)
            price = self.spread_instrument.make_price(rounded_price)
            order = self.order_factory.limit(
                instrument_id=self.spread_instrument.id,
                order_side=side,
                quantity=qty,
                price=price,
                time_in_force=time_in_force,
            )
            order_type = "LIMIT"
            price_str = f"{rounded_price:.4f} (original: {limit_price:.4f})"
        else:
            # Market order (use with caution for spreads)
            order = self.order_factory.market(
                instrument_id=self.spread_instrument.id,
                order_side=side,
                quantity=qty,
                time_in_force=time_in_force,
            )
            order_type = "MARKET"
            price_str = "N/A"
            self.logger.warning(
                "⚠️ Using MARKET order for spread | Consider LIMIT order for better execution",
                extra={
                    "extra": {
                        "event_type": "spread_order_warning",
                        "warning_type": "market_order_usage"
                    }
                }
            )
        
        # Track this as a spread order
        self._pending_spread_orders.add(order.client_order_id)
        
        # Submit order
        self.submit_order(order)
        
        # Log detailed order info
        self.logger.info(
            f"📤 SPREAD ORDER SUBMITTED TO BROKER| ID: {order.client_order_id} | {side.name} {qty} @ {price_str}",
            extra={
                "extra": {
                    "event_type": "spread_order_submitted",
                    "order_id": str(order.client_order_id),
                    "instrument_id": str(self.spread_instrument.id),
                    "side": side.name,
                    "quantity": str(qty),
                    "order_type": order_type,
                    "price_details": price_str,
                    "time_in_force": time_in_force.name,
                    "legs": [f"{leg_id} x{ratio}" for leg_id, ratio in self._spread_legs],
                    "status": "PENDING (waiting for broker confirmation)"
                }
            }
        )
        
        return True

    def close_spread_position(self) -> bool:
        """
        Close all positions on the spread.
        
        Uses Nautilus close_all_positions which will submit an offsetting
        order for the entire spread position.
        
        Returns:
            True if close was initiated, False if no spread position exists.
        """
        if not self.spread_id:
            self.logger.warning("No spread ID configured, nothing to close.")
            return False
        
        # Check if we have an open position on the spread
        positions = self.cache.positions_open(instrument_id=self.spread_id)
        if not positions:
            self.logger.info("No open spread positions to close.")
            return False
        
        pos = positions[0]
        pos_qty = float(pos.quantity)
        pos_side = pos.side.name
        
        self.logger.info(
            f"📤 CLOSING SPREAD POSITION | ID: {self.spread_id} | Qty: {pos_qty}",
            extra={
                "extra": {
                    "event_type": "spread_close_initiated",
                    "spread_id": str(self.spread_id),
                    "position_qty": pos_qty,
                    "position_side": pos_side,
                    "avg_entry_price": float(pos.avg_px_open),
                    "method": "close_all_positions",
                    "status": "PENDING (waiting for broker confirmation)"
                }
            }
        )
        
        self.close_all_positions(self.spread_id)
        return True

    def get_spread_position(self) -> Optional[Position]:
        """
        Get the current open position for the spread if it exists.
        
        Returns:
            The Position object, or None if no spread position.
        """
        if not self.spread_id:
            return None
            
        positions = self.cache.positions_open(instrument_id=self.spread_id)
        return positions[0] if positions else None

    # =========================================================================
    # SPREAD RECONCILIATION & LEGGING HANDLING
    # =========================================================================

    def get_effective_spread_quantity(self) -> float:
        """
        Розраховує реальну позицію по спреду, перевіряючи як сам інструмент спреду,
        так і його окремі ноги (якщо брокер 'розсипав' позицію).
        
        Handles two scenarios:
        1. Native Combo (Atomic): Broker holds the spread as a single instrument
        2. Legged Out (Scattered): Broker executed the spread but reports individual legs

        Returns:
            float: Кількість повних спредів (позитивне = Long, негативне = Short).
                   Повертає 0.0, якщо позицій немає.
        """
        if not self.spread_id or not self._spread_legs:
            return 0.0

        # 1. Спроба знайти "цілісну" позицію по спреду (Native Combo)
        direct_positions = self.cache.positions_open(instrument_id=self.spread_id)
        if direct_positions:
            pos = direct_positions[0]
            qty = float(pos.quantity)
            return qty if pos.side == PositionSide.LONG else -qty

        # 2. Якщо цілісної позиції немає, перевіряємо "синтетичну" через ноги
        # Логіка: ми шукаємо мінімальну спільну кількість, яку утворюють ноги.
        
        potential_spread_qtys = []
        
        for leg_id, leg_ratio in self._spread_legs:
            # Отримуємо всі позиції по цій нозі
            leg_positions = self.cache.positions_open(instrument_id=leg_id)
            
            # Рахуємо чисту позицію (Net Position) по нозі
            net_leg_qty = 0.0
            for p in leg_positions:
                q = float(p.quantity)
                net_leg_qty += q if p.side == PositionSide.LONG else -q
            
            if net_leg_qty == 0:
                # Якщо хоча б однієї ноги немає -> спреду немає
                return 0.0
            
            # Розраховуємо, скільки спредів утворює ця нога
            # Spread Qty = Leg Qty / Leg Ratio
            # Приклад: Leg Qty = -5 (Short), Ratio = -1 (Sell leg) -> Spread = 5 (Long)
            implied_spread_qty = net_leg_qty / leg_ratio
            potential_spread_qtys.append(implied_spread_qty)

        if not potential_spread_qtys:
            return 0.0

        # Перевірка на цілісність ("Broken Spread")
        # В ідеалі всі ноги повинні давати однакову кількість спредів.
        # Якщо ні -> у вас "розбитий" спред, беремо мінімальну по модулю, або 0.
        
        first_qty = potential_spread_qtys[0]
        is_broken = not all(abs(q - first_qty) < 1e-9 for q in potential_spread_qtys)
        
        if is_broken:
            self.logger.critical(
                f"BROKEN SPREAD DETECTED | Leg quantities do not match ratios |Implied quantities per leg: {potential_spread_qtys}",
                extra={
                    "extra": {
                        "event_type": "broken_spread_detected",
                        "implied_quantities": potential_spread_qtys
                    }
                }
            )
            # Тут рішення залежить від ризик-менеджменту. 
            # Безпечно повернути мінімальне значення або 0, щоб не закрити зайве.
            return 0.0 

        return first_qty

    def close_spread_smart(self, limit_price: Optional[float] = None) -> bool:
        """
        Розумне закриття спреду через зворотній комбо-ордер.
        
        IB завжди репортить позиції по спреду як окремі ноги, тому ми:
        1. Перевіряємо effective quantity через ноги
        2. Закриваємо через ЗВОРОТНІЙ комбо-ордер (не окремі ноги!)
        
        This approach avoids IB rejecting individual leg orders due to 
        "options strategy permissions" and ensures atomic closure.
        
        Args:
            limit_price: Optional limit price for the closing order.
                        If None, uses current mid price from spread quote.
        
        Returns:
            True if close order was submitted, False if no position exists.
        """
        effective_qty = self.get_effective_spread_quantity()
        
        if abs(effective_qty) < 1e-9:
            self.logger.info("No effective spread position to close.")
            return False

        # Визначаємо напрямок закриття
        # effective_qty > 0 означає LONG spread → потрібно SELL
        # effective_qty < 0 означає SHORT spread → потрібно BUY
        is_long = effective_qty > 0
        close_qty = abs(effective_qty)
        close_side = "SELL" if is_long else "BUY"
        
        # Отримуємо limit price для закриття
        closing_limit_price = limit_price
        if closing_limit_price is None and self.spread_instrument:
            # Спробуємо отримати поточний quote
            try:
                quote = self.cache.quote_tick(self.spread_instrument.id)
                if quote and quote.bid_price and quote.ask_price:
                    bid = float(quote.bid_price)
                    ask = float(quote.ask_price)
                    mid = (bid + ask) / 2
                    # Для закриття використовуємо mid price
                    closing_limit_price = mid
                    self.logger.info(f"   Using mid price for close: {mid:.4f} (bid={bid:.4f}, ask={ask:.4f})")
            except Exception as e:
                self.logger.warning(f"Could not get quote for spread, using market order: {e}")
        
        self.logger.info(
            f"📤 CLOSING SPREAD (Smart - Reverse Combo Order) | {close_side} {close_qty} | Limit: {closing_limit_price if closing_limit_price else 'MARKET'}",
            extra={
                "extra": {
                    "event_type": "spread_close_smart_initiated",
                    "spread_id": str(self.spread_id),
                    "effective_qty": effective_qty,
                    "close_direction": close_side,
                    "close_qty": close_qty,
                    "limit_price": closing_limit_price,
                    "mode": "REVERSE_COMBO_ORDER",
                    "Note": "IB scattered legs into individual positions, but we close via combo order for atomicity."
                }
            }
        )
        
        # Закриваємо через зворотній комбо-ордер
        # is_buy=False якщо ми LONG (потрібно SELL)
        # is_buy=True якщо ми SHORT (потрібно BUY)
        return self.open_spread_position(
            quantity=close_qty,
            is_buy=not is_long,  # Протилежний напрямок
            limit_price=closing_limit_price,
            time_in_force=TimeInForce.DAY
        )

    # =========================================================================
    # POSITION MANAGEMENT
    # =========================================================================

    def _reconcile_positions(self):
        """
        Reconcile internal state with actual portfolio positions.
        
        IMPORTANT: To support multiple strategies on the same instrument,
        we ONLY reconcile if we have an active_trade_id from a previous session.
        This means we only adopt positions we actually opened.
        """
        pos = self._get_open_position()
        
        if pos:
            # CRITICAL: Only reconcile if we have proof of ownership (active_trade_id)
            # This prevents "stealing" positions opened by other strategies
            if self.active_trade_id is not None:
                self.logger.info(
                    f"Resuming ownership of position ({pos.instrument_id}) "
                    f"with trade_id={self.active_trade_id}"
                )
                self._on_position_reconciled(pos)
            else:
                # Position exists but we don't own it - likely another strategy's trade
                self.logger.info(
                    f"Position exists for {pos.instrument_id} but no active_trade_id - "
                    f"not claiming (may belong to another strategy)"
                )
        else:
            self.logger.info("Reconciliation: no open positions (net is 0)")
            # Clear any stale internal state
            if self.active_trade_id is not None:
                self.logger.warning(
                    "Internal state shows open trade but no portfolio position. "
                    "Clearing stale state."
                )
                self.active_trade_id = None
                self.save_state()

    def _on_position_reconciled(self, position: Position):
        """Handle reconciled position. Override in strategy if needed."""
        self.logger.info(
            f"Reconciled position: {position.quantity} @ {position.avg_px_open}"
        )
        self._last_entry_price = float(position.avg_px_open)
        self._last_entry_qty = float(position.quantity)
        # Note: We do NOT create a new trade record here anymore since we 
        # only reconcile when active_trade_id already exists

    def _has_open_position(self) -> bool:
        """Check if there's an open position (source of truth: portfolio)."""
        return self._get_open_position() is not None

    def _is_position_owned(self) -> bool:
        """
        Check if the strategy currently owns a trade record.
        This is the preferred way to check for position status when multiple 
        strategies share the same instrument.
        """
        return self.active_trade_id is not None

    def _get_open_position(self) -> Optional[Position]:
        """
        Get the open position if it exists.
        Calculates Net Quantity across all venues for the symbol to handle 
        offsetting ghost positions (e.g. LONG on CME-EXTERNAL and SHORT on CME).
        """
        # Symbol to match (e.g., MESH6)
        target_symbol = str(self.instrument_id.symbol)
        all_open_positions = self.cache.positions_open()
        
        symbol_positions = []
        net_qty = 0.0
        
        for pos in all_open_positions:
            # Check if symbol matches (fuzzy match for venue-specific IDs)
            if str(pos.instrument_id.symbol) == target_symbol:
                symbol_positions.append(pos)
                qty = float(pos.quantity)
                if pos.side == PositionSide.LONG:
                    net_qty += qty
                else:
                    net_qty -= qty
        
        if not symbol_positions:
            return None
            
        # If net quantity is effectively zero, we are flat
        if abs(net_qty) < 1e-6: # Conservative floating point safety
            if len(symbol_positions) > 0:
                self.logger.debug(
                    f"Net position for {target_symbol} is negligible ({net_qty:.8f}) "
                    f"across {len(symbol_positions)} positions. Treating as FLAT."
                )
            return None
            
        # We have a non-zero net position. 
        # Prefer the position that exactly matches our instrument_id if possible
        exact_match = next((p for p in symbol_positions if p.instrument_id == self.instrument_id), None)
        if exact_match:
            return exact_match
            
        # Otherwise return the largest matching position as a proxy
        sorted_pos = sorted(symbol_positions, key=lambda x: float(x.quantity), reverse=True)
        pos = sorted_pos[0]
        self.logger.info(
            f"Fuzzy matched position {pos.instrument_id} for strategy instrument {self.instrument_id} "
            f"(Net Symbol Qty: {net_qty:.4f})"
        )
        return pos

    # =========================================================================
    # ORDER MANAGEMENT & DUPLICATE PREVENTION (Using Nautilus Cache)
    # =========================================================================

    def _get_pending_orders(self) -> list:
        """Get all pending orders for this instrument from cache."""
        # In-flight orders (submitted but not yet accepted/rejected)
        inflight = self.cache.orders_inflight(instrument_id=self.instrument_id)
        
        # Open orders (accepted but not filled/canceled)
        open_orders = self.cache.orders_open(instrument_id=self.instrument_id)
        
        return list(inflight) + list(open_orders)

    def can_submit_entry_order(self) -> tuple[bool, str]:
        """
        Check if an entry order can be submitted.
        Returns: (can_submit, reason)
        """
        # Check configuration
        if not self.strategy_config.enabled:
            return False, "Strategy disabled"
        
        # Check Nautilus state - must be RUNNING
        if self.state != ComponentState.RUNNING:
            return False, f"Strategy not running (state: {self.state.name})"
        
        # Check functional readiness
        if not self._functional_ready:
            return False, "Strategy not functionally ready (waiting for instrument/data)"
        
        # Check for ANY pending orders for this instrument (DUPLICATE PREVENTION)
        open_orders = list(self.cache.orders_open(instrument_id=self.instrument_id))
        inflight_orders = list(self.cache.orders_inflight(instrument_id=self.instrument_id))
        all_pending = open_orders + inflight_orders
        
        if all_pending:
            pending_ids = [order.client_order_id for order in all_pending]
            return False, f"Order already pending: {pending_ids}"
        
        # Check for existing positions
        pos = self._get_open_position()
        if pos:
            return False, f"Position already open: {pos.instrument_id} ({pos.quantity} {pos.side.name})"
        
        return True, "Ready"

    def can_submit_exit_order(self) -> tuple[bool, str]:
        """
        Check if an exit order can be submitted.
        Returns: (can_submit, reason)
        """
        # Check Nautilus state
        if self.state == ComponentState.STOPPED:
            return False, "Strategy stopped"
        
        # Check for open position
        pos = self._get_open_position()
        if not pos:
            return False, "No position to close"
        
        # Determine the side that would close this position
        closing_side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY

        # Check for ANY open orders on the closing side for this instrument
        # This covers orders from this strategy, previous instances, and manual orders
        open_orders = list(self.cache.orders_open(instrument_id=self.instrument_id))
        inflight_orders = list(self.cache.orders_inflight(instrument_id=self.instrument_id))
        all_pending = open_orders + inflight_orders
        
        active_closing_orders = [
            order.client_order_id for order in all_pending 
            if order.side == closing_side
        ]
        
        if active_closing_orders:
            return False, f"Exit order ({closing_side.name}) already pending: {active_closing_orders}"
        
        return True, ""

    def submit_bracket_order(
        self, 
        entry_order: Order, 
        stop_loss_price: Optional[float] = None, 
        take_profit_price: Optional[float] = None
    ) -> bool:
        """
        Submit a bracket order (entry + attached SL/TP).
        """
        can_submit, reason = self.can_submit_entry_order()
        if not can_submit:
            self.logger.warning(f"Cannot submit bracket order: {reason}")
            return False

        qty = entry_order.quantity
        tif = entry_order.time_in_force
        
        # LOGIC CHANGE: To bypass IBKR "Combo Strategy" permission rejections (Code 201),
        # we submit the entry order FIRST and INDEPENDENTLY. 
        # The exit legs will be submitted as an OCO pair once the entry is filled.
        
        # Track entry as pending
        self._pending_entry_orders.add(entry_order.client_order_id)
        
        # Save the exit leg intent
        self._pending_bracket_exits[entry_order.client_order_id] = {
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
            "quantity": float(qty.as_double()),
            "instrument_id": str(entry_order.instrument_id),
            "time_in_force": tif
        }

        self.submit_order(entry_order)
        self.logger.info(
            f"📈 Entry order submitted: {entry_order.client_order_id} | Broker-side SL/TP pending fill",
            extra={
                "extra": {
                    "event_type": "entry_order_submitted_bracket",
                    "order_id": str(entry_order.client_order_id),
                    "instrument_id": str(entry_order.instrument_id)
                }
            }
        )
        
        # Immediate save to persist the exit intent
        self.save_state()
        return True

    def _trigger_bracket_exits(self, exit_data: dict, entry_side: OrderSide):
        """Submit the SL and TP legs as an OCO pair after entry fill."""
        sl_price_val = exit_data.get("stop_loss_price")
        tp_price_val = exit_data.get("take_profit_price")
        inst_id_str = exit_data["instrument_id"]
        from nautilus_trader.model.identifiers import InstrumentId
        inst_id = InstrumentId.from_str(inst_id_str)
        qty = self.instrument.make_qty(exit_data["quantity"])
        tif = exit_data["time_in_force"]
        
        exit_side = OrderSide.SELL if entry_side == OrderSide.BUY else OrderSide.BUY
        
        instrument = self.cache.instrument(inst_id)
        if not instrument:
            self.logger.error(f"Cannot trigger bracket exits: {inst_id} not in cache")
            return

        orders = []

        # 1. Stop Loss (STOP_LIMIT)
        if sl_price_val is not None:
            rounded_sl = self.round_to_tick(sl_price_val, instrument)
            sl_trigger = instrument.make_price(rounded_sl)
            limit_offset = 0.05 if abs(rounded_sl) < 3.0 else 0.10
            if exit_side == OrderSide.SELL:
                sl_limit_val = max(0.01, rounded_sl - limit_offset)
            else:
                sl_limit_val = rounded_sl + limit_offset
            sl_limit = instrument.make_price(sl_limit_val)
            
            sl_order = self.order_factory.stop_limit(
                instrument_id=inst_id,
                order_side=exit_side,
                quantity=qty,
                trigger_price=sl_trigger,
                price=sl_limit,
                time_in_force=tif,
                reduce_only=True
            )
            orders.append(sl_order)
            self._bracket_exit_map[sl_order.client_order_id] = "STOP_LOSS"
            self._pending_exit_orders.add(sl_order.client_order_id)

        # 2. Take Profit (LIMIT)
        if tp_price_val is not None:
            rounded_tp = self.round_to_tick(tp_price_val, instrument)
            tp_limit = instrument.make_price(rounded_tp)
            
            tp_order = self.order_factory.limit(
                instrument_id=inst_id,
                order_side=exit_side,
                quantity=qty,
                price=tp_limit,
                time_in_force=tif,
                reduce_only=True
            )
            orders.append(tp_order)
            self._bracket_exit_map[tp_order.client_order_id] = "TAKE_PROFIT"
            self._pending_exit_orders.add(tp_order.client_order_id)

        if orders:
            from nautilus_trader.model.orders import OrderList
            order_list = OrderList(
                order_list_id=self.order_factory.generate_order_list_id(),
                orders=orders
            )
            self.submit_order_list(order_list)
            self.logger.info(
                f"✅ Sequential bracket legs submitted | ListID: {order_list.id}",
                extra={
                    "extra": {
                        "event_type": "bracket_legs_submitted",
                        "order_list_id": str(order_list.id),
                        "instrument_id": str(inst_id)
                    }
                }
            )
            self.save_state()

    def submit_entry_order(self, order: Order) -> bool:
        """
        Submit an entry order with validation.
        Returns: True if submitted, False otherwise
        """
        can_submit, reason = self.can_submit_entry_order()
        
        if not can_submit:
            self.logger.warning(f"Cannot submit entry order: {reason}")
            return False
        
        # Track as pending
        self._pending_entry_orders.add(order.client_order_id)
        
        # Submit to broker
        self.submit_order(order)
        self.logger.info(
            f"Entry order submitted: {order.client_order_id} | {order.side} {order.quantity} @ {order.order_type}",
            extra={
                "extra": {
                    "event_type": "entry_order_submitted",
                    "order_id": str(order.client_order_id),
                    "side": str(order.side),
                    "quantity": str(order.quantity),
                    "order_type": str(order.order_type)
                }
            }
        )
        
        return True

    def submit_exit_order(self, order: Order) -> bool:
        """
        Submit an exit order with validation.
        Returns: True if submitted, False otherwise
        """
        can_submit, reason = self.can_submit_exit_order()
        
        if not can_submit:
            self.logger.warning(f"Cannot submit exit order: {reason}")
            return False
        
        # Track as pending
        self._pending_exit_orders.add(order.client_order_id)
        
        # Submit to broker
        self.submit_order(order)
        self.logger.info(
            f"Exit order submitted: {order.client_order_id} | {order.side} {order.quantity} @ {order.order_type}",
            extra={
                "extra": {
                    "event_type": "exit_order_submitted",
                    "order_id": str(order.client_order_id),
                    "side": str(order.side),
                    "quantity": str(order.quantity),
                    "order_type": str(order.order_type)
                }
            }
        )
        
        return True

    def close_strategy_position(self, reason: str = "STRATEGY_EXIT"):
        """
        Close all positions for this instrument.
        Stores exit reason for trade recording.
        """
        # Check if we can submit an exit order (includes position check and duplicate prevention)
        can_submit, reason_msg = self.can_submit_exit_order()
        if not can_submit:
            # If position exists but order is pending, we don't need to log a warning every time, 
            # but for manual trigger it's useful.
            self.logger.info(f"Skipping position close: {reason_msg}")
            return
        
        pos = self._get_open_position() # Guaranteed to exist due to can_submit_exit_order
        self._last_exit_reason = reason
        
        # Determine the side and account to close this position
        side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY
        p_account = pos.account_id
        
        # QUANTITY SAFETY: Use our tracked inventory if available, otherwise fallback to pos.quantity
        # This prevents closing other strategies' positions on the same instrument
        close_qty = abs(self.signed_inventory)
        if close_qty == 0:
            self.logger.warning("Signed inventory is 0 but position exists. Falling back to broker position quantity.")
            close_qty = pos.quantity
        
        self.logger.info(
            f"Closing position {pos.instrument_id} | Side: {pos.side.name} | Qty: {close_qty} | Reason: {reason}",
            extra={
                "extra": {
                    "event_type": "position_close_initiated",
                    "instrument_id": str(pos.instrument_id),
                    "side": pos.side.name,
                    "quantity": close_qty,
                    "inventory": self.signed_inventory,
                    "reason": reason,
                    "account": p_account
                }
            }
        )

        # We manually submit an offsetting order using our tradeable ID and the position's account
        # This is more robust than close_all_positions for external/recovered positions.
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=self.instrument.make_qty(close_qty),
            # account_id=p_account # Nautilus usually uses the strategy's account_id unless specified
        )
        
        # If the position's account differs from the strategy's account, we MUST override
        if p_account != self.account_id:
            self.logger.warning(f"Position account {p_account} differs from strategy account {self.account_id}. Overriding.")

        # Track as pending
        self._pending_exit_orders.add(order.client_order_id)
        
        self.submit_order(order)
        self.logger.info(
            f"Exit order submitted: {order.client_order_id} | {order.side} {order.quantity} @ {order.order_type}",
            extra={
                "extra": {
                    "event_type": "exit_order_submitted_close",
                    "order_id": str(order.client_order_id),
                    "side": str(order.side),
                    "quantity": str(order.quantity),
                    "order_type": str(order.order_type),
                    "reason": reason
                }
            }
        )
        
        return True

    # =========================================================================
    # ORDER EVENT HANDLERS (Using Nautilus OrderStatus)
    # =========================================================================

    def on_order_submitted(self, event):
        """Order submitted event."""
        try:
            order_id = event.client_order_id
            order = self.cache.order(order_id)
            is_spread_order = order_id in self._pending_spread_orders
            
            if order:
                if is_spread_order:
                    self.logger.info(
                        f"📨 SPREAD ORDER ACCEPTED BY BROKER| ID: {order_id} | Status: {order.status.name} | Waiting for fill...",
                        extra={
                            "extra": {
                                "event_type": "spread_order_accepted",
                                "order_id": str(order_id),
                                "instrument_id": str(order.instrument_id),
                                "status": order.status.name
                            }
                        }
                    )
                else:
                    self.logger.debug(
                        f"Order submitted: {order_id} (status: {order.status.name})"
                    )
            self.on_order_submitted_safe(event)
        except Exception as e:
            self.on_unexpected_error(e)

    def on_order_rejected(self, event):
        """Handle order rejection - clear pending state."""
        try:
            order_id = event.client_order_id
            is_spread_order = order_id in self._pending_spread_orders
            
            # Remove from pending sets
            self._pending_entry_orders.discard(order_id)
            self._pending_exit_orders.discard(order_id)
            self._pending_spread_orders.discard(order_id)
            
            # Get order from cache to check status
            order = self.cache.order(order_id)
            
            if is_spread_order:
                self.logger.error(
                    f"❌ SPREAD ORDER REJECTED BY BROKER| ID: {order_id} | Reason: {event.reason}",
                    extra={
                        "extra": {
                            "event_type": "spread_order_rejected",
                            "order_id": str(order_id),
                            "instrument_id": str(order.instrument_id) if order else "N/A",
                            "reason": str(event.reason),
                            "Action Required": "Check order parameters or market conditions"
                        }
                    }
                )
            elif order:
                self.logger.error(
                    f"Order rejected: {order_id} "
                    f"(status: {order.status.name}, reason: {event.reason})"
                )
            else:
                self.logger.error(f"Order rejected: {event.reason}")
            
            self.on_order_rejected_safe(event)
        except Exception as e:
            self.on_unexpected_error(e)

    def on_order_cancelled(self, event):
        """Handle order cancellation - clear pending state."""
        try:
            order_id = event.client_order_id
            is_spread_order = order_id in self._pending_spread_orders
            
            # Remove from pending sets
            self._pending_entry_orders.discard(order_id)
            self._pending_exit_orders.discard(order_id)
            self._pending_spread_orders.discard(order_id)
            
            if is_spread_order:
                self.logger.warning(
                    f"⚠️ SPREAD ORDER CANCELLED | ID: {order_id} | No fill received.",
                    extra={
                        "extra": {
                            "event_type": "spread_order_cancelled",
                            "order_id": str(order_id)
                        }
                    }
                )
            else:
                self.logger.warning(f"Order cancelled: {order_id}")
            
            self.on_order_canceled_safe(event)
        except Exception as e:
            self.on_unexpected_error(e)

    def on_order_expired(self, event):
        """Handle order expiration - clear pending state."""
        try:
            order_id = event.client_order_id
            is_spread_order = order_id in self._pending_spread_orders
            
            # Remove from pending sets
            self._pending_entry_orders.discard(order_id)
            self._pending_exit_orders.discard(order_id)
            self._pending_spread_orders.discard(order_id)
            
            if is_spread_order:
                self.logger.warning(
                    f"⏰ SPREAD ORDER EXPIRED | ID: {order_id} | Time in Force reached - no fill.",
                    extra={
                        "extra": {
                            "event_type": "spread_order_expired",
                            "order_id": str(order_id)
                        }
                    }
                )
            else:
                self.logger.warning(f"Order expired: {order_id}")
            
            self.on_order_expired_safe(event)
        except Exception as e:
            self.on_unexpected_error(e)

    # =========================================================================
    # UTILITIES
    # =========================================================================

    def round_to_tick(self, price: float, instrument: Instrument) -> float:
        """
        Round a price to the nearest valid tick (minimum price variation).
        Handles special SPX complex order rules and instrument defaults.
        """
        # Default increment
        tick = 0.01 
        
        # 1. Try instrument's own price_increment if available
        if instrument.price_increment > 0:
            tick = float(instrument.price_increment)
        
        # 2. Special overrides for SPX/SPXW (CBOE)
        if "SPX" in str(instrument.id):
            # Complex orders (spreads) are generally 0.05 regardless of price
            is_spread = False
            legs = getattr(instrument, 'legs', None)
            
            # Handle if legs is a method (callable) or property (list)
            if callable(legs):
                legs = legs()
                
            if legs and len(legs) > 0:
                is_spread = True
            
            if is_spread:
                tick = 0.05
            else:
                # Single legs: 0.05 < 3.0, 0.10 >= 3.0
                abs_price = abs(price)
                tick = 0.05 if abs_price < 3.0 else 0.10
        
        # Use Decimal for precise rounding (avoids banker's rounding of float)
        try:
            p_dec = Decimal(str(price))
            t_dec = Decimal(str(tick))
            rounded = (p_dec / t_dec).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * t_dec
            return float(rounded)
        except Exception:
            # Fallback to simple round if Decimal fails
            return round(price / tick) * tick

    def on_order_filled(self, event):
        """Handle order fill - update tracking and record trade."""
        try:
            order_id = event.client_order_id
            
            # Determine if entry or exit based on our tracking
            # CRITICAL FIX: Do NOT discard here yet! Only discard if status is FILLED.
            is_entry = order_id in self._pending_entry_orders
            is_exit = order_id in self._pending_exit_orders
            is_spread_order = order_id in self._pending_spread_orders
            
            # Get order from cache to verify status
            order = self.cache.order(order_id)
            
            if is_spread_order and order:
                fill_value = float(event.last_qty) * float(event.last_px) * 100  # Options multiplier
                self.logger.info(
                    f"✅ SPREAD ORDER FILLED BY BROKER | {order.instrument_id} | {order.side.name} | Qty: {event.last_qty} | Px: {event.last_px} | Val: ${abs(fill_value):.2f}",
                    extra={
                        "extra": {
                            "event_type": "spread_fill",
                            "order_id": str(order_id),
                            "instrument_id": str(order.instrument_id),
                            "side": order.side.name,
                            "filled_qty": float(event.last_qty),
                            "fill_price": float(event.last_px),
                            "fill_value": float(abs(fill_value)),
                            "order_status": order.status.name,
                            "provider": "IB"
                        }
                    }
                )
            elif order:
                self.logger.info(
                    f"Order filled: {order_id} | {order.status.name} | Qty: {event.last_qty} | Px: {event.last_px}",
                    extra={
                        "extra": {
                            "event_type": "order_filled_generic",
                            "order_id": str(order_id),
                            "status": order.status.name,
                            "filled_qty": float(event.last_qty),
                            "fill_price": float(event.last_px)
                        }
                    }
                )
            
            if is_entry:
                self._on_entry_filled(event)
                # NOTE: For entry fills, save_state() is called INSIDE _start_trade_record_async()
                # after active_trade_id is set, to avoid race condition
            elif is_exit:
                # Check for mapped bracket exit reason
                mapped_reason = self._bracket_exit_map.get(order_id)
                if mapped_reason:
                    self.logger.info(
                        f"Bracket exit fill detected | Reason: {mapped_reason}",
                        extra={
                            "extra": {
                                "event_type": "bracket_exit_fill_detected",
                                "reason": mapped_reason,
                                "order_id": str(order_id)
                            }
                        }
                    )
                    self._last_exit_reason = mapped_reason
                    # Clean up map
                    self._bracket_exit_map.pop(order_id, None)
                
                self._on_exit_filled(event)
                # For exit fills, save state immediately (no async dependency)
                self.save_state()
            
            # Call strategy-specific handler
            self.on_order_filled_safe(event)
            
            # Save state for non-entry/exit fills (e.g., spread fills, other order types)
            if not is_entry and not is_exit:
                self.save_state()

            # CRITICAL: Clean up tracking sets ONLY if order is fully filled
            # If PARTIALLY_FILLED, we need to keep tracking it for subsequent fills
            if order and order.status == OrderStatus.PARTIALLY_FILLED:
                remaining_qty = float(order.quantity) - float(order.filled_qty)
                self.logger.warning(
                    f"⚡ PARTIAL FILL | {order_id} | Filled: {event.last_qty} | Total Filled: {order.filled_qty} | Remaining: {remaining_qty:.1f}",
                    extra={
                        "extra": {
                            "event_type": "partial_fill",
                            "order_id": str(order_id),
                            "instrument_id": str(order.instrument_id),
                            "this_fill_qty": float(event.last_qty),
                            "total_filled_qty": float(order.filled_qty),
                            "remaining_qty": remaining_qty,
                            "order_quantity": float(order.quantity),
                            "fill_price": float(event.last_px),
                            "is_spread": is_spread_order
                        }
                    }
                )
            
            if order and order.status == OrderStatus.FILLED:
                self._pending_entry_orders.discard(order_id)
                self._pending_exit_orders.discard(order_id)
                self._pending_spread_orders.discard(order_id)
                
                self.logger.info(
                    f"🏁 Order fully filled | Tracking cleaned up | ID: {order_id}",
                    extra={
                        "extra": {
                            "event_type": "tracking_cleanup",
                            "order_id": str(order_id),
                            "status": "FILLED"
                        }
                    }
                )
            
        except Exception as e:
            self.on_unexpected_error(e)

    def _on_entry_filled(self, event):
        """Handle entry fill - start trade record."""
        self.logger.info(
            f"Entry filled | Qty: {event.last_qty} | Px: {event.last_px}",
            extra={
                "extra": {
                    "event_type": "entry_filled_processed",
                    "quantity": float(event.last_qty),
                    "price": float(event.last_px),
                    "order_id": str(event.client_order_id)
                }
            }
        )
        
        # Update tracking
        self._last_entry_price = float(event.last_px)
        self._last_entry_qty = float(event.last_qty)
        
        # Update inventory
        qty = float(event.last_qty)
        if event.order_side == OrderSide.BUY:
            self.signed_inventory += qty
        else:
            self.signed_inventory -= qty
            
        self.logger.info(f"Inventory updated: {self.signed_inventory}")
        self.save_state()
        # Start trade record asynchronously
        self._schedule_async_task(
            self._start_trade_record_async(event)
        )
        
        # BRACKET TRIGGER: Check if we have stored exits for this entry
        if event.client_order_id in self._pending_bracket_exits:
            exit_data = self._pending_bracket_exits.pop(event.client_order_id)
            self._trigger_bracket_exits(exit_data, event.order_side)

    def _on_exit_filled(self, event):
        """Handle exit fill - close trade record."""
        self.logger.info(
            f"Exit filled | Qty: {event.last_qty} | Px: {event.last_px}",
            extra={
                "extra": {
                    "event_type": "exit_filled_processed",
                    "quantity": float(event.last_qty),
                    "price": float(event.last_px),
                    "order_id": str(event.client_order_id)
                }
            }
        )
        
        # Update inventory
        qty = float(event.last_qty)
        if event.order_side == OrderSide.BUY:
            self.signed_inventory += qty # Buying back (exit short) adds to inventory
        else:
            self.signed_inventory -= qty # Selling (exit long) subtracts
            
        self.logger.info(f"Inventory updated: {self.signed_inventory}")
        
        # Close trade record asynchronously
        self._schedule_async_task(
            self._close_trade_record_async(event)
        )



    async def _start_trade_record_async(self, event):
        """Async handler for starting trade record."""
        if not self._integration_manager:
            self.logger.warning("No integration manager available to start trade record")
            return
        
        try:
            recorder = getattr(self._integration_manager, 'trade_recorder', None)
            if recorder:
                direction = "LONG" if event.order_side == OrderSide.BUY else "SHORT"
                commission = float(event.commission) if hasattr(event, 'commission') else 0.0
                
                self.logger.info(
                    f"Initiating trade record for {self.strategy_id} | {direction}",
                    extra={
                        "extra": {
                            "event_type": "trade_record_initiation",
                            "strategy_id": self.strategy_id,
                            "direction": direction
                        }
                    }
                )
                self.active_trade_id = await recorder.start_trade(
                    strategy_id=self.strategy_id,
                    instrument_id=str(self.instrument_id),
                    entry_time=self.clock.utc_now().isoformat(),
                    entry_price=float(event.last_px),
                    quantity=float(event.last_qty),
                    direction=direction,
                    commission=commission,
                    raw_data=str(event),
                    trade_type="DAYTRADE"
                )
                self.save_state()
                self.logger.info(
                    f"Trade record started | ID: {self.active_trade_id}",
                    extra={
                        "extra": {
                            "event_type": "trade_record_started",
                            "trade_id": str(self.active_trade_id)
                        }
                    }
                )
            else:
                self.logger.warning("No trade recorder found on integration manager")
                # Still save state even without trade recorder
                self.save_state()
        except Exception as e:
            self.logger.error(f"Failed to start trade record: {e}", exc_info=True)
            # CRITICAL: Save state even on error to preserve inventory and other state
            self.save_state()

    async def _close_trade_record_async(self, event):
        """Async handler for closing trade record."""
        if not self._integration_manager:
            self.logger.warning("No integration manager available to close trade record")
            return
            
        if self.active_trade_id is None:
            self.logger.warning("Cannot close trade record: active_trade_id is None")
            return
        
        try:
            recorder = getattr(self._integration_manager, 'trade_recorder', None)
            if recorder:
                # Calculate PnL
                exit_price = float(event.last_px)
                entry_price = self._last_entry_price or 0.0
                quantity = float(event.last_qty)
                multiplier = float(self.instrument.multiplier) if self.instrument else 1.0
                
                # Simple PnL: (Exit - Entry) * Qty * Multiplier
                # For shorts: (Entry - Exit) * Qty * Multiplier
                if event.order_side == OrderSide.SELL:
                    pnl = (exit_price - entry_price) * quantity * multiplier
                else:
                    pnl = (entry_price - exit_price) * quantity * multiplier
                
                commission = float(event.commission) if hasattr(event, 'commission') else 0.0
                exit_reason = getattr(self, '_last_exit_reason', 'UNKNOWN')
                
                self.logger.info(
                    f"Closing trade record | ID: {self.active_trade_id} | Strategy: {self.strategy_id}",
                    extra={
                        "extra": {
                            "event_type": "trade_record_closing",
                            "trade_id": str(self.active_trade_id),
                            "strategy_id": self.strategy_id
                        }
                    }
                )
                await recorder.close_trade(
                    trade_id=self.active_trade_id,
                    exit_time=self.clock.utc_now().isoformat(),
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    pnl=pnl,
                    commission=commission,
                    raw_data=str(event)
                )
                
                self.logger.info(
                    f"Trade record closed | ID: {self.active_trade_id} | PnL: {pnl:.2f}",
                    extra={
                        "extra": {
                            "event_type": "trade_record_closed",
                            "trade_id": str(self.active_trade_id),
                            "pnl": pnl
                        }
                    }
                )
                self.active_trade_id = None
                self.save_state()
            else:
                self.logger.warning("No trade recorder found on integration manager")
        except Exception as e:
            self.logger.error(f"Failed to close trade record: {e}", exc_info=True)

    async def _start_trade_record_from_position_async(self, position: Position):
        """Async handler for starting trade record from a reconciled position."""
        if not self._integration_manager:
            return
        
        try:
            recorder = getattr(self._integration_manager, 'trade_recorder', None)
            if recorder:
                direction = "LONG" if position.side == PositionSide.LONG else "SHORT"
                
                self.active_trade_id = await recorder.start_trade(
                    strategy_id=self.strategy_id,
                    instrument_id=str(self.instrument_id),
                    entry_time=self.clock.utc_now().isoformat(),
                    entry_price=float(position.avg_px_open),
                    quantity=float(position.quantity),
                    direction=direction,
                    commission=0.0,
                    raw_data=f"RECONCILED: {position}",
                    trade_type="DAYTRADE"
                )
                self.save_state()
                self.logger.info(f"Trade record started for reconciled position: {self.active_trade_id}")
        except Exception as e:
            self.logger.error(f"Failed to start trade record from position: {e}")

    def _schedule_async_task(self, coro):
        """Schedule async task in the strategy's event loop."""
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            loop.create_task(coro)
        except Exception as e:
            self.logger.error(f"Failed to schedule async task: {e}")

    def on_bar(self, bar):
        """Handle bar event."""
        try:
            self.on_bar_safe(bar)
        except Exception as e:
            self.on_unexpected_error(e)

    def on_quote_tick(self, tick):
        """
        Handle quote tick event.
        Important for spread trading - spreads typically only provide Bid/Ask quotes,
        not trade ticks (TradeTicks).
        """
        try:
            self.on_quote_tick_safe(tick)
        except Exception as e:
            self.on_unexpected_error(e)

    # =========================================================================
    # STATE PERSISTENCE
    # =========================================================================

    def save_state(self):
        """Persist current state."""
        if self.persistence:
            state = self.get_state()
            state['active_trade_id'] = self.active_trade_id
            state['signed_inventory'] = self.signed_inventory
            state['_last_entry_price'] = self._last_entry_price
            state['_last_entry_qty'] = self._last_entry_qty
            state['_pending_entry_orders'] = [str(oid) for oid in self._pending_entry_orders]
            state['_pending_exit_orders'] = [str(oid) for oid in self._pending_exit_orders]
            state['_bracket_exit_map'] = {str(k): v for k, v in self._bracket_exit_map.items()}
            state['_pending_bracket_exits'] = {str(k): v for k, v in self._pending_bracket_exits.items()}
            self.persistence.save_state(self.strategy_id, state)

    def load_state(self):
        """Load state from persistence."""
        if self.persistence:
            state = self.persistence.load_state(self.strategy_id)
            if state:
                self.logger.info(f"Restoring state for {self.strategy_id}")
                self.active_trade_id = state.get('active_trade_id')
                self.signed_inventory = state.get('signed_inventory', 0.0)
                self._last_entry_price = state.get('_last_entry_price')
                self._last_entry_qty = state.get('_last_entry_qty')
                
                # Restore pending orders (will be validated against cache)
                self._pending_entry_orders = {
                    ClientOrderId(oid) for oid in state.get('_pending_entry_orders', [])
                }
                self._pending_exit_orders = {
                    ClientOrderId(oid) for oid in state.get('_pending_exit_orders', [])
                }
                
                self._bracket_exit_map = {
                    ClientOrderId(k): v for k, v in state.get('_bracket_exit_map', {}).items()
                }
                
                self._pending_bracket_exits = {
                    ClientOrderId(k): v for k, v in state.get('_pending_bracket_exits', {}).items()
                }
                
                self.set_state(state)

    # =========================================================================
    # ABSTRACT METHODS (Strategy-specific)
    # =========================================================================

    @abstractmethod
    def get_state(self) -> Dict[str, Any]:
        """Return strategy-specific state to persist."""
        return {}

    @abstractmethod
    def set_state(self, state: Dict[str, Any]):
        """Restore strategy-specific state."""
        pass

    # =========================================================================
    # SAFE HOOKS (Override in strategy)
    # =========================================================================

    def on_start_safe(self): 
        """Called after instrument is ready and data subscribed."""
        pass
    
    def on_stop_safe(self): pass
    def on_reset_safe(self): pass
    def on_resume_safe(self): pass
    def on_order_submitted_safe(self, event): pass
    def on_order_canceled_safe(self, event): pass
    def on_order_rejected_safe(self, event): pass
    def on_order_expired_safe(self, event): pass
    def on_order_filled_safe(self, event): pass
    def on_bar_safe(self, bar): pass
    def on_quote_tick_safe(self, tick): 
        """Called when a quote tick is received (important for spread trading)."""
        pass

    def on_unexpected_error(self, error: Exception):
        """Called when an unhandled exception occurs."""
        self.logger.exception(f"Unhandled strategy error in {self.strategy_id}: {error}")
        self.logger.error(traceback.format_exc())