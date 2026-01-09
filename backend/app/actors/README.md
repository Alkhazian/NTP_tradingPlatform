# Data Actors

## Концепція

**Data Actors** — це спеціалізовані компоненти, які відповідають за отримання, обробку та трансляцію ринкових даних. Вони **не виконують торгових операцій**, а лише забезпечують потоки даних для використання в торгових стратегіях.

## Архітектурний Підхід

### Технічна Реалізація
Data Actors технічно реалізовані як `Strategy` (наслідують `BaseStrategy`), щоб використовувати вбудовану інфраструктуру NautilusTrader:
- Автоматична підписка на ринкові дані
- Lifecycle management (start/stop)
- Доступ до кешу інструментів
- Отримання колбеків від DataEngine

### Організаційне Розділення
Незважаючи на технічну реалізацію, Data Actors **архітектурно відокремлені** від Trading Strategies:
- Живуть у папці `backend/app/actors/`
- Приховані в UI (не відображаються в списку стратегій)
- Чітко позначені в коментарях як "DataActor"
- Не містять торгової логіки (ордери, позиції, PnL)

## Структура Data Actor

```python
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
import redis.asyncio as redis
import json
import logging
import asyncio

from app.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

class MyDataActorConfig(StrategyConfig):
    strategy_type: str = "MyDataActor"
    instrument_id: str = "SYMBOL.VENUE"
    name: str = "My Data Actor"
    redis_url: str = "redis://redis:6379/0"
    # Додаткові параметри...

class MyDataActor(BaseStrategy):
    """
    DataActor for [опис призначення].
    Provides [які дані] to trading strategies.
    """
    def __init__(self, config: MyDataActorConfig, integration_manager=None):
        super().__init__(config, integration_manager)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.redis_client = None
        # Ініціалізація стану...

    def on_start_safe(self):
        # Підключення до Redis
        try:
            self.redis_client = redis.from_url(
                self.strategy_config.redis_url, 
                decode_responses=True
            )
        except Exception as e:
            logger.error(f"[Actor] Failed to initialize Redis: {e}")
        
        # Підписка на дані
        if self.cache.instrument(self.instrument_id):
            self.subscribe_quote_ticks(self.instrument_id)
        else:
            # Запит інструмента, якщо його немає в кеші
            self._request_instrument()

    def on_quote_tick(self, tick: QuoteTick):
        # Обробка тіка
        price = self._calculate_price(tick)
        
        # Трансляція через Redis
        asyncio.create_task(self._broadcast_data(price))

    def on_stop_safe(self):
        # ВАЖЛИВО: Відписатися від ринкових даних для звільнення IB слота
        try:
            self.unsubscribe_quote_ticks(self.instrument_id)
            asyncio.create_task(self._log_to_ui(f"Unsubscribed from {self.instrument_id}"))
        except Exception as e:
            logger.error(f"[Actor] Failed to unsubscribe: {e}")
        
        # Очищення ресурсів
        if self.redis_client:
            asyncio.create_task(self._log_to_ui("Stopped Actor"))
            asyncio.create_task(self.redis_client.close())

    async def _broadcast_data(self, data):
        """Публікація даних у Redis для споживання стратегіями"""
        if self.redis_client:
            await self.redis_client.publish(
                "my_data_channel",
                json.dumps({
                    "type": "my_data",
                    "data": data,
                    "timestamp": self.clock.timestamp_ns()
                })
            )

    def get_state(self):
        return {}

    def set_state(self, state):
        pass
```

## Використання в Стратегіях

### 1. Підписка на дані Actor'а

```python
class MyTradingStrategy(BaseStrategy):
    def __init__(self, config, integration_manager=None):
        super().__init__(config, integration_manager)
        self.redis_client = None
        self.pubsub = None

    async def on_start_safe(self):
        # Підключення до Redis
        self.redis_client = redis.from_url("redis://redis:6379/0")
        self.pubsub = self.redis_client.pubsub()
        
        # Підписка на канал Data Actor'а
        await self.pubsub.subscribe("my_data_channel")
        
        # Запуск слухача
        asyncio.create_task(self._listen_to_actor())

    async def _listen_to_actor(self):
        async for message in self.pubsub.listen():
            if message["type"] == "message":
                data = json.loads(message["data"])
                await self._process_actor_data(data)

    async def _process_actor_data(self, data):
        # Використання даних від Actor'а для торгової логіки
        if data["type"] == "my_data":
            price = data["data"]
            # Торгова логіка на основі отриманих даних
            self._evaluate_trade_signal(price)
```

