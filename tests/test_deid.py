import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from repair_survival import deid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deid_brute

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
FIX_CSV = ROOT / 'fixtures' / 'deid.csv'
FIX_TREE = ROOT / 'fixtures' / 'deid_tree.json'
HEADER = 'asset_id,cohort,installed,failed\n'


def write_csv(root, rows, name='data.csv'):
    p = root / name
    p.write_text(HEADER + '\n'.join(rows) + ('\n' if rows else ''), encoding='utf-8')
    return p


def write_tree(root, spec, name='tree.json'):
    p = root / name
    p.write_text(json.dumps(spec), encoding='utf-8')
    return p


def released_rows(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            'SELECT cohort,installed_bucket,failed FROM devices'
            ' ORDER BY cohort,installed_bucket,failed').fetchall()
    finally:
        con.close()


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class FixtureExampleTest(TempDirCase):
    """交付合成例的实跑核对：数值与 docs/contract.md 一致。"""

    def test_fixture_example_end_to_end(self):
        wh = self.dir / 'wh.db'
        out = self.dir / 'release.db'
        summary = deid.build_warehouse(FIX_CSV, FIX_TREE, '2026-01-31', 2, wh)
        self.assertEqual(summary['included'], 6)
        self.assertEqual(summary['excluded'], ['B3'])
        self.assertEqual(summary['date_level'], 0)
        self.assertEqual(summary['suppressed_devices'], 0)
        self.assertEqual(summary['objective']['distance'], 2)
        self.assertEqual(summary['objective']['mapping'], [
            ['seal-A', 'seal-A'], ['seal-B', 'seal-B'],
            ['seal-X1', 'seal-X'], ['seal-X2', 'seal-X']])
        pub = deid.publish(wh, out)
        self.assertEqual(pub['devices'], 6)
        self.assertEqual(released_rows(out), [
            ('seal-A', '2026-01', 0), ('seal-A', '2026-01', 1),
            ('seal-B', '2026-01', 0), ('seal-B', '2026-01', 1),
            ('seal-X', '2026-01', 0), ('seal-X', '2026-01', 1)])

    def test_warehouse_contents(self):
        wh = self.dir / 'wh.db'
        deid.build_warehouse(FIX_CSV, FIX_TREE, '2026-01-31', 2, wh)
        con = sqlite3.connect(wh)
        try:
            decisions = dict(con.execute(
                'SELECT asset_id,decision FROM input_rows'))
            self.assertEqual(decisions['B3'], 'excluded')
            self.assertEqual(con.execute(
                "SELECT reason FROM input_rows WHERE asset_id='B3'").fetchone()[0],
                'not_installed_by_cutoff')
            self.assertIsNone(con.execute(
                "SELECT event FROM input_rows WHERE asset_id='B3'").fetchone()[0])
            # 原文与来源可复核
            self.assertEqual(con.execute(
                "SELECT text FROM raw_lines WHERE source='csv' AND line_no=1"
            ).fetchone()[0], 'asset_id,cohort,installed,failed')
            self.assertTrue(con.execute(
                "SELECT text FROM raw_lines WHERE source='tree' LIMIT 1"
            ).fetchone()[0].startswith('{'))
            meta = dict(con.execute('SELECT key,value FROM meta'))
            self.assertEqual(meta['k'], '2')
            self.assertIn('不保证匿名', meta['disclaimer'])
            self.assertIn('动态规划', meta['optimality'])
            # 披露决策：组与逐行处置
            groups = con.execute(
                'SELECT gen_cohort,bucket,n,failures,decision FROM'
                ' disclosure_groups ORDER BY gen_cohort').fetchall()
            self.assertEqual(groups, [
                ('seal-A', '2026-01', 2, 1, 'released'),
                ('seal-B', '2026-01', 2, 1, 'released'),
                ('seal-X', '2026-01', 2, 1, 'released')])
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM release_rows WHERE disposition='released'"
            ).fetchone()[0], 6)
            # 最优性依据：每级别每节点都有两分支数值（树共 6 个节点）
            self.assertEqual(con.execute(
                'SELECT COUNT(*) FROM dp_nodes').fetchone()[0], 3 * 6)
            root_row = con.execute(
                "SELECT cut_suppressed,cut_distance,open_suppressed,open_distance,"
                "chosen FROM dp_nodes WHERE level=0 AND node='seal'").fetchone()
            self.assertEqual(root_row, (0, 8, 0, 2, 'open'))
            levels = con.execute(
                'SELECT level,suppressed,distance FROM dp_levels'
                ' ORDER BY level').fetchall()
            self.assertEqual(levels, [(0, 0, 2), (1, 0, 8), (2, 0, 14)])
        finally:
            con.close()


