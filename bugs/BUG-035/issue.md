模板渲染 render(src/template.py)有两个问题:替换值为 None 时输出了字面量 None(契约应渲染为空串),且替换值里的 & 字符没有被转义。两处分别位于 template 与 escapes 模块,请对齐契约修复,保证原有测试通过。
