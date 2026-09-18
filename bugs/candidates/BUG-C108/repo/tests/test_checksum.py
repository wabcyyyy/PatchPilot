from src.checksum import fast_checksum
from src.checksum_ref import checksum


def test_matches_reference_on_multibyte():
    for text in ("héllo", "你好", "naïve café"):
        assert fast_checksum(text) == checksum(text)


def test_matches_reference_on_ascii():
    for text in ("", "hello", "PatchPilot-2026"):
        assert fast_checksum(text) == checksum(text)


def test_reference_known_value():
    # A=65, B=66
    assert checksum("AB") == 131


def test_fast_known_value():
    assert fast_checksum("AB") == 131


def test_multibyte_changes_result():
    # é 是两个 UTF-8 字节,校验和必须受它影响
    assert fast_checksum("héllo") != fast_checksum("hllo")
