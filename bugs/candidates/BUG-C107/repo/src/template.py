"""极简模板:{{key}} 占位符替换。"""

from src.escapes import escape_html

AUTOESCAPE = True


def render(template, ctx, autoescape=AUTOESCAPE):
    """替换 {{key}};autoescape 开启时替换值做 HTML 转义;None 渲染为空串。"""
    out = template
    for key, value in ctx.items():
        token = "{{" + key + "}}"
        replacement = escape_html(str(value)) if autoescape else str(value)
        out = out.replace(token, replacement)
    return out
