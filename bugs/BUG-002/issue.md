分页函数 `page_slice`(src/pagination.py)每页会丢掉最后一条数据:page_slice(range(10), 1, 3) 应返回 [0,1,2],实际返回 [0,1]。请修复,保证原有测试通过。
