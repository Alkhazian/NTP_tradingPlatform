import redis.asyncio as redis
import json
import logging
import os

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

class RedisManager:
    def __init__(self):
        self.redis = None

    async def connect(self):
        self.redis = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
        try:
            await self.redis.ping()
            logger.info("Connected to Redis")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            return False

    async def publish(self, channel: str, message: dict):
        if self.redis:
            await self.redis.publish(channel, json.dumps(message))

    async def subscribe(self, *channels: str):
        if self.redis:
            pubsub = self.redis.pubsub()
            await pubsub.subscribe(*channels)
            return pubsub
        return None

    async def psubscribe(self, *patterns: str):
        if self.redis:
            pubsub = self.redis.pubsub()
            await pubsub.psubscribe(*patterns)
            return pubsub
        return None

    async def close(self):
        if self.redis:
            await self.redis.close()
