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
from typing import Dict, Any, Optional, Set
from datetime import datetime, timedelta
import traceback
import logging

from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Venue
from nautilus_trader.model.enums import OrderSide, PositionSide, OrderStatus
from nautilus_trader.model.orders import Order
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
        
        # Trade persistence
        self.active_trade_id: Optional[int] = None

    # =========================================================================
    # LIFECYCLE MANAGEMENT (Using Nautilus ComponentState)
    # =========================================================================

    def on_start(self):
        """
        Lifecycle hook: Called when strategy is started.
        Nautilus state: INITIALIZED -> STARTING -> RUNNING
        """
        self.logger.info(f"Strategy {self.strategy_id} starting...")
        
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
            self.logger.info(f"Instrument {self.instrument_id} not found in cache. Strategy is in WAITING state.")
            
            # Set timeout for instrument availability
            self.clock.set_time_alert(
                name=f"{self.id}.instrument_timeout",
                alert_time=self.clock.utc_now() + timedelta(seconds=60),
                callback=self._on_instrument_timeout
            )

    def on_instrument(self, instrument: Instrument):
        """Called when instrument data is received."""
        if instrument.id == self.instrument_id:
            self.logger.info(f"Instrument received: {instrument.id}")
            self.instrument = instrument
            self._on_instrument_ready()

    def _on_instrument_ready(self):
        """Called when instrument is confirmed available."""
        if self._instrument_ready:
            return

        self._instrument_ready = True
        self.logger.info(f"Instrument {self.instrument_id} ready")

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
                f"Timeout waiting for instrument {self.instrument_id}. "
                "Strategy cannot start."
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
    # POSITION MANAGEMENT
    # =========================================================================

    def _reconcile_positions(self):
        """Reconcile internal state with actual portfolio positions (using net position logic)."""
        pos = self._get_open_position()
        
        if pos:
            self.logger.info(f"Reconciling: found open position ({pos.instrument_id})")
            self._on_position_reconciled(pos)
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
        
        # If no active trade record, start one for the reconciled position
        if self.active_trade_id is None:
            self.logger.info("Starting trade record for reconciled position")
            self._schedule_async_task(
                self._start_trade_record_from_position_async(position)
            )

    def _has_open_position(self) -> bool:
        """Check if there's an open position (source of truth: portfolio)."""
        return self._get_open_position() is not None

    def _get_open_position(self) -> Optional[Position]:
        """
        Get the open position if it exists.
        Calculates Net Quantity across all venues for the symbol to handle 
        offsetting ghost positions (e.g. LONG on CME-EXTERNAL and SHORT on CME).
        """
        exact_positions = self.cache.positions_open(instrument_id=self.instrument_id)
        
        # Check all positions for this symbol
        symbol = str(self.instrument_id.symbol)
        all_positions = self.cache.positions_open()
        
        symbol_positions = []
        net_qty = 0.0
        
        for pos in all_positions:
            if str(pos.instrument_id.symbol) == symbol:
                symbol_positions.append(pos)
                qty = float(pos.quantity)
                if pos.side == PositionSide.LONG:
                    net_qty += qty
                else:
                    net_qty -= qty
        
        if not symbol_positions:
            return None
            
        # If net quantity is zero, we are effectively flat
        if abs(net_qty) < 1e-9: # Floating point safety
            if len(symbol_positions) > 1:
                self.logger.info(
                    f"Net position for {symbol} is 0.0 across {len(symbol_positions)} offsetting positions. "
                    "Treating as FLAT."
                )
            return None
            
        # If we have an exact match, prefer returning that one
        if exact_positions:
            return exact_positions[0]
            
        # Otherwise return the first fuzzy match (which we now know has non-zero net)
        pos = symbol_positions[0]
        self.logger.info(
            f"Fuzzy matched position {pos.instrument_id} for strategy instrument {self.instrument_id} "
            f"(Net Symbol Qty: {net_qty})"
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
            f"Entry order submitted: {order.client_order_id} "
            f"({order.side} {order.quantity} @ {order.order_type})"
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
            f"Exit order submitted: {order.client_order_id} "
            f"({order.side} {order.quantity} @ {order.order_type})"
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
        
        self.logger.info(
            f"Closing position {pos.instrument_id} (Side: {pos.side.name}) on account {p_account} "
            f"using tradeable instrument {self.instrument_id} (Reason: {reason})"
        )

        # We manually submit an offsetting order using our tradeable ID and the position's account
        # This is more robust than close_all_positions for external/recovered positions.
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=pos.quantity,
            # account_id=p_account # Nautilus usually uses the strategy's account_id unless specified
        )
        
        # If the position's account differs from the strategy's account, we MUST override
        if p_account != self.account_id:
            self.logger.warning(f"Position account {p_account} differs from strategy account {self.account_id}. Overriding.")

        # Track as pending
        self._pending_exit_orders.add(order.client_order_id)
        
        self.submit_order(order)

    # =========================================================================
    # ORDER EVENT HANDLERS (Using Nautilus OrderStatus)
    # =========================================================================

    def on_order_submitted(self, event):
        """Order submitted event."""
        try:
            order = self.cache.order(event.client_order_id)
            if order:
                self.logger.debug(
                    f"Order submitted: {event.client_order_id} "
                    f"(status: {order.status.name})"
                )
            self.on_order_submitted_safe(event)
        except Exception as e:
            self.on_unexpected_error(e)

    def on_order_rejected(self, event):
        """Handle order rejection - clear pending state."""
        try:
            order_id = event.client_order_id
            
            # Remove from pending sets
            self._pending_entry_orders.discard(order_id)
            self._pending_exit_orders.discard(order_id)
            
            # Get order from cache to check status
            order = self.cache.order(order_id)
            if order:
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
            
            # Remove from pending sets
            self._pending_entry_orders.discard(order_id)
            self._pending_exit_orders.discard(order_id)
            
            self.logger.warning(f"Order cancelled: {order_id}")
            self.on_order_canceled_safe(event)
        except Exception as e:
            self.on_unexpected_error(e)

    def on_order_expired(self, event):
        """Handle order expiration - clear pending state."""
        try:
            order_id = event.client_order_id
            
            # Remove from pending sets
            self._pending_entry_orders.discard(order_id)
            self._pending_exit_orders.discard(order_id)
            
            self.logger.warning(f"Order expired: {order_id}")
            self.on_order_expired_safe(event)
        except Exception as e:
            self.on_unexpected_error(e)

    def on_order_filled(self, event):
        """Handle order fill - update tracking and record trade."""
        try:
            order_id = event.client_order_id
            
            # Determine if entry or exit based on our tracking
            is_entry = order_id in self._pending_entry_orders
            is_exit = order_id in self._pending_exit_orders
            
            # Remove from pending
            self._pending_entry_orders.discard(order_id)
            self._pending_exit_orders.discard(order_id)
            
            # Get order from cache to verify status
            order = self.cache.order(order_id)
            if order:
                self.logger.info(
                    f"Order filled: {order_id} "
                    f"(status: {order.status.name}, "
                    f"qty: {event.last_qty}, "
                    f"price: {event.last_px})"
                )
            
            if is_entry:
                self._on_entry_filled(event)
            elif is_exit:
                self._on_exit_filled(event)
            
            # Call strategy-specific handler
            self.on_order_filled_safe(event)
            
            # Save state AFTER strategy-specific logic (ensures timers etc are saved)
            self.save_state()
            
        except Exception as e:
            self.on_unexpected_error(e)

    def _on_entry_filled(self, event):
        """Handle entry fill - start trade record."""
        self.logger.info(f"Entry filled: {event.last_qty} @ {event.last_px}")
        
        # Update tracking
        self._last_entry_price = float(event.last_px)
        self._last_entry_qty = float(event.last_qty)
        
        # Start trade record asynchronously
        self._schedule_async_task(
            self._start_trade_record_async(event)
        )

    def _on_exit_filled(self, event):
        """Handle exit fill - close trade record."""
        self.logger.info(f"Exit filled: {event.last_qty} @ {event.last_px}")
        
        # Close trade record asynchronously
        self._schedule_async_task(
            self._close_trade_record_async(event)
        )

    async def _start_trade_record_async(self, event):
        """Async handler for starting trade record."""
        if not self._integration_manager:
            return
        
        try:
            recorder = getattr(self._integration_manager, 'trade_recorder', None)
            if recorder:
                direction = "LONG" if event.order_side == OrderSide.BUY else "SHORT"
                commission = float(event.commission) if hasattr(event, 'commission') else 0.0
                
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
                self.logger.info(f"Trade record started: {self.active_trade_id}")
        except Exception as e:
            self.logger.error(f"Failed to start trade record: {e}")

    async def _close_trade_record_async(self, event):
        """Async handler for closing trade record."""
        if not self._integration_manager or self.active_trade_id is None:
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
                
                await recorder.close_trade(
                    trade_id=self.active_trade_id,
                    exit_time=self.clock.utc_now().isoformat(),
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    pnl=pnl,
                    commission=commission,
                    raw_data=str(event)
                )
                
                self.logger.info(f"Trade record closed: {self.active_trade_id}, PnL: {pnl}")
                self.active_trade_id = None
                self.save_state()
        except Exception as e:
            self.logger.error(f"Failed to close trade record: {e}")

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

    # =========================================================================
    # STATE PERSISTENCE
    # =========================================================================

    def save_state(self):
        """Persist current state."""
        if self.persistence:
            state = self.get_state()
            state['active_trade_id'] = self.active_trade_id
            state['_last_entry_price'] = self._last_entry_price
            state['_last_entry_qty'] = self._last_entry_qty
            state['_pending_entry_orders'] = [str(oid) for oid in self._pending_entry_orders]
            state['_pending_exit_orders'] = [str(oid) for oid in self._pending_exit_orders]
            self.persistence.save_state(self.strategy_id, state)

    def load_state(self):
        """Load state from persistence."""
        if self.persistence:
            state = self.persistence.load_state(self.strategy_id)
            if state:
                self.logger.info(f"Restoring state for {self.strategy_id}")
                self.active_trade_id = state.get('active_trade_id')
                self._last_entry_price = state.get('_last_entry_price')
                self._last_entry_qty = state.get('_last_entry_qty')
                
                # Restore pending orders (will be validated against cache)
                self._pending_entry_orders = {
                    ClientOrderId(oid) for oid in state.get('_pending_entry_orders', [])
                }
                self._pending_exit_orders = {
                    ClientOrderId(oid) for oid in state.get('_pending_exit_orders', [])
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

    def on_unexpected_error(self, error: Exception):
        """Called when an unhandled exception occurs."""
        self.logger.exception(f"Unhandled strategy error in {self.strategy_id}: {error}")
        self.logger.error(traceback.format_exc())