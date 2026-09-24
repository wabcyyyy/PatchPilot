路径段解码函数 decode_segment(src/dec.py)与编码函数 encode_segment(src/enc.py)应严格互逆,但包含加号或多字节字符的路径段解码结果不对。请以 enc 的编码规则为准修复 dec,保证原有测试通过。