class RandomizedDpVsBruteTest(unittest.TestCase):
    """小树独立穷举核对：DP 最优解必须与枚举全部切面的结果一致。"""

    def random_tree(self, rng):
        counter = iter(range(1000))

        def build(depth):
            name = 'n%d' % next(counter)
            if depth >= 3 or rng.random() < 0.45:
                return {'name': name}
            return {'name': name,
                    'children': [build(depth + 1)
                                 for _ in range(rng.randint(1, 3))]}

        return build(0)

    def random_case(self, rng):
        tree = self.random_tree(rng)
        leaves = []

        def collect(node):
            if not node.get('children'):
                leaves.append(node['name'])
            for c in node.get('children') or []:
                collect(c)

        collect(tree)
        records = []
        for leaf in leaves:
            for _ in range(rng.randint(1, 4)):
                month = rng.choice([1, 1, 1, 2])
                day_n = rng.randint(1, 28 if month == 2 else 31)
                installed = '2026-%02d-%02d' % (month, day_n)
                failed = ''
                if rng.random() < 0.5:
                    fday = day_n + rng.randint(0, 12)
                    if fday <= 31:
                        failed = '2026-%02d-%02d' % (month, fday)
                    elif month == 1 and fday - 31 <= 28:
                        failed = '2026-02-%02d' % (fday - 31)
                records.append([leaf, installed, failed])
        # 截止语义：as_of=2026-01-31；保证至少一台纳入
        included = [r for r in records if r[1] <= '2026-01-31']
        if not included:
            records[0][1] = '2026-01-15'
            if records[0][2] and records[0][2] < records[0][1]:
                records[0][2] = ''
        k = rng.choice([2, 3])
        events = []
        for leaf, installed, failed in records:
            if installed > '2026-01-31':
                continue
            events.append((leaf, installed,
                           1 if failed and failed <= '2026-01-31' else 0))
        return tree, records, events, k

    def test_dp_matches_brute_force(self):
        rng = random.Random(20261113)
        for case_no in range(200):
            tree, _records, events, k = self.random_case(rng)
            with self.subTest(case=case_no):
                root = deid.parse_tree(json.dumps(tree))
                plan = deid.optimize(events, root, k)
                got = (plan['suppressed'], plan['distance'], plan['level'],
                       tuple(sorted(plan['mapping'].items())))
                want, want_released = deid_brute.brute_force(events, tree, k)
                self.assertEqual(got, want)
                groups, assigned = deid._apply_plan(
                    [(i, c, iso, ev) for i, (c, iso, ev) in enumerate(events)],
                    plan['mapping'], plan['level'], k)
                released = Counter((gen, b, ev)
                                   for _i, _c, gen, b, ev, disp in assigned
                                   if disp == 'released')
                self.assertEqual(released, Counter(want_released))
                self.assertEqual(len(plan['trace']),
                                 3 * self._node_count(tree))

    def _node_count(self, tree):
        return 1 + sum(self._node_count(c) for c in tree.get('children') or [])


