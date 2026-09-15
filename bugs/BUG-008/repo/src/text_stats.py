VOWELS = set("aeiouAEIOU")


def count_vowels(text):
    """统计元音字母个数;None 视为 0。"""
    return sum(1 for ch in text if ch in VOWELS)
