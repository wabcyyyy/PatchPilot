"""文本清理助手。"""


def clean_text(text):
    """压缩文本:去掉首尾的全部空白字符。"""
    return text.strip(" ")
