def average(numbers):
    """算术平均;空序列抛 ValueError。"""
    if not numbers:
        raise ValueError("empty sequence")
    return sum(numbers) // len(numbers)
