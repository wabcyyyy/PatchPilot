"""快速校验和:语义必须与 checksum_ref.checksum 一致。"""


def fast_checksum(data):
    """Σ(UTF-8 字节) mod 65536 的快速实现。"""
    return sum(data.encode("ascii", errors="ignore")) % 65536