class RareGroupTest(TempDirCase):
    """稀有组与无故障方差组：整组抑制并给出原因。"""

    def test_below_k_and_no_variance_suppressed(self):
        tree = write_tree(self.dir, {'name': 'g', 'children': [
            {'name': 'g1'}, {'name': 'g2'}]})
        csv = write_csv(self.dir, [
            'a1,g1,2026-01-01,', 'a2,g1,2026-01-02,',
            'b1,g2,2026-02-01,', 'b2,g2,2026-02-02,', 'b3,g2,2026-02-03,',
        ])
        wh = self.dir / 'wh.db'
        summary = deid.build_warehouse(csv, tree, '2026-02-28', 3, wh)
        self.assertEqual(summary['released_devices'], 0)
        self.assertEqual(summary['suppressed_devices'], 5)
        con = sqlite3.connect(wh)
        try:
            groups = dict(((gen, b), (n, f, dec, reason))
                          for gen, b, n, f, dec, reason
                          in con.execute('SELECT gen_cohort,bucket,n,failures,'
                                         'decision,reason FROM disclosure_groups'))
            self.assertEqual(groups[('g', '2026-01')],
                             (2, 0, 'suppressed', 'below_k+no_failure_variance'))
            self.assertEqual(groups[('g', '2026-02')],
                             (3, 0, 'suppressed', 'no_failure_variance'))
        finally:
            con.close()
        out = self.dir / 'release.db'
        deid.publish(wh, out)
        self.assertEqual(released_rows(out), [])

    def test_total_below_k_all_suppressed(self):
        tree = write_tree(self.dir, {'name': 'r', 'children': [
            {'name': 'a'}, {'name': 'b'}]})
        csv = write_csv(self.dir, [
            'a1,a,2026-01-01,2026-01-02', 'a2,a,2026-01-01,',
            'b1,b,2026-01-01,2026-01-03', 'b2,b,2026-01-01,',
        ])
        wh = self.dir / 'wh.db'
        summary = deid.build_warehouse(csv, tree, '2026-01-31', 5, wh)
        self.assertEqual(summary['released_devices'], 0)
        self.assertEqual(summary['objective']['mapping'],
                         [['a', 'a'], ['b', 'b']])
        con = sqlite3.connect(wh)
        try:
            reasons = [r[0] for r in con.execute(
                'SELECT reason FROM disclosure_groups ORDER BY gen_cohort')]
            self.assertEqual(reasons, ['below_k', 'below_k'])
        finally:
            con.close()


class CensoringTest(TempDirCase):
    """沿用原截止语义：当天故障算事件，未来故障/空故障删失，未来安装排除。"""

    def test_cutoff_semantics(self):
        tree = write_tree(self.dir, {'name': 'c'})
        csv = write_csv(self.dir, [
            'c1,c,2026-01-01,2026-01-11',   # 截止当天故障 -> 事件
            'c2,c,2026-01-02,2026-01-15',   # 未来故障 -> 删失
            'c3,c,2026-01-03,',             # 空故障 -> 删失
            'c4,c,2026-02-01,',             # 未来安装 -> 排除
        ])
        wh = self.dir / 'wh.db'
        summary = deid.build_warehouse(csv, tree, '2026-01-11', 2, wh)
        self.assertEqual(summary['included'], 3)
        self.assertEqual(summary['excluded'], ['c4'])
        out = self.dir / 'release.db'
        deid.publish(wh, out)
        rows = released_rows(out)
        self.assertEqual(rows, [('c', '2026-01', 0), ('c', '2026-01', 0),
                                ('c', '2026-01', 1)])
        con = sqlite3.connect(wh)
        try:
            events = dict(con.execute(
                'SELECT asset_id,event FROM input_rows'))
            self.assertEqual(events, {'c1': 1, 'c2': 0, 'c3': 0, 'c4': None})
        finally:
            con.close()


class TiedOptimaTest(TempDirCase):
    """并列最优：先比日期级别，再比排序后的叶->祖先映射。"""

    def build_plan(self, tree_spec, rows, k=2):
        tree = write_tree(self.dir, tree_spec)
        csv = write_csv(self.dir, rows)
        wh = self.dir / 'wh.db'
        summary = deid.build_warehouse(csv, tree, '2026-01-31', k, wh)
        return summary

    def test_level_tie_prefers_month(self):
        # 全部同结果 -> 任何切面/级别都全抑制、距离 0 -> 选级别 0
        summary = self.build_plan(
            {'name': 'r', 'children': [
                {'name': 'p', 'children': [{'name': 'a'}]}, {'name': 'b'}]},
            ['a1,a,2026-01-01,2026-01-02', 'b1,b,2026-01-01,2026-01-03'])
        self.assertEqual(summary['date_level'], 0)
        self.assertEqual(summary['suppressed_devices'], 2)
        self.assertEqual(summary['objective']['mapping'],
                         [['a', 'a'], ['b', 'b']])

    def test_mapping_tie_ancestor_name_wins(self):
        # 并列时比较排序映射：'A' < 'x'，根切面胜出
        summary = self.build_plan(
            {'name': 'A', 'children': [{'name': 'x'}, {'name': 'y'}]},
            ['x1,x,2026-01-01,2026-01-02', 'y1,y,2026-01-01,2026-01-03'])
        self.assertEqual(summary['objective']['mapping'],
                         [['x', 'A'], ['y', 'A']])

    def test_mapping_tie_with_released_rows(self):
        # b 只有未来安装（零纳入）：{a->a,b->A} 与 {a->a,b->b} 距离同为 0
        tree_spec = {'name': 'r', 'children': [
            {'name': 'p', 'children': [{'name': 'a'}]},
            {'name': 'A', 'children': [{'name': 'b'}]}]}
        rows = ['a1,a,2026-01-05,2026-01-06', 'a2,a,2026-01-05,',
                'b1,b,2026-02-01,']
        summary = self.build_plan(tree_spec, rows)
        self.assertEqual(summary['released_devices'], 2)
        self.assertEqual(summary['objective']['distance'], 0)
        self.assertEqual(summary['objective']['mapping'],
                         [['a', 'a'], ['b', 'A']])
        # 与独立穷举一致
        events = [('a', '2026-01-05', 1), ('a', '2026-01-05', 0)]
        want, _ = deid_brute.brute_force(events, tree_spec, 2)
        self.assertEqual((summary['suppressed_devices'],
                          summary['objective']['distance'],
                          summary['date_level'],
                          tuple(map(tuple, summary['objective']['mapping']))),
                         want)


