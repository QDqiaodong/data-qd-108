import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

from repair_survival import uncertainty as unc
from repair_survival.estimate import estimate
from repair_survival.report import analyze as old_analyze
from repair_survival.subjects import subjects

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
ASSETS = ROOT / 'fixtures' / 'assets.csv'
UNC_FIX = ROOT / 'fixtures' / 'uncertainty.csv'
HEADER = 'asset_id,cohort,installed,failed\n'
AS_OF = '2026-01-11'

# 独立标准正态参考值(标准公开表/高精度实现),用于核对标准库二分求逆精度。
Z_REFERENCE = {
    0.80: 1.2815515655446004,
    0.90: 1.6448536269514722,
    0.95: 1.959963984540054,
    0.99: 2.5758293035489004,
}


def frac(node_field):
    return Fraction(node_field['fraction'])


def write_csv(root, rows, name='data.csv'):
    p = root / name
    p.write_text(HEADER + '\n'.join(rows) + ('\n' if rows else ''), encoding='utf-8')
    return p



def log_log_bounds(s, g, z):
    if s == 0:
        return 0.0, 0.0
    if s == 1 and g == 0:
        return 1.0, 1.0
    w = math.log(-math.log(float(s)))
    sigma = math.sqrt(float(g))
    lo = math.exp(-math.exp(w + z * sigma))
    hi = math.exp(-math.exp(w - z * sigma))
    return max(0.0, min(1.0, lo)), max(0.0, min(1.0, hi))


