from pathlib import Path


def read_head(path, n):
    """返回文件前 n 行(不含换行符);文件不存在返回 []。"""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return lines[:n]
