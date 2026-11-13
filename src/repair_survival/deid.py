"""离线脱敏建仓与发布（指定披露规则，不保证匿名）。

在既有四列台账（asset_id,cohort,installed,failed）之上新增两个独立入口：

- build：读取 CSV、as-of、整数 k>=2 与单根 cohort 树（叶子精确对应输入批型），
  把原文、来源、纳入/排除与披露决策固化进一个 SQLite 仓库；
- publish：只读一个仓库快照，另建全新 SQLite，仅含泛化批型、安装时间桶与
  故障布尔值，一设备一行、重复元组不去重。

日期、身份与截止语义沿用 repair_survival.subjects；不改变
subjects/estimate/report/cli/competing/uncertainty/workbook 的任何行为。
"""
import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .subjects import day

MODE = 'cohort_disclosure_warehouse'
SCHEMA_VERSION = '1'
LEVELS = (0, 1, 2)  # 0=月(YYYY-MM) 1=年(YYYY) 2=*（安装时间桶统一三选一）

DISCLAIMER = (
    '本仓库与发布物仅实现指定披露规则（每个 泛化批型×安装时间桶 组至少 k 台且'
    '故障/未故障并存，否则整组抑制），不保证匿名；重识别、链接攻击与背景知识'
    '风险未评估，不构成匿名化或隐私保证。'
)
GROUP_RULE = (
    '泛化批型×安装时间桶 组：n>=k 且 0<failures<n 时 released，否则整组 suppressed'
    '（reason=below_k / no_failure_variance / below_k+no_failure_variance）'
)
OPTIMALITY_NOTE = (
    '切面与日期级别由树上动态规划选定：目标（抑制人数, 保留行距离和, 日期级别, '
    '排序叶映射）按字典序且对子树可加，每节点只需比较"在此泛化"与"向下展开"两种'
    '选择，子树最优的组合即全局最优，无需枚举全部切面；dp_nodes 与 dp_levels 表'
    '保存各节点两个分支的（抑制, 距离）数值供独立复核。'
)

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE raw_lines(source TEXT NOT NULL, line_no INTEGER NOT NULL,
                       text TEXT NOT NULL, PRIMARY KEY(source, line_no));
CREATE TABLE tree_nodes(name TEXT PRIMARY KEY, parent TEXT,
                        depth INTEGER NOT NULL, is_leaf INTEGER NOT NULL);
CREATE TABLE input_rows(row_no INTEGER PRIMARY KEY, line_no INTEGER NOT NULL,
                        asset_id TEXT NOT NULL, cohort TEXT NOT NULL,
                        installed TEXT NOT NULL, failed TEXT NOT NULL,
                        decision TEXT NOT NULL, reason TEXT, event INTEGER);
CREATE TABLE dp_nodes(level INTEGER NOT NULL, node TEXT NOT NULL,
                      cut_suppressed INTEGER NOT NULL, cut_distance INTEGER NOT NULL,
                      open_suppressed INTEGER, open_distance INTEGER,
                      chosen TEXT NOT NULL, PRIMARY KEY(level, node));
CREATE TABLE dp_levels(level INTEGER PRIMARY KEY, suppressed INTEGER NOT NULL,
                       distance INTEGER NOT NULL, mapping TEXT NOT NULL);
CREATE TABLE plan(leaf TEXT PRIMARY KEY, ancestor TEXT NOT NULL,
                  edges INTEGER NOT NULL);
CREATE TABLE disclosure_groups(gen_cohort TEXT NOT NULL, bucket TEXT NOT NULL,
                               n INTEGER NOT NULL, failures INTEGER NOT NULL,
                               decision TEXT NOT NULL, reason TEXT,
                               PRIMARY KEY(gen_cohort, bucket));
CREATE TABLE release_rows(row_no INTEGER PRIMARY KEY, gen_cohort TEXT NOT NULL,
                          bucket TEXT NOT NULL, event INTEGER NOT NULL,
                          disposition TEXT NOT NULL);
