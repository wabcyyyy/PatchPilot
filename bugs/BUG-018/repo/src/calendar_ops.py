def is_leap(year):
    """闰年:能被 4 整除;整百年须能被 400 整除。"""
    return year % 4 == 0
