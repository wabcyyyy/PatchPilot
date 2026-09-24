"""路径段解码,与 enc.encode_segment 互逆。"""


def decode_segment(text):
    """解码 %XX 序列;'+' 按字面处理,多字节 UTF-8 序列合并为单字符。"""
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "+":
            out.append(" ")
            i += 1
        elif ch == "%" and i + 3 <= len(text):
            out.append(chr(int(text[i + 1 : i + 3], 16)))
            i += 3
        else:
            out.append(ch)
            i += 1
    return "".join(out)
