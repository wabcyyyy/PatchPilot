# 问题:无时区的时间戳比较报错

仓库中的 `is_due`(src/schedule.py)用于判断日程是否到期。当调用方传入不带时区信息(naive)的 deadline 或 now 时,函数直接抛 TypeError,而不是把 naive 时间按 UTC 解释后正常比较。请修复:naive 值一律视为 UTC,aware 与 naive 混用时比较不再抛异常;同时保持 aware 值之间的既有行为不变。
