from src.text_stats import count_vowels


def test_counts_vowels():
    assert count_vowels("PatchPilot") == 3


def test_none_counts_zero():
    assert count_vowels(None) == 0


def test_no_vowels():
    assert count_vowels("rhythm") == 0


def test_empty_string():
    assert count_vowels("") == 0