class QuantileTest(unittest.TestCase):
    def test_standard_normal_quantiles_against_reference(self):
        for level, ref in Z_REFERENCE.items():
            with self.subTest(level=level):
                self.assertAlmostEqual(unc.two_sided_z(level), ref, delta=1e-12)
                # erf 回代:覆盖率回到请求置信水平
                z = unc.two_sided_z(level)
                coverage = math.erf(z / math.sqrt(2))
                self.assertAlmostEqual(coverage, level, delta=1e-12)

    def test_quantile_monotone_and_symmetry(self):
        qs = [unc.two_sided_z(x / 100) for x in range(80, 100)]
        self.assertEqual(qs, sorted(qs))
        self.assertAlmostEqual(unc.normal_quantile(0.5), 0.0, delta=1e-12)
        self.assertAlmostEqual(unc.normal_quantile(0.1), -unc.normal_quantile(0.9), delta=1e-12)

    def test_confidence_level_validation(self):
        for good in (0.80, 0.99, 0.95, 0.875):
            self.assertEqual(unc.validate_confidence_level(good), float(good))
        for bad in (0.799, 0.991, 0.0, 1.0, -0.5, math.nan, math.inf, -math.inf,
                    True, False, '0.95', None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    unc.validate_confidence_level(bad)


class UncertaintyTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.out = self.root / 'uncertainty.json'

    def build(self, rows, as_of=AS_OF, horizon=10, level=0.95, name='data.csv'):
        p = write_csv(self.root, rows, name)
        return unc.build_report(p, as_of, horizon, level)

    def run_analyze(self, rows, horizon=10, level=0.95, as_of=AS_OF, name='data.csv'):
        p = write_csv(self.root, rows, name)
        return unc.analyze(p, as_of, horizon, self.out, level)

    # ---- 既有估计保持与确定性 ------------------------------------------------
    def test_km_point_estimates_and_rmst_unchanged(self):
        rows, _ = subjects(ASSETS, AS_OF)
        old = estimate(rows, 10)
        report = unc.build_report(ASSETS, AS_OF, 10)
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['mode'], 'kaplan_meier_uncertainty')
        for name, cohort in old.items():
            self.assertEqual(report['cohorts'][name]['km'], cohort)
        self.assertEqual(report['excluded_future_installations'], ['F1'])
        # seal-A 既有数值
        a = report['cohorts']['seal-A']
        self.assertEqual(a['km']['median_days'], 5)
        self.assertEqual(a['km']['rmst_days'], 6.75)

    def test_repeated_runs_byte_identical(self):
        unc.analyze(UNC_FIX, AS_OF, 10, self.out)
        first = self.out.read_bytes()
        unc.analyze(UNC_FIX, AS_OF, 10, self.out)
        self.assertEqual(first, self.out.read_bytes())
        # 同输入不同临时文件名也不影响字节
        payload = json.loads(first)
        self.assertEqual(first, (json.dumps(payload, indent=2, sort_keys=True,
                                            allow_nan=False) + '\n').encode())

    def test_sha256_from_single_byte_read(self):
        report = unc.build_report(UNC_FIX, AS_OF, 10)
        self.assertEqual(report['parameters']['source_sha256'],
                         hashlib.sha256(UNC_FIX.read_bytes()).hexdigest())
        # 每份源文件只读取一次原始字节:解析与 SHA256 必须来自同一份字节
        raw = UNC_FIX.read_bytes()
        with mock.patch.object(unc.Path, 'read_bytes', return_value=raw) as read_mock:
            unc.build_report(UNC_FIX, AS_OF, 10)
        read_mock.assert_called_once_with()

    def test_old_cli_output_schema_unchanged(self):
        old_out = self.root / 'old.json'
        before_py = old_analyze(ASSETS, AS_OF, 10, old_out)
        self.assertNotIn('schema_version', before_py)
        self.assertNotIn('confidence_level', before_py)
        # 新报告不写入旧文件
        unc.analyze(UNC_FIX, AS_OF, 10, self.out)
        old_bytes = old_out.read_bytes()
        old_analyze(ASSETS, AS_OF, 10, old_out)
        self.assertEqual(old_bytes, old_out.read_bytes())

    # ---- Greenwood 与 log-log 区间 -------------------------------------------
    def test_simple_non_censored_greenwood_terms_and_intervals(self):
        # 4 台依次在年龄 2/4/7/10 失效:Y=4,d=1 -> Y=3,d=1 -> Y=2,d=1 -> Y=1,d=1
        fail_days = {1: '2026-01-03', 2: '2026-01-05',
                     3: '2026-01-08', 4: '2026-01-11'}
        rows = ['X%d,g,2026-01-01,%s' % (i, fail_days[i]) for i in range(1, 5)]
        report = self.build(rows, horizon=10)
        g = report['cohorts']['g']
        table = {n['day']: n for n in g['risk_table']}
        expected_terms = {2: Fraction(1, 12), 4: Fraction(1, 6),
                          7: Fraction(1, 2), 10: None}
        cumulative = Fraction(0)
        z = unc.two_sided_z(0.95)
        survival = Fraction(1)
        for t in (2, 4, 7, 10):
            n = table[t]
            self.assertEqual(n['at_risk_count'], expected_terms and {2: 4, 4: 3, 7: 2, 10: 1}[t])
            self.assertEqual(n['failure_count'], 1)
            self.assertEqual(n['censored_count'], 0)
            if expected_terms[t] is not None:
                cumulative += expected_terms[t]
                self.assertEqual(frac(n['greenwood_term']), expected_terms[t])
                self.assertEqual(frac(n['greenwood_cumulative']), cumulative)
            else:
                self.assertIsNone(n['greenwood_term'])
                self.assertIsNone(n['greenwood_cumulative'])
            survival *= {2: Fraction(3, 4), 4: Fraction(2, 3),
                         7: Fraction(1, 2), 10: Fraction(0, 1)}[t]
            self.assertEqual(frac(n['survival']), survival)
            if 0 < survival < 1:
                lo, hi = log_log_bounds(survival, cumulative, z)
                self.assertAlmostEqual(n['interval']['lower'], lo, places=14)
                self.assertAlmostEqual(n['interval']['upper'], hi, places=14)
                self.assertEqual(n['interval']['method'], 'log_log')
                self.assertLess(n['interval']['lower'], n['interval']['upper'])
            else:
                self.assertEqual(n['interval'], {'lower': 0.0, 'upper': 0.0,
                                                  'method': 'degenerate_zero'})

    def test_interval_bounds_clip_to_unit_and_ordered(self):
        rows = ['X1,g,2026-01-01,2026-01-03', 'X2,g,2026-01-01,2026-01-03',
                'X3,g,2026-01-01,2026-01-08']
        report = self.build(rows, horizon=10)
        for n in report['cohorts']['g']['risk_table']:
            self.assertGreaterEqual(n['interval']['lower'], 0.0)
            self.assertLessEqual(n['interval']['upper'], 1.0)
            self.assertLessEqual(n['interval']['lower'], n['interval']['upper'])

    def test_tied_failures_count_d_and_censor_shares_Y(self):
        # 年龄3: Y=4, d=2(并列) + c=1(同龄删失,较晚安装);年龄7: Y=1 删失
        rows = [
            'Y1,g,2026-01-01,2026-01-04',
            'Y2,g,2026-01-01,2026-01-04',
            'Y3,g,2026-01-08,',
            'Y4,g,2026-01-04,',
        ]
        report = self.build(rows, horizon=10)
        table = {n['day']: n for n in report['cohorts']['g']['risk_table']}
        n3 = table[3]
        self.assertEqual(n3['at_risk_count'], 4)
        self.assertEqual(n3['failure_count'], 2)
        self.assertEqual(n3['censored_count'], 1)
        self.assertEqual(n3['failures'], ['Y1', 'Y2'])
        self.assertEqual(n3['censored'], ['Y3'])
        self.assertEqual(n3['at_risk'], ['Y1', 'Y2', 'Y3', 'Y4'])
        # Greenwood 分子只含 d=2,删失不进入:2/(4*2)=1/4
        self.assertEqual(frac(n3['greenwood_term']), Fraction(1, 4))
        self.assertEqual(frac(n3['survival']), Fraction(1, 2))
        # 删失节点不改变 S 与累计 G
        n7 = table[7]
        self.assertEqual(n7['failure_count'], 0)
        self.assertIsNone(n7['greenwood_term'])
        self.assertEqual(frac(n7['survival']), Fraction(1, 2))
        self.assertEqual(frac(n7['greenwood_cumulative']), Fraction(1, 4))
        self.assertEqual(n7['at_risk_count'], 1)

    def test_risk_set_identity_matches_independent_enumeration(self):
        rows = [
            'A1,g,2026-01-01,2026-01-03',
            'A2,g,2026-01-01,2026-01-06',
            'A3,g,2026-01-06,',
            'A4,g,2026-01-01,',
        ]
        durations = {'A1': (2, True), 'A2': (5, True), 'A3': (5, False),
                     'A4': (10, False)}
        report = self.build(rows, horizon=10)
        for node in report['cohorts']['g']['risk_table']:
            t = node['day']
            want = sorted(a for a, (d, _e) in durations.items() if d >= t)
            self.assertEqual(node['at_risk'], want)
            self.assertEqual(node['at_risk_count'], len(want))

    def test_no_events_zero_variance_and_unit_intervals(self):
        rows = ['Z1,g,2026-01-01,', 'Z2,g,2026-01-01,']
        report = self.build(rows, horizon=10)
        g = report['cohorts']['g']
        for n in g['risk_table']:
            self.assertEqual(frac(n['survival']), Fraction(1))
            self.assertEqual(frac(n['greenwood_cumulative']), Fraction(0))
            self.assertEqual(n['interval']['lower'], 1.0)
            self.assertEqual(n['interval']['upper'], 1.0)
        h = g['horizon']
        self.assertEqual(h['status'], 'ok')
        self.assertEqual(frac(h['variance']), Fraction(0))
        self.assertEqual(h['standard_error'], 0.0)
        self.assertEqual(h['interval'], {'lower': 10.0, 'upper': 10.0})
        self.assertEqual(frac(h['rmst_days']), Fraction(10))

    def test_complete_failure_zero_survival_interval(self):
        rows = ['T1,g,2026-01-01,2026-01-04', 'T2,g,2026-01-01,2026-01-04']
        report = self.build(rows, horizon=10)
        g = report['cohorts']['g']
        last = g['risk_table'][-1]
        self.assertEqual(frac(last['survival']), Fraction(0))
        self.assertEqual(last['interval'], {'lower': 0.0, 'upper': 0.0,
                                            'method': 'degenerate_zero'})

    def test_censor_at_horizon_and_event_at_horizon_boundaries(self):
        # 最后删失恰在 horizon:区间仍可估计;事件恰在 horizon 不增 RMST 方差
        rows_censor = ['S1,g,2026-01-01,']
        r1 = self.build(rows_censor, horizon=10)
        g1 = r1['cohorts']['g']
        self.assertEqual(g1['status'], 'estimable')
        self.assertEqual(g1['horizon']['interval'], {'lower': 10.0, 'upper': 10.0})
        rows_event = ['E1,g,2026-01-01,2026-01-11', 'E2,g,2026-01-01,2026-01-06']
        r2 = self.build(rows_event, horizon=10)
        g2 = r2['cohorts']['g']
        # 年龄5事件(Y=2,d=1)是唯一 t<10 的方差项;年龄10事件 A_j=0 不贡献
        table = {n['day']: n for n in g2['risk_table']}
        self.assertEqual(frac(table[10]['survival']), Fraction(0))
        h = g2['horizon']
        # 手算: S: [0,5)=1, [5,10)=1/2 -> RMST=7.5; A_5=2.5, 项=2.5^2/(2*1)=3.125
        self.assertEqual(frac(h['rmst_days']), Fraction(15, 2))
        self.assertEqual(frac(h['variance']), Fraction(25, 8))
        self.assertAlmostEqual(h['standard_error'], math.sqrt(25 / 8))

    # ---- RMST 方差独立手算与奇异 ---------------------------------------------
    def test_rmst_variance_independent_hand_calc(self):
        rows = [
            'X1,g,2026-01-01,2026-01-03',
            'X2,g,2026-01-01,2026-01-05',
            'X3,g,2026-01-01,2026-01-08',
            'X4,g,2026-01-01,2026-01-11',
        ]
        report = self.build(rows, horizon=10)
        h = report['cohorts']['g']['horizon']
        # S: 1 (0-2), 3/4 (2-4), 1/2 (4-7), 1/4 (7-10)
        # RMST = 2 + 1.5 + 1.5 + 0.75 = 5.75
        self.assertEqual(frac(h['rmst_days']), Fraction(23, 4))
        # A_2=3.75=15/4, A_4=2.25=9/4, A_7=0.75=3/4
        terms = [(Fraction(15, 4), 4, 1), (Fraction(9, 4), 3, 1),
                 (Fraction(3, 4), 2, 1)]
        var = Fraction(0)
        for a, y, d in terms:
            var += a ** 2 * Fraction(d, y * (y - d))
        self.assertEqual(frac(h['variance']), var)
        self.assertEqual(frac(h['variance']), Fraction(147, 64))
        self.assertAlmostEqual(h['standard_error'], math.sqrt(147 / 64))
        z = unc.two_sided_z(0.95)
        self.assertAlmostEqual(h['interval']['lower'],
                               max(0.0, 5.75 - z * math.sqrt(147 / 64)))
        self.assertAlmostEqual(h['interval']['upper'],
                               min(10.0, 5.75 + z * math.sqrt(147 / 64)))

    def test_rmst_interval_clipped_to_horizon(self):
        # 无故障:RMST=horizon,区间被截断到 [0,horizon]
        rows = ['Z1,g,2026-01-01,']
        report = self.build(rows, horizon=3)
        h = report['cohorts']['g']['horizon']
        self.assertEqual(h['interval']['lower'], 3.0)
        self.assertEqual(h['interval']['upper'], 3.0)

    def test_greenwood_singular_keeps_rmst_point_but_null_interval(self):
        # Y=d 完全失效(零日事件按原约定):RMST 点值 0 保留,方差/SE/区间为 null
        rows = ['T1,g,2026-01-11,2026-01-11', 'T2,g,2026-01-01,2026-01-01']
        report = self.build(rows, horizon=5, as_of='2026-01-11')
        g = report['cohorts']['g']
        node0 = g['risk_table'][0]
        self.assertEqual(node0['day'], 0)
        self.assertEqual(node0['at_risk_count'], 2)
        self.assertEqual(node0['failure_count'], 2)
        self.assertIsNone(node0['greenwood_cumulative'])
        self.assertEqual(node0['interval']['method'], 'degenerate_zero')
        h = g['horizon']
        self.assertEqual(h['status'], 'greenwood_singular')
        self.assertEqual(h['reason'], 'greenwood_singular')
        self.assertEqual(frac(h['rmst_days']), Fraction(0))
        self.assertIsNone(h['variance'])
        self.assertIsNone(h['standard_error'])
        self.assertIsNone(h['interval'])

    def test_greenwood_singular_after_positive_area(self):
        # 年龄3先失效一台(Y=4,d=1),年龄6剩余三台并列失效(Y=3,d=3),horizon=10
        rows = [
            'P1,g,2026-01-01,2026-01-04',
            'P2,g,2026-01-01,2026-01-07',
            'P3,g,2026-01-01,2026-01-07',
            'P4,g,2026-01-01,2026-01-07',
        ]
        report = self.build(rows, horizon=10)
        g = report['cohorts']['g']
        h = g['horizon']
        self.assertEqual(h['status'], 'greenwood_singular')
        # RMST 点值仍输出:S: [0,3)=1, [3,6)=3/4, 之后 0 -> 3+2.25=5.25
        self.assertEqual(frac(h['rmst_days']), Fraction(21, 4))
        self.assertIsNone(h['variance'])
        self.assertIsNone(h['interval'])
        singular_node = {n['day']: n for n in g['risk_table']}[6]
        self.assertIsNone(singular_node['greenwood_cumulative'])
        self.assertEqual(frac(singular_node['survival']), Fraction(0))

    # ---- 支持缺口 -------------------------------------------------------------
    def test_risk_set_gap_when_horizon_exceeds_followup(self):
        # 单台年龄5删失,horizon=8:末尾 S>0 且无在风险个体 -> risk_set_gap
        rows = ['Q1,g,2026-01-01,']
        report = self.build(rows, horizon=8, as_of='2026-01-06')
        g = report['cohorts']['g']
        self.assertEqual(g['status'], 'risk_set_gap')
        self.assertEqual(g['gap'], {'start_day': 5, 'end_day': 8})
        self.assertEqual(g['support_end_day'], 5)
        h = g['horizon']
        self.assertEqual(h['status'], 'risk_set_gap')
        self.assertIsNone(h['rmst_days'])
        self.assertIsNone(h['variance'])
        self.assertIsNone(h['interval'])
        # 缺口前节点仍给区间
        self.assertEqual(frac(g['risk_table'][-1]['survival']), Fraction(1))

    def test_cohort_statuses_independent(self):
        # 同一报告内独立状态:gap 队列年龄2即删失,horizon=3 出现 [2,3) 空档;
        # ok 队列两人,年龄2一台失效,另一台年龄3才失效,horizon=3 前区间均有支持。
        rows = [
            'Q1,gap,2026-01-02,',
            'X1,ok,2026-01-01,2026-01-03',
            'X2,ok,2026-01-01,2026-01-04',
        ]
        report = self.build(rows, horizon=3, as_of='2026-01-04')
        self.assertEqual(report['cohorts']['gap']['status'], 'risk_set_gap')
        self.assertEqual(report['cohorts']['gap']['gap'], {'start_day': 2, 'end_day': 3})
        self.assertEqual(report['cohorts']['ok']['status'], 'estimable')
        self.assertEqual(report['cohorts']['ok']['horizon']['status'], 'ok')

    def test_no_baseline_support_branch(self):
        # 四列有效数据下该分支不可自然到达,直接调用 cohort 构建器验证分支口径
        members = [{'asset_id': 'R1', 'cohort': 'g', 'duration_days': 3, 'event': False}]
        g = unc._cohort_uncertainty(members, horizon=6, z=unc.two_sided_z(0.95))
        # 年龄0即进入,但年龄3删失后 [3,6) 无支持 -> risk_set_gap(前缀可估计)
        self.assertEqual(g['status'], 'risk_set_gap')
        self.assertEqual(g['gap'], {'start_day': 3, 'end_day': 6})
        # 空成员 -> 无基线支持的防御分支
        g0 = unc._cohort_uncertainty([], horizon=6, z=unc.two_sided_z(0.95))
        self.assertEqual(g0['status'], 'no_baseline_support')
        self.assertEqual(g0['horizon']['status'], 'no_baseline_support')
        self.assertIsNone(g0['horizon']['rmst_days'])

    # ---- 输入契约 -------------------------------------------------------------
    def test_input_contract_rejections(self):
        cases = {
            'bad_header': 'id,cohort,installed,failed\nX1,g,2026-01-01,\n',
            'ragged': HEADER + 'X1,g,2026-01-01\n',
            'empty_field': HEADER + 'X1,,2026-01-01,\n',
            'duplicate': HEADER + 'X1,g,2026-01-01,\nX1,g,2026-01-02,\n',
            'bad_date': HEADER + 'X1,g,2026-1-1,\n',
            'failure_first': HEADER + 'X1,g,2026-01-03,2026-01-02\n',
            'all_future': HEADER + 'X1,g,2026-02-01,\n',
            'empty': '',
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                p = self.root / (name + '.csv')
                p.write_text(text, encoding='utf-8')
                with self.assertRaises(ValueError):
                    unc.build_report(p, AS_OF, 10)

    def test_missing_source_is_oserror(self):
        with self.assertRaises(OSError):
            unc.build_report(self.root / 'missing.csv', AS_OF, 10)

    def test_horizon_validation(self):
        p = write_csv(self.root, ['X1,g,2026-01-01,'])
        for bad in (0, -1, 2.5, True, False, '10'):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    unc.build_report(p, AS_OF, bad)

    def test_invalid_confidence_preserves_old_report(self):
        self.run_analyze(['X1,g,2026-01-01,'], level=0.95)
        before = self.out.read_bytes()
        p = write_csv(self.root, ['X1,g,2026-01-01,'], name='again.csv')
        for bad in (0.799, 0.991, math.nan, math.inf):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    unc.analyze(p, AS_OF, 10, self.out, bad)
                self.assertEqual(self.out.read_bytes(), before)
        # 坏输入同样保留旧报告,修正后可直接重跑
        bad = self.root / 'bad.csv'
        bad.write_text(HEADER + 'X1,g,2026-1-1,\n')
        with self.assertRaises(ValueError):
            unc.analyze(bad, AS_OF, 10, self.out)
        self.assertEqual(self.out.read_bytes(), before)
        good = write_csv(self.root, ['X1,g,2026-01-01,'], name='fixed.csv')
        unc.analyze(good, AS_OF, 10, self.out)
        self.assertTrue(self.out.exists())

    def test_no_nan_or_infinity_serialized(self):
        report = self.run_analyze([
            'X1,g,2026-01-01,2026-01-03',
            'X2,g,2026-01-01,2026-01-06',
            'X3,g,2026-01-01,',
        ])
        text = self.out.read_text()
        self.assertNotIn('NaN', text)
        self.assertNotIn('Infinity', text)
        json.loads(text)  # 严格 JSON 可解析

    def test_no_runtime_varying_fields(self):
        r1 = self.run_analyze(['X1,g,2026-01-01,'])
        r2 = unc.build_report(write_csv(self.root, ['X1,g,2026-01-01,'], name='b.csv'),
                              AS_OF, 10)
        self.assertEqual(set(r1['parameters']),
                         {'as_of', 'horizon_days', 'confidence_level', 'source_sha256'})
        for r in (r1, r2):
            serialized = json.dumps(r)
            self.assertNotIn(str(self.root), serialized)

    # ---- 写入/替换失败 --------------------------------------------------------
    def test_replace_failure_preserves_old_report_and_cleans_temp(self):
        self.run_analyze(['X1,g,2026-01-01,'])
        before = self.out.read_bytes()
        p = write_csv(self.root, ['X1,g,2026-01-01,'], name='again.csv')
        with mock.patch.object(unc.os, 'replace', side_effect=OSError('injected replace failure')):
            with self.assertRaises(OSError):
                unc.analyze(p, AS_OF, 10, self.out)
        self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual([q for q in self.root.iterdir() if q.suffix == '.tmp'], [])

    def test_write_failure_preserves_old_report_and_cleans_temp(self):
        self.run_analyze(['X1,g,2026-01-01,'])
        before = self.out.read_bytes()
        p = write_csv(self.root, ['X1,g,2026-01-01,'], name='again.csv')

        def bad_fdopen(fd, *a, **k):
            os.close(fd)
            raise OSError('injected write failure')

        with mock.patch.object(unc.os, 'fdopen', side_effect=bad_fdopen):
            with self.assertRaises(OSError):
                unc.analyze(p, AS_OF, 10, self.out)
        self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual([q for q in self.root.iterdir() if q.suffix == '.tmp'], [])
        # 修正后可直接重跑
        unc.analyze(p, AS_OF, 10, self.out)
        self.assertTrue(self.out.exists())


class UncertaintyCliTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.out = self.root / 'report.json'

    def run_cli(self, *args):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(SRC) + os.pathsep + env.get('PYTHONPATH', '')
        return subprocess.run([sys.executable, '-m', 'repair_survival.uncertainty', *args],
                              capture_output=True, text=True, env=env, cwd=self.root)

    def test_end_to_end_cli_with_real_csv(self):
        args = ('--source', str(UNC_FIX), '--as-of', AS_OF, '--horizon', '10',
                '--confidence-level', '0.95', '--output', str(self.out))
        r = self.run_cli(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        summary = json.loads(r.stdout)
        self.assertEqual(summary['mode'], 'kaplan_meier_uncertainty')
        self.assertEqual(summary['schema_version'], 1)
        self.assertEqual(summary['cohorts']['seal-X']['rmst'], 'ok')
        report = json.loads(self.out.read_text())
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['parameters']['confidence_level'], 0.95)
        self.assertEqual(report['parameters']['source_sha256'],
                         hashlib.sha256(UNC_FIX.read_bytes()).hexdigest())
        # 点估计
        x = report['cohorts']['seal-X']
        self.assertEqual(x['km']['rmst_days'], 5.75)
        # 逐节点区间
        node2 = {n['day']: n for n in x['risk_table']}[2]
        self.assertEqual(node2['at_risk_count'], 4)
        self.assertEqual(node2['interval']['method'], 'log_log')
        self.assertLess(node2['interval']['lower'], node2['interval']['upper'])
        # RMST 区间
        h = x['horizon']
        self.assertEqual(h['interval']['lower'] < h['rmst_days']['value'] < h['interval']['upper'],
                         True)
        # 确定性:重跑字节一致
        first = self.out.read_bytes()
        r2 = self.run_cli(*args)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(self.out.read_bytes(), first)
        # 解释标注
        self.assertFalse(report['interpretation']['simultaneous_confidence_band'])
        self.assertFalse(report['interpretation']['causal_interpretation_provided'])

    def test_cli_confidence_boundaries_and_rejections(self):
        base = ('--source', str(UNC_FIX), '--as-of', AS_OF, '--horizon', '10')
        for level in ('0.80', '0.99'):
            r = self.run_cli(*base, '--confidence-level', level, '--output', str(self.out))
            self.assertEqual(r.returncode, 0, r.stderr)
        for level in ('0.799', '0.991', 'nan', 'inf'):
            r = self.run_cli(*base, '--confidence-level', level, '--output', str(self.out))
            self.assertNotEqual(r.returncode, 0, level)

    def test_cli_gap_is_success_diagnostic(self):
        src = self.root / 'gap.csv'
        src.write_text(HEADER + 'Q1,g,2026-01-01,\n')
        r = self.run_cli('--source', str(src), '--as-of', '2026-01-06', '--horizon', '8',
                         '--output', str(self.out))
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(self.out.read_text())
        self.assertEqual(report['cohorts']['g']['status'], 'risk_set_gap')
        self.assertIsNone(report['cohorts']['g']['horizon']['interval'])

    def test_cli_singular_is_success_diagnostic(self):
        src = self.root / 'sing.csv'
        src.write_text(HEADER + 'T1,g,2026-01-11,2026-01-11\nT2,g,2026-01-01,2026-01-01\n')
        r = self.run_cli('--source', str(src), '--as-of', '2026-01-11',
                         '--horizon', '5', '--output', str(self.out))
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(self.out.read_text())
        self.assertEqual(report['cohorts']['g']['horizon']['status'], 'greenwood_singular')

    def test_cli_bad_input_keeps_old_report(self):
        good = self.root / 'good.csv'
        good.write_text(HEADER + 'X1,g,2026-01-01,\n')
        r = self.run_cli('--source', str(good), '--as-of', AS_OF, '--horizon', '10',
                         '--output', str(self.out))
        self.assertEqual(r.returncode, 0, r.stderr)
        before = self.out.read_bytes()
        bad = self.root / 'bad.csv'
        bad.write_text(HEADER + 'X1,g,2026-1-1,\n')
        r2 = self.run_cli('--source', str(bad), '--as-of', AS_OF, '--horizon', '10',
                          '--output', str(self.out))
        self.assertNotEqual(r2.returncode, 0)
        self.assertEqual(self.out.read_bytes(), before)
        missing = self.run_cli('--source', str(self.root / 'nope.csv'), '--as-of', AS_OF,
                               '--horizon', '10', '--output', str(self.out))
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(self.out.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
