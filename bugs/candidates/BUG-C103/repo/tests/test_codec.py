from src.dec import decode_segment
from src.enc import encode_segment


def test_roundtrip_multibyte():
    for text in ("你好", "café", "a/b c"):
        assert decode_segment(encode_segment(text)) == text


def test_plus_is_literal():
    assert decode_segment("a+b") == "a+b"


def test_multibyte_escape_decoded():
    assert decode_segment("%E4%BD%A0%E5%A5%BD") == "你好"


def test_plain_passthrough():
    assert decode_segment("plain-path.txt") == "plain-path.txt"


def test_single_byte_escape():
    assert decode_segment("%2F") == "/"


def test_encode_is_stable():
    assert encode_segment("a b") == "a%20b"
