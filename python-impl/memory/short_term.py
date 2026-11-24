"""
短期记忆 — 基于Redis的会话级记忆
存储最近N轮对话上下文，设置TTL自动过期。
适合维护多轮对话的连续性。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

logger = logging.getLogger(__name__)


class ShortTermMemory:
    """
    短期记忆：基于Redis的会话缓存。

    特点：
    - Redis存储，支持分布式部署
    - TTL自动过期（默认30分钟）
    - 保留最近N轮对话
    - 支持滑动窗口淘汰
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        max_turns: int = 20,
        ttl_seconds: int = 1800,
    ):
        self.max_turns = max_turns
        self.max_messages = max_turns * 2
        self.ttl_seconds = ttl_seconds
        self._redis_url = redis_url
        self._redis: Any = None
        self._fallback_store: dict[str, list] = {}
        self._warned_unavailable = False

    def _warn_unavailable(self, error: Exception | None = None) -> None:
        if self._warned_unavailable:
            return
        if error is None:
            logger.warning("Redis client is unavailable; short-term memory is using its fallback store")
        else:
            logger.warning(
                "Redis is unavailable (%s); short-term memory is using its fallback store. "
                "Check REDIS_URL and Redis authentication.",
                type(error).__name__,
            )
        self._warned_unavailable = True

    async def _close_redis(self) -> None:
        client, self._redis = self._redis, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    async def _get_redis(self):
        """懒加载Redis连接"""
        if self._redis is None:
            if aioredis is None:
                self._warn_unavailable()
                return None
            try:
                # RESP2 works with older Redis servers that do not implement HELLO.
                self._redis = aioredis.from_url(
                    self._redis_url, decode_responses=True, protocol=2
                )
                await self._redis.ping()
                self._warned_unavailable = False
            except Exception as exc:
                await self._close_redis()
                self._warn_unavailable(exc)
        return self._redis

    def _session_key(self, session_id: str) -> str:
        return f"smartcs:short_term:{session_id}"

    async def add_message(self, session_id: str, role: str, content: str) -> None:
        """添加一条对话消息"""
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }

        r = await self._get_redis()

        if r is not None:
            key = self._session_key(session_id)
            try:
                async with r.pipeline(transaction=True) as pipe:
                    pipe.rpush(key, json.dumps(message, ensure_ascii=False))
                    pipe.ltrim(key, -self.max_messages, -1)
                    pipe.expire(key, self.ttl_seconds)
                    await pipe.execute()
                return
            except Exception as exc:
                await self._close_redis()
                self._warn_unavailable(exc)

        self._append_fallback(session_id, message)

    def _append_fallback(self, session_id: str, message: dict[str, Any]) -> None:
        messages = self._fallback_store.setdefault(session_id, [])
        messages.append(message)
        self._fallback_store[session_id] = messages[-self.max_messages:]

    async def replace_history(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Restore the recent SQL history into Redis after a key expires or is missing."""
        recent = [dict(item) for item in messages[-self.max_messages:]]
        self._fallback_store[session_id] = recent
        r = await self._get_redis()
        if r is None:
            return

        key = self._session_key(session_id)
        try:
            async with r.pipeline(transaction=True) as pipe:
                pipe.delete(key)
                if recent:
                    pipe.rpush(key, *(json.dumps(item, ensure_ascii=False) for item in recent))
                    pipe.expire(key, self.ttl_seconds)
                await pipe.execute()
        except Exception as exc:
            await self._close_redis()
            self._warn_unavailable(exc)

    async def get_history(self, session_id: str, last_n: int | None = None) -> list[dict]:
        """获取对话历史"""
        r = await self._get_redis()

        if r is not None:
            key = self._session_key(session_id)
            n = self.max_messages if last_n is None else max(0, last_n)
            try:
                if n == 0:
                    return []
                raw = await r.lrange(key, -n, -1)
                return [json.loads(item) for item in raw]
            except Exception as exc:
                await self._close_redis()
                self._warn_unavailable(exc)

        history = self._fallback_store.get(session_id, [])
        if last_n is not None:
            return history[-max(0, last_n):] if last_n else []
        return list(history)

    async def clear(self, session_id: str) -> None:
        """清除指定session的短期记忆"""
        r = await self._get_redis()
        if r is not None:
            try:
                await r.delete(self._session_key(session_id))
            except Exception as exc:
                await self._close_redis()
                self._warn_unavailable(exc)
        self._fallback_store.pop(session_id, None)

    async def health_check(self) -> bool:
        """Return whether the configured Redis server currently answers PING."""
        r = await self._get_redis()
        if r is None:
            return False
        try:
            await r.ping()
            return True
        except Exception as exc:
            await self._close_redis()
            self._warn_unavailable(exc)
            return False

    async def get_context_window(self, session_id: str, max_tokens: int = 4000) -> str:
        """获取适配上下文窗口大小的对话历史文本"""
        history = await self.get_history(session_id)

        context_parts = []
        estimated_tokens = 0

        for msg in reversed(history):
            msg_text = f"{msg['role']}: {msg['content']}"
            msg_tokens = len(msg_text) // 2  # 粗略估算
            if estimated_tokens + msg_tokens > max_tokens:
                break
            context_parts.insert(0, msg_text)
            estimated_tokens += msg_tokens

        return "\n".join(context_parts)