"""

_RELEASE_SCHEMA = (
    'CREATE TABLE devices(cohort TEXT NOT NULL, installed_bucket TEXT NOT NULL,'
    ' failed INTEGER NOT NULL CHECK(failed IN (0,1)))'
)


class _Node:
    __slots__ = ('name', 'children', 'parent', 'depth')

    def __init__(self, name):
        self.name = name
        self.children = []
        self.parent = None
        self.depth = 0


def validate_k(k):
    if isinstance(k, bool) or not isinstance(k, int) or k < 2:
        raise ValueError('k must be an integer >= 2')
    return k


def _bucket(iso_date, level):
    if level == 0:
        return iso_date[:7]
    if level == 1:
        return iso_date[:4]
    return '*'


def _no_dup_keys(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError('duplicate key in tree object')
        seen.add(key)
    return dict(pairs)


def _build_node(spec, parent, depth, names):
    if not isinstance(spec, dict):
        raise ValueError('tree node must be an object')
    extra = sorted(set(spec) - {'name', 'children'})
    if extra:
        raise ValueError('unexpected tree node keys: %s' % extra)
    name = spec.get('name')
    if not isinstance(name, str) or not name:
        raise ValueError('tree node name must be a non-empty string')
    if name in names:
        raise ValueError('duplicate tree node name: %s' % name)
    names.add(name)
    children = spec.get('children', [])
    if not isinstance(children, list):
        raise ValueError('tree children must be a list')
    node = _Node(name)
    node.parent = parent
    node.depth = depth
    node.children = [_build_node(c, node, depth + 1, names) for c in children]
    return node


def parse_tree(text):
    """解析单根 cohort 树 JSON：{"name":..., "children":[...]}，无 children 为叶子。"""
    try:
        doc = json.loads(text, object_pairs_hook=_no_dup_keys)
        return _build_node(doc, None, 0, set())
    except json.JSONDecodeError as e:
        raise ValueError('invalid tree JSON: %s' % e) from e
    except RecursionError:
        raise ValueError('tree nesting too deep') from None


def _post_order(root):
    order = []
    stack = [(root, False)]
    while stack:
        node, done = stack.pop()
        if done:
            order.append(node)
            continue
        stack.append((node, True))
        stack.extend((c, False) for c in node.children)
    return order


def _pre_order(root):
    order = []
    stack = [root]
    while stack:
        node = stack.pop()
        order.append(node)
        stack.extend(reversed(node.children))
    return order


def optimize(records, root, k):
    """对固定数据与树求全局最优（日期级别, 叶->祖先映射）。

    records 为 (cohort, installed_iso, event 0/1) 序列，cohort 必须都是叶子。
    目标按字典序最小化：（抑制人数, 保留行距离和, 日期级别, 排序叶映射），
    距离=叶到入选祖先的树边数+日期级别(0/1/2)，按保留行计。
    树上动态规划，不枚举全部切面；返回含 dp 痕迹的 plan dict。
    """
    validate_k(k)
    post = _post_order(root)
    sub_leaves = {}
    for u in post:
        if not u.children:
            sub_leaves[u] = [u.name]
        else:
            sub_leaves[u] = sorted(x for c in u.children for x in sub_leaves[c])
    trace = []
    summaries = []
    best_global = None
    for level in LEVELS:
        counts = {u.name: {} for u in post if not u.children}
        for cohort, iso, event in records:
            if cohort not in counts:
                raise ValueError('record cohort is not a tree leaf: %r' % cohort)
            cell = counts[cohort].setdefault(_bucket(iso, level), [0, 0])
            cell[0] += 1
            cell[1] += event
        agg = {}
        for u in post:
            a = {}
            if not u.children:
                for b, (n, f) in counts[u.name].items():
                    a[b] = [n, f, 0]
            else:
                for c in u.children:
                    for b, (n, f, es) in agg[c].items():
                        cell = a.setdefault(b, [0, 0, 0])
                        cell[0] += n
                        cell[1] += f
                        cell[2] += es + n
            agg[u] = a
        best = {}
        for u in post:
            cut_s = 0
            cut_d = 0
            for b, (n, f, es) in agg[u].items():
                if n >= k and 0 < f < n:
                    cut_d += es + n * level
                else:
                    cut_s += n
            cut = (cut_s, cut_d, tuple((name, u.name) for name in sub_leaves[u]))
            if u.children:
                open_s = sum(best[c][0] for c in u.children)
                open_d = sum(best[c][1] for c in u.children)
                open_m = tuple(sorted(m for c in u.children for m in best[c][2]))
                opn = (open_s, open_d, open_m)
                if cut <= opn:
                    best[u] = cut
                    chosen = 'cut'
                else:
                    best[u] = opn
                    chosen = 'open'
                trace.append((level, u.name, cut_s, cut_d, open_s, open_d, chosen))
            else:
                best[u] = cut
                trace.append((level, u.name, cut_s, cut_d, None, None, 'cut'))
        s, d, m = best[root]
        summaries.append({'level': level, 'suppressed': s, 'distance': d,
                          'mapping': [list(p) for p in m]})
        cand = (s, d, level, m)
        if best_global is None or cand < best_global:
            best_global = cand
    s, d, level, m = best_global
    return {'level': level, 'mapping': dict(m), 'suppressed': s, 'distance': d,
            'trace': trace, 'level_summaries': summaries}


def _apply_plan(records, mapping, level, k):
    """records 为 (key, cohort, installed_iso, event)；返回 (组决策, 逐行处置)。"""
    groups = {}
    assign = []
    for key, cohort, iso, event in records:
        gen = mapping[cohort]
        b = _bucket(iso, level)
        cell = groups.setdefault((gen, b), [0, 0])
        cell[0] += 1
        cell[1] += event
        assign.append((key, cohort, gen, b, event))
    decisions = {}
    for gb, (n, f) in groups.items():
        reasons = []
        if n < k:
            reasons.append('below_k')
        if not 0 < f < n:
            reasons.append('no_failure_variance')
        decisions[gb] = {'n': n, 'failures': f,
                         'decision': 'suppressed' if reasons else 'released',
                         'reason': '+'.join(reasons) if reasons else None}
    rows = [(key, cohort, gen, b, ev, decisions[(gen, b)]['decision'])
            for key, cohort, gen, b, ev in assign]
    return decisions, rows


def _parse_csv(text, cutoff):
    """与 subjects.subjects 完全一致的日期、身份与截止语义；逐行给纳入/排除。"""
    reader = csv.DictReader(io.StringIO(text, newline=''))
    if reader.fieldnames != ['asset_id', 'cohort', 'installed', 'failed']:
        raise ValueError('unexpected columns')
    rows = []
    seen = set()
    for r in reader:
        line_no = reader.line_num
        if None in r or any(v is None for v in r.values()):
            raise ValueError('ragged CSV')
        if any(not r[k].strip() for k in ('asset_id', 'cohort', 'installed')):
            raise ValueError('empty required field')
        if r['asset_id'] in seen:
            raise ValueError('duplicate asset')
        seen.add(r['asset_id'])
        start = day(r['installed'])
        end = day(r['failed']) if r['failed'] else None
        if end is not None and end < start:
            raise ValueError('failure precedes installation')
        if start > cutoff:
            rows.append({'line_no': line_no, 'asset_id': r['asset_id'],
                         'cohort': r['cohort'], 'installed': r['installed'],
                         'failed': r['failed'], 'decision': 'excluded',
                         'reason': 'not_installed_by_cutoff', 'event': None})
        else:
            event = 1 if end is not None and end <= cutoff else 0
            rows.append({'line_no': line_no, 'asset_id': r['asset_id'],
                         'cohort': r['cohort'], 'installed': r['installed'],
                         'failed': r['failed'], 'decision': 'included',
                         'reason': None, 'event': event})
    return rows


def _objective(groups, rows, depths, level):
    suppressed = sum(g['n'] for g in groups.values() if g['decision'] == 'suppressed')
    distance = sum(depths[cohort] - depths[gen] + level
                   for _key, cohort, gen, _b, _ev, disp in rows
                   if disp == 'released')
    return suppressed, distance


def build_warehouse(source, tree, as_of, k, warehouse):
    """读取 CSV+树+as-of+k，构建脱敏仓库（原子替换；失败保留旧仓库）。"""
    k = validate_k(k)
    cutoff = day(as_of)
    data = Path(source).read_bytes()
    text = data.decode('utf-8')
    rows = _parse_csv(text, cutoff)
    included = [(i, r) for i, r in enumerate(rows, 1) if r['decision'] == 'included']
    if not included:
        raise ValueError('no observed subjects')
    tdata = Path(tree).read_bytes()
    ttext = tdata.decode('utf-8')
    target = Path(warehouse)
    for src in (source, tree):
        if target.resolve() == Path(src).resolve():
            raise ValueError('warehouse target must differ from inputs')
    root = parse_tree(ttext)
    post = _post_order(root)
    leaves = {u.name for u in post if not u.children}
    cohorts = {r['cohort'] for r in rows}
    if leaves != cohorts:
        raise ValueError(
            'tree leaves must exactly match input cohorts (missing: %s, unexpected: %s)'
            % (sorted(cohorts - leaves), sorted(leaves - cohorts)))
    depths = {u.name: u.depth for u in post}
    records = [(r['cohort'], r['installed'], r['event']) for _i, r in included]
    plan = optimize(records, root, k)
    mapping = plan['mapping']
    level = plan['level']
    groups, assigned = _apply_plan(
        [(i, r['cohort'], r['installed'], r['event']) for i, r in included],
        mapping, level, k)
    suppressed, distance = _objective(groups, assigned, depths, level)
    if suppressed != plan['suppressed'] or distance != plan['distance']:
        raise ValueError('internal plan inconsistency')

    meta = [
        ('schema_version', SCHEMA_VERSION),
        ('mode', MODE),
        ('k', str(k)),
        ('as_of', as_of),
        ('date_level', str(level)),
        ('source_path', str(source)),
        ('source_sha256', hashlib.sha256(data).hexdigest()),
        ('source_bytes', str(len(data))),
        ('tree_path', str(tree)),
        ('tree_sha256', hashlib.sha256(tdata).hexdigest()),
        ('tree_bytes', str(len(tdata))),
        ('included_devices', str(len(included))),
        ('excluded_devices', str(len(rows) - len(included))),
        ('objective_suppressed', str(suppressed)),
        ('objective_distance', str(distance)),
        ('objective_level', str(level)),
        ('objective_mapping', json.dumps(sorted(mapping.items()))),
        ('group_rule', GROUP_RULE),
        ('optimality', OPTIMALITY_NOTE),
        ('disclaimer', DISCLAIMER),
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.deid-wh-', suffix='.tmp', dir=target.parent)
    os.close(fd)
    try:
        con = sqlite3.connect(tmp)
        try:
            con.executescript(_SCHEMA)
            con.executemany('INSERT INTO meta(key,value) VALUES(?,?)', meta)
            con.executemany(
                'INSERT INTO raw_lines(source,line_no,text) VALUES(?,?,?)',
                [('csv', i, line) for i, line in enumerate(text.splitlines(), 1)]
                + [('tree', i, line) for i, line in enumerate(ttext.splitlines(), 1)])
            con.executemany(
                'INSERT INTO tree_nodes(name,parent,depth,is_leaf) VALUES(?,?,?,?)',
                [(u.name, u.parent.name if u.parent else None, u.depth,
                  0 if u.children else 1) for u in _pre_order(root)])
            con.executemany(
                'INSERT INTO input_rows(row_no,line_no,asset_id,cohort,installed,'
                'failed,decision,reason,event) VALUES(?,?,?,?,?,?,?,?,?)',
                [(i, r['line_no'], r['asset_id'], r['cohort'], r['installed'],
                  r['failed'], r['decision'], r['reason'], r['event'])
                 for i, r in enumerate(rows, 1)])
            con.executemany(
                'INSERT INTO dp_nodes(level,node,cut_suppressed,cut_distance,'
                'open_suppressed,open_distance,chosen) VALUES(?,?,?,?,?,?,?)',
                sorted(plan['trace']))
            con.executemany(
                'INSERT INTO dp_levels(level,suppressed,distance,mapping)'
                ' VALUES(?,?,?,?)',
                [(s['level'], s['suppressed'], s['distance'],
                  json.dumps(s['mapping'])) for s in plan['level_summaries']])
            con.executemany(
                'INSERT INTO plan(leaf,ancestor,edges) VALUES(?,?,?)',
                [(leaf, anc, depths[leaf] - depths[anc])
                 for leaf, anc in sorted(mapping.items())])
            con.executemany(
                'INSERT INTO disclosure_groups(gen_cohort,bucket,n,failures,'
                'decision,reason) VALUES(?,?,?,?,?,?)',
                [(gen, b, g['n'], g['failures'], g['decision'], g['reason'])
                 for (gen, b), g in sorted(groups.items())])
            con.executemany(
                'INSERT INTO release_rows(row_no,gen_cohort,bucket,event,disposition)'
                ' VALUES(?,?,?,?,?)',
                [(key, gen, b, ev, disp)
                 for key, _cohort, gen, b, ev, disp in assigned])
            con.commit()
        finally:
            con.close()
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)
    released_n = sum(1 for *_x, disp in assigned if disp == 'released')
    return {
        'warehouse': str(target), 'mode': MODE,
        'schema_version': int(SCHEMA_VERSION), 'k': k, 'as_of': as_of,
        'date_level': level, 'included': len(included),
        'excluded': sorted(r['asset_id'] for r in rows
                           if r['decision'] == 'excluded'),
        'groups_released': sum(1 for g in groups.values()
                               if g['decision'] == 'released'),
        'groups_suppressed': sum(1 for g in groups.values()
                                 if g['decision'] == 'suppressed'),
        'released_devices': released_n,
        'suppressed_devices': suppressed,
        'objective': {'suppressed': suppressed, 'distance': distance,
                       'date_level': level,
                       'mapping': [[leaf, anc] for leaf, anc
                                   in sorted(mapping.items())]},
    }


def _to_int(value, name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError('warehouse meta %s is not an integer' % name) from None


def _read_snapshot(warehouse):
    """在单个只读事务中读取并校验仓库快照；返回全部已核验内容。"""
    p = Path(warehouse)
    if not p.is_file():
        raise ValueError('warehouse not found: %s' % warehouse)
    con = sqlite3.connect(p.resolve().as_uri() + '?mode=ro', uri=True)
    con.isolation_level = None
    try:
        con.execute('BEGIN')
        meta = dict(con.execute('SELECT key,value FROM meta'))
        if meta.get('mode') != MODE:
            raise ValueError('not a deid warehouse (bad mode)')
        if meta.get('schema_version') != SCHEMA_VERSION:
            raise ValueError('unsupported warehouse schema_version')
        k = validate_k(_to_int(meta.get('k'), 'k'))
        level = _to_int(meta.get('date_level'), 'date_level')
        if level not in LEVELS:
            raise ValueError('warehouse meta date_level out of range')
        tree = {}
        for name, parent, depth, is_leaf in con.execute(
                'SELECT name,parent,depth,is_leaf FROM tree_nodes'):
            tree[name] = (parent, depth, is_leaf)
        leaves = {n for n, (_p, _d, is_leaf) in tree.items() if is_leaf}
        plan_rows = con.execute('SELECT leaf,ancestor,edges FROM plan').fetchall()
        if len(plan_rows) != len({leaf for leaf, _a, _e in plan_rows}):
            raise ValueError('warehouse plan has duplicate leaf')
        mapping = {leaf: anc for leaf, anc, _e in plan_rows}
        if set(mapping) != leaves:
            raise ValueError('warehouse plan does not cover exactly the tree leaves')
        for leaf, anc, edges in plan_rows:
            if anc not in tree:
                raise ValueError('warehouse plan ancestor unknown: %s' % anc)
            path = set()
            cur = leaf
            while cur is not None:
                path.add(cur)
                cur = tree[cur][0]
            if anc not in path:
                raise ValueError('planned ancestor not on leaf path: %s->%s'
                                 % (leaf, anc))
            if edges != tree[leaf][1] - tree[anc][1]:
                raise ValueError('warehouse plan edges mismatch for leaf %s' % leaf)
        selected = set(mapping.values())
        for anc in selected:
            cur = tree[anc][0]
            while cur is not None:
                if cur in selected:
                    raise ValueError('warehouse plan is not a cut (nested ancestors)')
                cur = tree[cur][0]
        included = con.execute(
            "SELECT row_no,cohort,installed,event FROM input_rows"
            " WHERE decision='included' ORDER BY row_no").fetchall()
        for _row_no, cohort, _iso, event in included:
            if cohort not in mapping:
                raise ValueError('included row cohort not covered by plan')
            if event not in (0, 1):
                raise ValueError('included row has bad event flag')
        groups, assigned = _apply_plan(included, mapping, level, k)
        stored_groups = {
            (gen, b): (n, f, dec, reason)
            for gen, b, n, f, dec, reason in con.execute(
                'SELECT gen_cohort,bucket,n,failures,decision,reason'
                ' FROM disclosure_groups')}
        expected_groups = {
            gb: (g['n'], g['failures'], g['decision'], g['reason'])
            for gb, g in groups.items()}
        if stored_groups != expected_groups:
            raise ValueError('warehouse disclosure_groups inconsistent with snapshot')
        stored_release = con.execute(
            'SELECT row_no,gen_cohort,bucket,event,disposition FROM release_rows'
            ' ORDER BY row_no').fetchall()
        expected_release = [(key, gen, b, ev, disp)
                            for key, _c, gen, b, ev, disp in assigned]
        if stored_release != expected_release:
            raise ValueError('warehouse release_rows inconsistent with snapshot')
        depths = {n: d for n, (_p, d, _l) in tree.items()}
        suppressed, distance = _objective(groups, assigned, depths, level)
        if (_to_int(meta.get('objective_suppressed'), 'objective_suppressed'),
                _to_int(meta.get('objective_distance'), 'objective_distance'),
                _to_int(meta.get('objective_level'), 'objective_level')) != (
                suppressed, distance, level):
            raise ValueError('warehouse objective meta inconsistent with snapshot')
        released = sorted((gen, b, ev) for _key, _c, gen, b, ev, disp in assigned
                          if disp == 'released')
        con.execute('COMMIT')
    except Exception:
        try:
            con.execute('ROLLBACK')
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()
    return {'k': k, 'level': level, 'groups': groups, 'assigned': assigned,
            'released': released, 'suppressed': suppressed, 'distance': distance}


def publish(warehouse, output):
    """只读一个仓库快照，另建全新 SQLite 发布物（原子替换；失败保留旧目标）。"""
    snap = _read_snapshot(warehouse)
    released = snap['released']
    target = Path(output)
    if target.resolve() == Path(warehouse).resolve():
        raise ValueError('publish output must differ from warehouse')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.deid-rel-', suffix='.tmp', dir=target.parent)
    os.close(fd)
    try:
        con = sqlite3.connect(tmp)
        try:
            con.execute(_RELEASE_SCHEMA)
            con.executemany(
                'INSERT INTO devices(cohort,installed_bucket,failed) VALUES(?,?,?)',
                released)
            con.commit()
        finally:
            con.close()
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return {
        'output': str(target), 'devices': len(released),
        'groups_released': sum(1 for g in snap['groups'].values()
                               if g['decision'] == 'released'),
        'groups_suppressed': sum(1 for g in snap['groups'].values()
                                 if g['decision'] == 'suppressed'),
        'suppressed_devices': snap['suppressed'],
    }


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='repair_survival.deid',
        description='Offline de-identification warehouse build/publish'
        ' (specified disclosure rule, not an anonymity guarantee)')
    sub = p.add_subparsers(dest='command', required=True)
    b = sub.add_parser('build', help='build de-identification warehouse')
    b.add_argument('--source', required=True)
    b.add_argument('--tree', required=True)
    b.add_argument('--as-of', required=True)
    b.add_argument('--k', required=True, type=int)
    b.add_argument('--warehouse', required=True)
    u = sub.add_parser('publish', help='publish generalized SQLite from warehouse')
    u.add_argument('--warehouse', required=True)
    u.add_argument('--output', required=True)
    a = p.parse_args(argv)
    try:
        if a.command == 'build':
            r = build_warehouse(a.source, a.tree, a.as_of, a.k, a.warehouse)
        else:
            r = publish(a.warehouse, a.output)
    except (ValueError, OSError, sqlite3.Error) as e:
        p.exit(2, str(e) + '\n')
    print(json.dumps(r, sort_keys=True))


if __name__ == '__main__':
    main()
