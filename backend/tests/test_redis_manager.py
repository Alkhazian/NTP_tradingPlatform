import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from app.redis_manager import RedisManager

@pytest.mark.asyncio
async def test_redis_connect_success():
    """Test successful Redis connection."""
    with patch("redis.asyncio.from_url") as mock_from_url:
        mock_redis = AsyncMock()
        mock_from_url.return_value = mock_redis
        
        manager = RedisManager()
        result = await manager.connect()
        
        assert result is True
        assert manager.redis is mock_redis
        mock_redis.ping.assert_called_once()

@pytest.mark.asyncio
async def test_redis_connect_failure():
    """Test Redis connection failure handling."""
    with patch("redis.asyncio.from_url") as mock_from_url:
        mock_redis = AsyncMock()
        # Ping raises exception
        mock_redis.ping.side_effect = Exception("Connection refused")
        mock_from_url.return_value = mock_redis
        
        manager = RedisManager()
        result = await manager.connect()
        
        assert result is False

@pytest.mark.asyncio
async def test_publish():
    """Test message publishing."""
    with patch("redis.asyncio.from_url"):
        manager = RedisManager()
        manager.redis = AsyncMock() # Mock manually set
        
        await manager.publish("test_channel", {"key": "value"})
        
        # Verify publish called with JSON string
        manager.redis.publish.assert_called_with("test_channel", '{"key": "value"}')

@pytest.mark.asyncio
async def test_subscribe():
    """Test channel subscription."""
    manager = RedisManager()
    manager.redis = AsyncMock()
    
    # client.pubsub() is synchronous in redis-py, returns PubSub object
    mock_pubsub = AsyncMock()
    manager.redis.pubsub = MagicMock(return_value=mock_pubsub)
    
    pubsub = await manager.subscribe("channel1", "channel2")
    
    # PubSub.subscribe is async
    mock_pubsub.subscribe.assert_called_with("channel1", "channel2")
    assert pubsub is mock_pubsub
