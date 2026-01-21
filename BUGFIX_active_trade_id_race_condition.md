# Race Condition Fix: active_trade_id Not Saved

## Проблема

Стратегія `mes-orb-01` не зберігала `active_trade_id` в state файл через **race condition** між синхронним та асинхронним кодом.

### Що відбувалося:

1. **Order Fill Event** → викликається `on_order_filled()`
2. **Синхронно**: 
   - Оновлюється `signed_inventory`
   - Викликається `_on_entry_filled(event)`
   - Запускається **async task** `_start_trade_record_async()`
   - Викликається `save_state()` на рядку 1307 ❌
3. **Асинхронно** (через ~17ms):
   - `active_trade_id = await recorder.start_trade(...)` ✅
   - Викликається `save_state()` на рядку 1388 ✅

**Проблема**: Перший `save_state()` (рядок 1307) виконувався **ДО** того, як `active_trade_id` був встановлений в async task, тому зберігався `active_trade_id = null`.

### Наслідки:

- Позиція існувала в брокера (MESH6.CME, 1 контракт LONG @ 6865.37)
- Але стратегія не "володіла" позицією (`active_trade_id = null`)
- Логіка виходу не спрацьовувала, бо `_is_position_owned()` повертав `False`
- Стоп-лосс та trailing stop не працювали
- Forced exit о 15:45 ET не спрацьовував

## Рішення

### Зміни в `/root/ntd_trader_dashboard/backend/app/strategies/base.py`:

#### 1. Видалено передчасний `save_state()` для entry fills (рядок 1290-1310)

**Було**:
```python
if is_entry:
    self._on_entry_filled(event)
elif is_exit:
    self._on_exit_filled(event)

# Save state AFTER strategy-specific logic
self.save_state()  # ❌ Викликається ДО встановлення active_trade_id
```

**Стало**:
```python
if is_entry:
    self._on_entry_filled(event)
    # NOTE: For entry fills, save_state() is called INSIDE _start_trade_record_async()
    # after active_trade_id is set, to avoid race condition
elif is_exit:
    self._on_exit_filled(event)
    # For exit fills, save state immediately (no async dependency)
    self.save_state()

# Call strategy-specific handler
self.on_order_filled_safe(event)

# Save state for non-entry/exit fills (e.g., spread fills, other order types)
if not is_entry and not is_exit:
    self.save_state()
```

#### 2. Додано fallback `save_state()` в exception handler (рядок 1388-1397)

**Було**:
```python
else:
    self.logger.warning("No trade recorder found on integration manager")
except Exception as e:
    self.logger.error(f"Failed to start trade record: {e}", exc_info=True)
```

**Стало**:
```python
else:
    self.logger.warning("No trade recorder found on integration manager")
    # Still save state even without trade recorder
    self.save_state()
except Exception as e:
    self.logger.error(f"Failed to start trade record: {e}", exc_info=True)
    # CRITICAL: Save state even on error to preserve inventory and other state
    self.save_state()
```

## Результат

Тепер `active_trade_id` буде коректно зберігатися в state файл:

1. Entry fill → async task створює trade record → встановлює `active_trade_id` → викликає `save_state()`
2. Стратегія "володіє" позицією (`_is_position_owned()` повертає `True`)
3. Логіка виходу працює коректно:
   - Стоп-лосс перевіряється на кожному барі
   - Trailing stop оновлюється
   - Forced exit о 15:45 ET спрацьовує

## Тестування

Після перезапуску системи:
1. Стратегія повинна коректно відновити `active_trade_id` зі state файлу
2. Претендувати на існуючу позицію через `_reconcile_positions()`
3. Керувати позицією (стопи, виходи)

## Файли змінено

- `/root/ntd_trader_dashboard/backend/app/strategies/base.py` (2 зміни)
