"""模板渲染:把用户模板中的占位符替换为值。"""


def render(template, values):
    """渲染模板:占位符 {name} 替换为 values[name]。"""
    return template.format(**values)
