配置读取函数 `lookup_setting`(src/config.py)在 store 未设置某个已知配置项时直接抛 KeyError。按契约:store 优先,未设置回退 DEFAULTS,未知键返回 None。请修复,保证原有测试通过。