class LeakScanTest(TempDirCase):
    """泄漏扫描：发布物不得含 ID、精确日期、原文、被泛化掉的叶名与逐人审计。"""

    def test_published_db_has_no_leaks(self):
        tree = write_tree(self.dir, {'name': 'PUBCOHORT', 'children': [
            {'name': 'leaf-secret-a'}, {'name': 'leaf-secret-b'}]})
        csv = write_csv(self.dir, [
            'SECRET-ASSET-0001,leaf-secret-a,2026-01-05,2026-01-09',
            'SECRET-ASSET-0002,leaf-secret-a,2026-01-06,',
            'SECRET-ASSET-0003,leaf-secret-b,2026-01-07,2026-01-08',
            'SECRET-ASSET-0004,leaf-secret-b,2026-01-08,',
        ], name='secret-ledger.csv')
        wh = self.dir / 'wh.db'
        deid.build_warehouse(csv, tree, '2026-01-31', 3, wh)
        out = self.dir / 'release.db'
        deid.publish(wh, out)
        blob = out.read_bytes()
        forbidden = ['SECRET-ASSET-0001', 'SECRET-ASSET-0002', 'SECRET-ASSET-0003',
                     'SECRET-ASSET-0004', 'leaf-secret-a', 'leaf-secret-b',
                     'asset_id', 'secret-ledger',
                     'SECRET-ASSET-0001,leaf-secret-a',
                     '2026-01-05', '2026-01-09']
        for token in forbidden:
            self.assertNotIn(token.encode(), blob, token)
        self.assertIn(b'PUBCOHORT', blob)  # 泛化批型名允许出现
        self.assertIsNone(re.search(rb'\d{4}-\d{2}-\d{2}', blob))
        con = sqlite3.connect(out)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            self.assertEqual(tables, ['devices'])
            cols = [r[1] for r in con.execute('PRAGMA table_info(devices)')]
            self.assertEqual(cols, ['cohort', 'installed_bucket', 'failed'])
            buckets = [r[0] for r in con.execute(
                'SELECT DISTINCT installed_bucket FROM devices')]
            for b in buckets:
                self.assertRegex(b, r'^(\d{4}-\d{2}|\d{4}|\*)$')
        finally:
            con.close()


class DeterminismTest(TempDirCase):
    """发布只读一个仓库快照：源移走仍可发布，重复发布字节一致，重复元组不去重。"""

    def test_source_moved_and_byte_deterministic(self):
        tree = write_tree(self.dir, {'name': 'c'})
        csv = write_csv(self.dir, [
            'd1,c,2026-01-05,2026-01-06',
            'd2,c,2026-01-05,',          # 与 d3 完全同元组
            'd3,c,2026-01-05,',
        ])
        wh = self.dir / 'wh.db'
        deid.build_warehouse(csv, tree, '2026-01-31', 2, wh)
        csv.rename(self.dir / 'moved-away.csv')
        tree.rename(self.dir / 'moved-away.json')
        out1 = self.dir / 'r1.db'
        out2 = self.dir / 'r2.db'
        deid.publish(wh, out1)
        deid.publish(wh, out2)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())
        rows = released_rows(out1)
        self.assertEqual(rows, [('c', '2026-01', 0), ('c', '2026-01', 0),
                                ('c', '2026-01', 1)])
        self.assertEqual(len(rows), 3)  # 重复元组不去重