### 2. Пряме читання з Redis

```python
class MyTradingStrategy(BaseStrategy):
    async def _get_latest_actor_data(self):
        # Читання останнього значення з Redis
        redis_client = redis.from_url("redis://redis:6379/0")
        data = await redis_client.get("my_data_latest")
        return json.loads(data) if data else None
```

## Реєстрація Data Actor

### В `backend/app/nautilus_manager.py`:

```python
async def start_my_actor(self):
    """Start My Data Actor"""
    if not self.strategy_manager:
        raise RuntimeError("Strategy Manager not initialized")
    
    # Перевірка чи існує
    strategies = self.strategy_manager.get_all_strategies_status()
    actor_exists = any(s['id'] == 'my-actor-01' for s in strategies)
    
    if not actor_exists:
        from app.actors.my_actor import MyDataActor, MyDataActorConfig
        
        config = MyDataActorConfig(
            id="my-actor-01",
            name="My Data Actor",
            instrument_id="SYMBOL.VENUE"
        )
        
        await self.strategy_manager.create_strategy(config)
    
    await self.strategy_manager.start_strategy('my-actor-01')
    return 'my-actor-01'

async def stop_my_actor(self):
    """Stop My Data Actor"""
    if not self.strategy_manager:
        raise RuntimeError("Strategy Manager not initialized")
    
    await self.strategy_manager.stop_strategy('my-actor-01')
    return 'my-actor-01'
```

### API Endpoints в `backend/app/main.py`:

```python
@app.post("/actors/my-actor/start")
async def start_my_actor():
    try:
        id = await nautilus_manager.start_my_actor()
        return {"status": "started", "id": id}
    except Exception as e:
        logger.error(f"Error starting actor: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/actors/my-actor/stop")
async def stop_my_actor():
    try:
        id = await nautilus_manager.stop_my_actor()
        return {"status": "stopped", "id": id}
    except Exception as e:
        logger.error(f"Error stopping actor: {e}")
        raise HTTPException(status_code=500, detail=str(e))
```

## Приховування в UI

У `frontend/src/components/Strategies.tsx`:

```typescript
strategies
    .filter(s => !s.id.includes('actor')) // Приховати всі актори
    .map((strategy) => (
        // Рендер стратегії
    ))
```

## Реєстрація в Strategy Registry

**ВАЖЛИВО**: Навіть якщо це Data Actor, він повинен бути зареєстрований в `backend/app/strategies/registry.json` для динамічного імпорту:

```json
{
    "strategy_type": "MyDataActor",
    "module": "app.actors.my_actor",
    "class_name": "MyDataActor",
    "comment": "Data Actor (uses Strategy infrastructure for plumbing)"
}
```

Це необхідно, бо `StrategyManager` використовує registry для завантаження класів.

## Існуючі Data Actors

### SPX Streamer (`spx_streamer.py`)
- **Призначення**: Трансляція real-time цін S&P 500 Index
- **Канали Redis**: 
  - `spx_stream_price` - оновлення цін
  - `spx_stream_log` - логи актора
- **Використання**: Analytics Dashboard, торгові стратегії на основі SPX

## Best Practices

1. **Один Actor = Один Тип Даних**: Кожен actor відповідає за конкретний тип даних
2. **Stateless**: Актори не повинні зберігати складний стан (тільки кеш для оптимізації)
3. **Robust Error Handling**: Всі Redis операції в try/except
4. **Logging**: Детальне логування для діагностики
5. **Polling Fallback**: Якщо `on_instrument_added` не спрацьовує, використовувати polling
6. **⚠️ ОБОВ'ЯЗКОВО Unsubscribe**: У `on_stop_safe()` завжди викликати `unsubscribe_quote_ticks()` для звільнення IB слотів
7. **Clean Shutdown**: Закривати Redis з'єднання в `on_stop_safe()`
8. **Registry Entry**: Додати actor в `backend/app/strategies/registry.json` з коментарем "Data Actor"

## Переваги Підходу

✅ **Розділення відповідальностей**: Дані окремо від торгової логіки  
✅ **Перевикористання**: Один actor може живити багато стратегій  
✅ **Тестування**: Легко тестувати стратегії з mock даними  
✅ **Масштабування**: Актори можна запускати окремо від стратегій  
✅ **Надійність**: Використання перевіреної інфраструктури Nautilus
