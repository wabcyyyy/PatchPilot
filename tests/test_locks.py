"""M8 锁与幂等测试:InMemory 恒跑;Redis 用例在服务可达时才执行。"""

from __future__ import annotations

import pytest

from app.storage.locks import InMemoryLock, build_lock


def _redis_ping_ok() -> bool:
    """端口有人听不等于 Redis 可用:以真实 ping 为准(半就绪/其他服务一律跳过)。"""
    try:
        import redis

        client = redis.Redis.from_url(
            "redis://localhost:6379/0", socket_connect_timeout=1, socket_timeout=1
        )
        return bool(client.ping())
    except Exception:  # noqa: BLE001
        return False


def test_inmemory_lock_semantics() -> None:
    lock = InMemoryLock()
    assert lock.acquire("k", ttl_seconds=10)
    assert not lock.acquire("k", ttl_seconds=10)  # 重入失败:同一任务不并发
    lock.release("k")
    assert lock.acquire("k", ttl_seconds=10)  # 释放后可再获取


def test_inmemory_lock_ttl_expiry() -> None:
    import time

    lock = InMemoryLock()
    assert lock.acquire("k", ttl_seconds=1)
    # TTL 兜底:进程崩溃后锁自动过期,不会死锁(这里直接拨快时钟验证语义)
    lock._expiry["k"] = time.monotonic() - 0.001
    assert lock.acquire("k", ttl_seconds=10)


def test_build_lock_defaults_to_memory_when_no_url() -> None:
    assert isinstance(build_lock(""), InMemoryLock)


@pytest.mark.skipif(not _redis_ping_ok(), reason="本机 6379 无可用 Redis 服务")
def test_redis_lock_roundtrip() -> None:
    lock = build_lock("redis://localhost:6379/0")
    assert not isinstance(lock, InMemoryLock)
    assert lock.acquire("k-redis", ttl_seconds=30)
    assert not lock.acquire("k-redis", ttl_seconds=30)
    lock.release("k-redis")
    assert lock.acquire("k-redis", ttl_seconds=30)
    lock.release("k-redis")
