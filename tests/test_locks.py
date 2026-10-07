"""M8 锁与幂等测试:InMemory 恒跑;Redis 用例在服务可达时才执行。"""

from __future__ import annotations

import os

import pytest

from app.storage.locks import InMemoryLock, build_lock

# 复盘 R-5:Redis 测试地址可经环境变量适配(本机 ta-redis 映射宿主 6380,
# 写死 6379 时永远 skip);默认仍是 6379,CI/他机零配置不变
REDIS_URL = os.environ.get("PATCHPILOT_TEST_REDIS_URL", "redis://localhost:6379/0")


def _redis_ping_ok() -> bool:
    """端口有人听不等于 Redis 可用:以真实 ping 为准(半就绪/其他服务一律跳过)。"""
    try:
        import redis

        client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        return bool(client.ping())
    except Exception:
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


@pytest.mark.skipif(not _redis_ping_ok(), reason=f"{REDIS_URL} 无可用 Redis 服务")
def test_redis_lock_roundtrip() -> None:
    lock = build_lock(REDIS_URL)
    assert not isinstance(lock, InMemoryLock)
    assert lock.acquire("k-redis", ttl_seconds=30)
    assert not lock.acquire("k-redis", ttl_seconds=30)
    lock.release("k-redis")
    assert lock.acquire("k-redis", ttl_seconds=30)
    lock.release("k-redis")


def test_inmemory_force_release_ignores_holder() -> None:
    """N-20 整改:启动恢复需要无视持有者直接删锁(release 无 token 语义)。"""
    lock = InMemoryLock()
    assert lock.acquire("k-stale", ttl_seconds=600)
    lock.force_release("k-stale")
    assert lock.acquire("k-stale", ttl_seconds=600)


@pytest.mark.skipif(not _redis_ping_ok(), reason=f"{REDIS_URL} 无可用 Redis 服务")
def test_redis_force_release() -> None:
    lock = build_lock(REDIS_URL)
    assert lock.acquire("k-force", ttl_seconds=30)
    lock.force_release("k-force")
    assert lock.acquire("k-force", ttl_seconds=30)
    lock.release("k-force")
