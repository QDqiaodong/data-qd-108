"""独立穷举参考实现：枚举全部切面 x 三个日期级别求最优，仅用于小规模测试核对。

与 repair_survival.deid.optimize 的动态规划完全独立：这里直接枚举覆盖全部
叶子的每一个树切面，按规格字典序比较 （抑制人数, 保留行距离和, 日期级别,
排序叶映射）。只适用于叶子很少的树。
"""
from itertools import product


def _walk(node, parent=None, depth=0, acc=None):
    if acc is None:
        acc = {}
    acc[node['name']] = (parent, depth)
    for child in node.get('children') or []:
        _walk(child, node['name'], depth + 1, acc)
    return acc


def all_cuts(node):
    """枚举覆盖全部叶子的所有切面（每片叶子恰有一个入选祖先）。"""
    yield frozenset([node['name']])
    children = node.get('children') or []
    if children:
        for combo in product(*(list(all_cuts(c)) for c in children)):
            yield frozenset().union(*combo)


def _bucket(iso_date, level):
    if level == 0:
        return iso_date[:7]
    if level == 1:
        return iso_date[:4]
    return '*'


def brute_force(records, tree, k):
    """records 为 (cohort, installed_iso, event 0/1)。

    返回 (最优目标元组 （抑制， 距离， 级别， 排序映射）, 最优发布的排序行列表）。
    """
    info = _walk(tree)
    leaves = [name for name, (_p, _d) in info.items()
              if not any(p == name for p, _d in info.values())]

    def path_to_root(name):
        out = []
        cur = name
        while cur is not None:
            out.append(cur)
            cur = info[cur][0]
        return out

    best = None
    best_released = None
    for level in (0, 1, 2):
        for cut in all_cuts(tree):
            mapping = {}
            for leaf in leaves:
                chosen = [n for n in path_to_root(leaf) if n in cut]
                assert len(chosen) == 1, 'not a covering cut'
                mapping[leaf] = chosen[0]
            groups = {}
            for cohort, iso, event in records:
                key = (mapping[cohort], _bucket(iso, level))
                cell = groups.setdefault(key, [0, 0])
                cell[0] += 1
                cell[1] += event
            suppressed = 0
            distance = 0
            released = []
            for (gen, bucket), (n, f) in groups.items():
                if n >= k and 0 < f < n:
                    for cohort, iso, event in records:
                        if (mapping[cohort], _bucket(iso, level)) == (gen, bucket):
                            distance += info[cohort][1] - info[gen][1] + level
                            released.append((gen, bucket, event))
                else:
                    suppressed += n
            cand = (suppressed, distance, level, tuple(sorted(mapping.items())))
            if best is None or cand < best:
                best = cand
                best_released = sorted(released)
    return best, best_released
