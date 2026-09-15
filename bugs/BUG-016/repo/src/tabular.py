def index_of(rows, column, value):
    """返回第一个 rows[i][column] == value 的下标;找不到返回 -1;缺列的行跳过。"""
    for i, row in enumerate(rows):
        if row[column] == value:
            return i
    return -1
