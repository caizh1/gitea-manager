"""完整保留原始运行状态字段，只识别已实测的两次原生配置路径切换。"""


def native_runtime_transition(expected, actual):
    before = {row.get('id'): row for row in expected}
    after = {row.get('id'): row for row in actual}
    if len(before) != len(expected) or len(after) != len(actual) or before.keys() != after.keys():
        return False
    changed = False
    for identifier, left in before.items():
        right = after[identifier]
        if left == right:
            continue
        if identifier != 'runtime-state' or left.keys() != right.keys():
            return False
        if any(left[key] != right[key] for key in left if key != 'revision'):
            return False
        old, new = left.get('revision'), right.get('revision')
        if type(old) is not int or type(new) is not int or old < 0 or new != old + 2:
            return False
        changed = True
    return changed
