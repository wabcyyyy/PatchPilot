"""任务锁与幂等:Redis SET NX EX 为主,进程内锁为兜底(无 Redis 时服务照常可用)。

加锁/释放/超时三个时机:
- 加锁:任务创建时(同 repo+commit+issue 幂等键);
- 释放:任务到达终态时主动释放;
- 超时:TTL 兜底,进程崩溃后锁自动过期,不会死锁。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

log = logging.getLogger(__name__)


class BaseLock:
    def acquire(self, key: str, ttl_seconds: int = 600) -> bool: ...

    def release(self, key: str) -> None: ...


class RedisLock(BaseLock):
    """基于 Redis 的分布式锁:SET NX EX 加锁,比对 token 后删除释放。"""

    def __init__(self, redis_url: str) -> None:
        import redis  # 懒加载:未安装 redis 包时退回内存锁

        self._client = redis.Redis.from_url(redis_url, decode_responses=True)
        self._prefix = "patchpilot:lock:"

    def acquire(self, key: str, ttl_seconds: int = 600) -> bool:
        token = uuid.uuid4().hex
        ok = self._client.set(self._prefix + key, token, nx=True, ex=ttl_seconds)
        if ok:
            self._tokens: dict[str, str] = getattr(self, "_tokens", {})
            self._tokens[key] = token
        return bool(ok)

    def release(self, key: str) -> None:
        token = getattr(self, "_tokens", {}).pop(key, None)
        if token is None:
            return
        # 只删除自己持有的锁(token 校验),避免误删他人的锁
        self._client.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
            "else return 0 end",
            1,
            self._prefix + key,
            token,
        )


class InMemoryLock(BaseLock):
    """进程内兜底锁:单进程部署(测试/开发)时与 Redis 语义一致。"""

    def __init__(self) -> None:
        self._expiry: dict[str, float] = {}
        self._mu = threading.Lock()

    def acquire(self, key: str, ttl_seconds: int = 600) -> bool:
        with self._mu:
            now = time.monotonic()
            if self._expiry.get(key, 0) > now:
                return False
            self._expiry[key] = now + ttl_seconds
            return True

    def release(self, key: str) -> None:
        with self._mu:
            self._expiry.pop(key, None)


def build_lock(redis_url: str = "") -> BaseLock:
    """按配置选择锁实现;Redis 不可用时自动退回内存锁并告警。"""
    if not redis_url:
        return InMemoryLock()
    try:
        lock = RedisLock(redis_url)
        lock._client.ping()
        log.info("using Redis task lock (%s)", redis_url)
        return lock
    except Exception as exc:  # noqa: BLE001
        log.warning("redis unavailable (%s); falling back to in-memory lock", exc)
        return InMemoryLock()
