"""路径段编码。"""

SAFE_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-~")


def encode_segment(text):
    """路径段编码:SAFE_CHARS 之外的字符逐字节转 %XX(大写十六进制)。"""
    out = []
    for ch in text:
        if ch in SAFE_CHARS:
            out.append(ch)
        else:
            out.append("".join(f"%{byte:02X}" for byte in ch.encode("utf-8")))
    return "".join(out)
