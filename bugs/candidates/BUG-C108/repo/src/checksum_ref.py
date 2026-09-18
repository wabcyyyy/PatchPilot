"""参考实现:大输入较慢,但语义权威。"""


def checksum(data):
    """Σ(UTF-8 字节) mod 65536,逐字节取模的朴素实现。"""
    total = 0
    for byte in data.encode("utf-8"):
        total = (total + byte) % 65536
    return total