class WriteFailureTest(TempDirCase):
    """建仓、发布各自失败保留旧目标。"""

    def test_failed_build_keeps_old_warehouse(self):
        tree = write_tree(self.dir, {'name': 'c'})
        csv = write_csv(self.dir, ['d1,c,2026-01-05,2026-01-06'])
        wh = self.dir / 'wh.db'
        wh.write_bytes(b'OLD-WAREHOUSE')
        with self.assertRaises(ValueError):
            deid.build_warehouse(csv, tree, '2026-01-31', 1, wh)  # k<2
        self.assertEqual(wh.read_bytes(), b'OLD-WAREHOUSE')
        with self.assertRaises(OSError):
            deid.build_warehouse(self.dir / 'missing.csv', tree,
                                 '2026-01-31', 2, wh)
        self.assertEqual(wh.read_bytes(), b'OLD-WAREHOUSE')
        self.assertEqual(list(self.dir.glob('.deid-wh-*')), [])

    def test_build_to_directory_target_fails(self):
        tree = write_tree(self.dir, {'name': 'c'})
        csv = write_csv(self.dir, ['d1,c,2026-01-05,2026-01-06'])
        target = self.dir / 'whdir'
        target.mkdir()
        with self.assertRaises(OSError):
            deid.build_warehouse(csv, tree, '2026-01-31', 2, target)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(self.dir.glob('.deid-wh-*')), [])

    def test_failed_publish_keeps_old_output(self):
        tree = write_tree(self.dir, {'name': 'c'})
        csv = write_csv(self.dir, ['d1,c,2026-01-05,2026-01-06'])
        wh = self.dir / 'wh.db'
        deid.build_warehouse(csv, tree, '2026-01-31', 2, wh)
        out = self.dir / 'release.db'
        out.write_bytes(b'OLD-RELEASE')
        with self.assertRaises(ValueError):
            deid.publish(self.dir / 'missing.db', out)
        self.assertEqual(out.read_bytes(), b'OLD-RELEASE')
        bad = self.dir / 'bad.db'
        bad.write_bytes(b'not a sqlite database')
        with self.assertRaises(sqlite3.Error):
            deid.publish(bad, out)
        self.assertEqual(out.read_bytes(), b'OLD-RELEASE')
        self.assertEqual(list(self.dir.glob('.deid-rel-*')), [])

    def test_publish_to_directory_target_fails(self):
        tree = write_tree(self.dir, {'name': 'c'})
        csv = write_csv(self.dir, ['d1,c,2026-01-05,2026-01-06'])
        wh = self.dir / 'wh.db'
        deid.build_warehouse(csv, tree, '2026-01-31', 2, wh)
        target = self.dir / 'outdir'
        target.mkdir()
        with self.assertRaises(OSError):
            deid.publish(wh, target)
        self.assertTrue(target.is_dir())

    def test_tampered_warehouse_rejected(self):
        tree = write_tree(self.dir, {'name': 'c'})
        csv = write_csv(self.dir, ['d1,c,2026-01-05,2026-01-06'])
        wh = self.dir / 'wh.db'
        deid.build_warehouse(csv, tree, '2026-01-31', 2, wh)
        con = sqlite3.connect(wh)
        con.execute("UPDATE release_rows SET disposition='released'")
        con.commit()
        con.close()
        with self.assertRaises(ValueError):
            deid.publish(wh, self.dir / 'release.db')


class ValidationTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.tree = write_tree(self.dir, {'name': 'c'})
        self.csv = write_csv(self.dir, ['d1,c,2026-01-05,2026-01-06'])
        self.wh = self.dir / 'wh.db'

    def test_k_validation(self):
        for bad in (0, 1, -3, True, False, '2', 2.5, None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    deid.build_warehouse(self.csv, self.tree, '2026-01-31',
                                         bad, self.wh)

    def test_tree_leaf_mismatch(self):
        tree = write_tree(self.dir, {'name': 'other'}, name='t2.json')
        with self.assertRaises(ValueError):
            deid.build_warehouse(self.csv, tree, '2026-01-31', 2, self.wh)

    def test_tree_structure_errors(self):
        for spec_text in ('{"name":"a","name":"b"}',
                          '{"name":"a","children":{"name":"b"}}',
                          '{"name":""}',
                          '{"name":"a","extra":1}',
                          '[{"name":"a"}]',
                          '{"name":"a","children":[{"name":"a"}]}'):
            p = self.dir / 'bad-tree.json'
            p.write_text(spec_text, encoding='utf-8')
            with self.subTest(spec=spec_text):
                with self.assertRaises(ValueError):
                    deid.build_warehouse(self.csv, p, '2026-01-31', 2, self.wh)

    def test_csv_semantics_reuse(self):
        cases = [
            ['d1,c,2026-01-05,2026-01-06', 'd1,c,2026-01-05,'],   # 重复身份
            ['d1,c,2026-1-5,'],                                    # 非严格日期
            ['d1,c,2026-01-05,2026-01-04'],                        # 故障早于安装
            ['d1,c,2026-01-05,extra,'],                            # 参差记录
            ['d1,,2026-01-05,'],                                   # 空必填
            ['d1,c,2026-02-01,'],                                  # 全部未来安装
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                csv = write_csv(self.dir, rows, name='bad.csv')
                with self.assertRaises(ValueError):
                    deid.build_warehouse(csv, self.tree, '2026-01-31', 2, self.wh)

    def test_bad_as_of(self):
        with self.assertRaises(ValueError):
            deid.build_warehouse(self.csv, self.tree, '2026-1-31', 2, self.wh)

    def test_target_must_differ_from_inputs(self):
        with self.assertRaises(ValueError):
            deid.build_warehouse(self.csv, self.tree, '2026-01-31', 2, self.csv)
        self.assertTrue(self.csv.read_text().startswith(HEADER))
        deid.build_warehouse(self.csv, self.tree, '2026-01-31', 2, self.wh)
        with self.assertRaises(ValueError):
            deid.publish(self.wh, self.wh)
        con = sqlite3.connect(self.wh)
        try:
            self.assertEqual(dict(con.execute('SELECT key,value FROM meta'))['k'],
                             '2')
        finally:
            con.close()

    def test_crlf_csv_accepted(self):
        p = self.dir / 'crlf.csv'
        p.write_bytes(HEADER.replace('\n', '\r\n').encode()
                      + b'd1,c,2026-01-05,2026-01-06\r\n'
                      + b'd2,c,2026-01-05,\r\n')
        summary = deid.build_warehouse(p, self.tree, '2026-01-31', 2, self.wh)
        self.assertEqual(summary['included'], 2)


class CliTest(TempDirCase):
    def run_cli(self, *args):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(SRC) + os.pathsep + env.get('PYTHONPATH', '')
        return subprocess.run(
            [sys.executable, '-m', 'repair_survival.deid', *args],
            capture_output=True, text=True, env=env)

    def test_cli_build_publish_roundtrip(self):
        csv = self.dir / 'data.csv'
        csv.write_bytes(FIX_CSV.read_bytes())
        tree = self.dir / 'tree.json'
        tree.write_bytes(FIX_TREE.read_bytes())
        wh = self.dir / 'wh.db'
        out = self.dir / 'release.db'
        r = self.run_cli('build', '--source', str(csv), '--tree', str(tree),
                         '--as-of', '2026-01-31', '--k', '2',
                         '--warehouse', str(wh))
        self.assertEqual(r.returncode, 0, r.stderr)
        summary = json.loads(r.stdout)
        self.assertEqual(summary['date_level'], 0)
        r = self.run_cli('publish', '--warehouse', str(wh), '--output', str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['devices'], 6)
        self.assertTrue(out.is_file())

    def test_cli_errors_exit_nonzero(self):
        csv = write_csv(self.dir, ['d1,c,2026-01-05,2026-01-06'])
        tree = write_tree(self.dir, {'name': 'c'})
        wh = self.dir / 'wh.db'
        r = self.run_cli('build', '--source', str(csv), '--tree', str(tree),
                         '--as-of', '2026-01-31', '--k', '1',
                         '--warehouse', str(wh))
        self.assertEqual(r.returncode, 2)
        self.assertIn('k must be an integer >= 2', r.stderr)
        self.assertFalse(wh.exists())
        r = self.run_cli('build', '--source', str(csv))
        self.assertEqual(r.returncode, 2)
        r = self.run_cli('publish', '--warehouse', str(self.dir / 'none.db'),
                         '--output', str(self.dir / 'out.db'))
        self.assertEqual(r.returncode, 2)


if __name__ == '__main__':
    unittest.main()
