标签拼接函数 `join_labels`(src/labels.py)在不传 sep 参数时抛出 TypeError(NoneType 不能与 str 相加)。按契约,sep 缺省应为逗号。请修复,保证原有测试通过。
