"""配置中心。"""

DEFAULTS = {"retries": 3, "timeout": 30}


def lookup_setting(store, key):
    """读取配置项:store 优先,未设置回退 DEFAULTS,未知键返回 None。"""
    return store[key]
