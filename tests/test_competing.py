import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

from repair_survival import competing

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures' / 'competing.csv'
SRC = ROOT / 'src'
AS_OF = '2026-01-07'
HEADER = 'asset_id,cohort,installed,observed_from,exit_date,exit_kind\n'


def frac(pair):
    return Fraction(pair['fraction'])


class CompetingTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.out = self.root / 'report.json'

    def write(self, text, name='data.csv'):
        p = self.root / name
        p.write_text(text, encoding='utf-8')
        return p

    def build(self, text, as_of=AS_OF, horizon=6, name='data.csv'):
        return competing.build_report(self.write(text, name), as_of, horizon)

    def run_fixture(self):
        return competing.analyze(FIX, AS_OF, 6, self.out)

    def test_worked_example_membership_and_segments(self):
        c = self.run_fixture()['cohorts']['seal-C']
        self.assertEqual(c['status'], 'estimable')
        self.assertEqual(c['subjects'], 5)
        nodes = c['risk_table']
        self.assertEqual([n['day'] for n in nodes], [0, 2, 3, 4, 6])
        n0, n2, n3, n4, n6 = nodes
        self.assertEqual(n0['entered'], ['A', 'B', 'C'])
        self.assertEqual(n0['at_risk'], ['A', 'B', 'C'])
        self.assertEqual(n2['at_risk'], ['A', 'B', 'C'])
        self.assertEqual(n2['at_risk_count'], 3)
        self.assertEqual(n2['failures'], ['A'])
        self.assertEqual(n2['at_risk_after'], 2)
        self.assertEqual(frac(n2['survival']), Fraction(2, 3))
        self.assertEqual(n3['entered'], ['D'])
        self.assertEqual(n3['at_risk'], ['B', 'C', 'D'])
        self.assertEqual(n4['entered'], ['E'])
        self.assertEqual(n4['at_risk'], ['B', 'C', 'D', 'E'])
        self.assertEqual(n4['at_risk_count'], 4)
        self.assertEqual(n4['replacements'], ['B'])
        self.assertEqual(frac(n4['survival']), Fraction(1, 2))
        self.assertEqual(frac(n4['replacement_cumulative']), Fraction(1, 6))
        self.assertEqual(n6['at_risk'], ['C', 'D', 'E'])
        self.assertEqual(n6['at_risk_count'], 3)
        self.assertEqual(n6['failures'], ['D'])
        self.assertEqual(n6['censored'], ['C', 'E'])
        self.assertEqual(frac(n6['survival']), Fraction(1, 3))
        self.assertEqual(frac(n6['failure_cumulative']), Fraction(1, 2))
        self.assertEqual(frac(n6['replacement_cumulative']), Fraction(1, 6))
        # 逐段面积: [0,2)=2, [2,3)=2/3, [3,4)=2/3, [4,6)=1
        expected = [Fraction(2), Fraction(2, 3), Fraction(2, 3), Fraction(1)]
        total = Fraction(0)
        for cur, nxt, want in zip(nodes, nodes[1:], expected):
            seg = frac(cur['survival']) * (nxt['day'] - cur['day'])
            self.assertEqual(seg, want)
            total += seg
        self.assertEqual(total, Fraction(13, 3))
        h = c['horizon']
        self.assertTrue(h['estimable'])
        self.assertEqual(h['survival'], {'fraction': '1/3', 'value': 1 / 3})
        self.assertEqual(h['failure_cumulative']['fraction'], '1/2')
        self.assertEqual(h['replacement_cumulative']['fraction'], '1/6')
        self.assertEqual(h['rmst_days'], {'fraction': '13/3', 'value': 13 / 3})
        self.assertEqual(frac(h['rmst_days']), total)

    def test_fixture_exclusions_and_censor_reasons(self):
        r = self.run_fixture()
        dev = {d['asset_id']: d for d in r['devices']}
        self.assertEqual(dev['H']['status'], 'excluded')
        self.assertEqual(dev['H']['exclusion_reason'], 'not_installed_by_cutoff')
        self.assertIsNone(dev['H']['entry_age_days'])
        self.assertEqual(dev['I']['exclusion_reason'], 'not_enrolled_by_cutoff')
        j = dev['J']
        self.assertEqual(j['exit_kind'], 'censor')
        self.assertEqual(j['censor_reason'], 'exit_after_cutoff')
        self.assertEqual(j['declared_exit_date'], '2026-01-10')
        self.assertEqual(j['declared_exit_kind'], 'failure')
        self.assertEqual(j['entry_age_days'], 1)
        self.assertEqual(j['exit_age_days'], 6)
        self.assertEqual(dev['C']['censor_reason'], 'no_declared_exit')
        self.assertEqual(dev['A']['record'], 1)
        self.assertEqual(dev['J']['record'], 10)
        y = r['cohorts']['seal-Y']
        self.assertEqual(y['status'], 'no_baseline_support')
        self.assertEqual(y['gap'], {'start_day': 0, 'end_day': 1})
        self.assertEqual(y['support_end_day'], 0)
        self.assertIsNone(y['horizon']['survival'])
        self.assertIsNone(y['horizon']['rmst_days'])
        z = r['cohorts']['seal-Z']
        self.assertEqual(z['status'], 'estimable')
        self.assertEqual(z['horizon']['survival']['fraction'], '1/2')
        self.assertEqual(z['horizon']['replacement_cumulative']['fraction'], '1/2')
        self.assertEqual(z['horizon']['failure_cumulative']['fraction'], '0')
        self.assertEqual(z['horizon']['rmst_days']['fraction'], '5')

    def test_source_digest_and_file_matches_return(self):
        r = self.run_fixture()
        self.assertEqual(r['schema_version'], 2)
        self.assertEqual(r['mode'], 'delayed_entry_competing_risks')
        self.assertEqual(r['parameters']['source_sha256'], hashlib.sha256(FIX.read_bytes()).hexdigest())
        self.assertEqual(r['parameters']['as_of'], AS_OF)
        self.assertEqual(r['parameters']['horizon_days'], 6)
        self.assertEqual(self.out.read_text(), json.dumps(r, indent=2, sort_keys=True) + '\n')

    def test_repeated_runs_byte_identical(self):
        self.run_fixture()
        before = self.out.read_bytes()
        self.run_fixture()
        self.assertEqual(before, self.out.read_bytes())

    def test_probability_mass_conserved(self):
        r = self.run_fixture()
        for c in r['cohorts'].values():
            for node in c['risk_table']:
                if node['survival'] is None:
                    continue
                total = frac(node['survival']) + frac(node['failure_cumulative']) + frac(node['replacement_cumulative'])
                self.assertEqual(total, 1)

    def test_independent_risk_set_enumeration(self):
        # 独立参照:直接按 e<=t<=x 逐日枚举成员,与生产边界扫描的风险集核对。
        spec = {'A': (0, 2), 'B': (0, 4), 'C': (0, 6), 'D': (3, 6), 'E': (4, 6)}
        c = self.run_fixture()['cohorts']['seal-C']
        for node in c['risk_table']:
            t = node['day']
            want = sorted(a for a, (e, x) in spec.items() if e <= t <= x)
            self.assertEqual(node['at_risk'], want)
            self.assertEqual(node['at_risk_count'], len(want))

    def test_shared_risk_set_row_order_invariant(self):
        rows = [
            'P1,g,2026-01-01,2026-01-01,2026-01-03,failure',
            'P2,g,2026-01-01,2026-01-01,2026-01-03,replacement',
            'P3,g,2026-01-01,2026-01-01,,',
        ]
        base = self.build(HEADER + '\n'.join(rows) + '\n', as_of='2026-01-05', horizon=4)
        shuffled = self.build(HEADER + '\n'.join(reversed(rows)) + '\n', as_of='2026-01-05', horizon=4, name='shuffled.csv')
        self.assertEqual(base['cohorts'], shuffled['cohorts'])
        g = base['cohorts']['g']
        n2 = g['risk_table'][1]
        self.assertEqual(n2['day'], 2)
        self.assertEqual(n2['at_risk'], ['P1', 'P2', 'P3'])
        self.assertEqual(n2['failures'], ['P1'])
        self.assertEqual(n2['replacements'], ['P2'])
        self.assertEqual(frac(n2['failure_cumulative']), Fraction(1, 3))
        self.assertEqual(frac(n2['replacement_cumulative']), Fraction(1, 3))
        self.assertEqual(frac(n2['survival']), Fraction(1, 3))
        self.assertEqual(g['horizon']['rmst_days']['fraction'], '8/3')
        # 记录序号与原文摘要反映真实输入顺序
        self.assertEqual({d['asset_id']: d['record'] for d in shuffled['devices']},
                         {'P3': 1, 'P2': 2, 'P1': 3})
        self.assertNotEqual(base['parameters']['source_sha256'], shuffled['parameters']['source_sha256'])

    def test_risk_set_gap_blocks_horizon_and_reentry(self):
        # Q1 年龄0进入、2删失;Q2 年龄4进入、6删失(不同安装日,同一截止日)
        text = HEADER + 'Q1,g,2026-01-05,2026-01-05,,\nQ2,g,2026-01-01,2026-01-05,,\n'
        g = self.build(text)['cohorts']['g']
        self.assertEqual(g['status'], 'risk_set_gap')
        self.assertEqual(g['gap'], {'start_day': 2, 'end_day': 4})
        self.assertEqual(g['support_end_day'], 2)
        self.assertEqual(g['prefix']['day'], 2)
        self.assertEqual(g['prefix']['survival']['fraction'], '1')
        self.assertEqual(g['prefix']['rmst_days']['fraction'], '2')
        h = g['horizon']
        self.assertFalse(h['estimable'])
        for key in ('survival', 'failure_cumulative', 'replacement_cumulative', 'rmst_days'):
            self.assertIsNone(h[key])
        nodes = {n['day']: n for n in g['risk_table']}
        self.assertEqual(sorted(nodes), [0, 2, 4, 6])
        self.assertEqual(nodes[2]['censored'], ['Q1'])
        self.assertEqual(nodes[2]['survival']['fraction'], '1')
        # 年龄4再进入不能恢复:成员与计数仍在,概率为空且不继续累积
        self.assertEqual(nodes[4]['entered'], ['Q2'])
        self.assertEqual(nodes[4]['at_risk'], ['Q2'])
        self.assertIsNone(nodes[4]['survival'])
        self.assertEqual(nodes[6]['censored'], ['Q2'])
        self.assertIsNone(nodes[6]['survival'])

    def test_no_baseline_support(self):
        text = (HEADER + 'R1,g,2026-01-01,2026-01-03,,\n'
                'R2,g,2026-01-01,2026-01-03,2026-01-05,failure\n')
        g = self.build(text)['cohorts']['g']
        self.assertEqual(g['status'], 'no_baseline_support')
        self.assertEqual(g['gap'], {'start_day': 0, 'end_day': 2})
        self.assertEqual(g['support_end_day'], 0)
        self.assertEqual(g['prefix']['day'], 0)
        self.assertIsNone(g['horizon']['survival'])
        self.assertIsNone(g['horizon']['rmst_days'])
        n0 = g['risk_table'][0]
        self.assertEqual(n0['day'], 0)
        self.assertEqual(n0['at_risk'], [])

    def test_censor_exactly_at_horizon_supported(self):
        text = HEADER + 'S1,g,2026-01-01,2026-01-01,,\n'
        g = self.build(text)['cohorts']['g']
        self.assertEqual(g['status'], 'estimable')
        self.assertEqual(g['horizon']['survival']['fraction'], '1')
        self.assertEqual(g['horizon']['failure_cumulative']['fraction'], '0')
        self.assertEqual(g['horizon']['replacement_cumulative']['fraction'], '0')
        self.assertEqual(g['horizon']['rmst_days']['fraction'], '6')
        # horizon=7 时 [6,7) 失去支持
        g7 = self.build(text, horizon=7, name='h7.csv')['cohorts']['g']
        self.assertEqual(g7['status'], 'risk_set_gap')
        self.assertEqual(g7['gap'], {'start_day': 6, 'end_day': 7})
        self.assertIsNone(g7['horizon']['rmst_days'])
        self.assertEqual(g7['prefix']['rmst_days']['fraction'], '6')

    def test_zero_day_failure_absorbs_all_mass(self):
        text = HEADER + 'T1,g,2026-01-01,2026-01-01,2026-01-01,failure\n'
        g = self.build(text, horizon=5)['cohorts']['g']
        self.assertEqual(g['status'], 'estimable')
        self.assertEqual(g['horizon']['survival']['fraction'], '0')
        self.assertEqual(g['horizon']['failure_cumulative']['fraction'], '1')
        self.assertEqual(g['horizon']['replacement_cumulative']['fraction'], '0')
        self.assertEqual(g['horizon']['rmst_days']['fraction'], '0')
        n0 = g['risk_table'][0]
        self.assertEqual(n0['at_risk'], ['T1'])
        self.assertEqual(n0['failures'], ['T1'])
        self.assertEqual(n0['at_risk_after'], 0)

    def test_same_day_entry_and_exit(self):
        text = (HEADER + 'W1,g,2026-01-01,2026-01-01,,\n'
                'W2,g,2026-01-01,2026-01-04,2026-01-04,failure\n')
        g = self.build(text, as_of='2026-01-06', horizon=5)['cohorts']['g']
        n3 = g['risk_table'][1]
        self.assertEqual(n3['day'], 3)
        self.assertEqual(n3['entered'], ['W2'])
        self.assertEqual(n3['at_risk'], ['W1', 'W2'])
        self.assertEqual(n3['failures'], ['W2'])
        self.assertEqual(frac(n3['failure_cumulative']), Fraction(1, 2))
        self.assertEqual(frac(n3['survival']), Fraction(1, 2))
        self.assertEqual(g['horizon']['rmst_days']['fraction'], '4')

    def test_failure_and_censor_share_denominator(self):
        text = (HEADER + 'V1,g,2026-01-01,2026-01-01,2026-01-03,failure\n'
                'V2,g,2026-01-01,2026-01-01,,\n')
        g = self.build(text, as_of='2026-01-03', horizon=2)['cohorts']['g']
        n2 = g['risk_table'][1]
        self.assertEqual(n2['failures'], ['V1'])
        self.assertEqual(n2['censored'], ['V2'])
        self.assertEqual(n2['at_risk_count'], 2)
        self.assertEqual(frac(n2['failure_cumulative']), Fraction(1, 2))
        self.assertEqual(frac(n2['survival']), Fraction(1, 2))
        self.assertEqual(g['status'], 'estimable')

    def test_same_age_meets_on_age_axis_across_install_dates(self):
        text = (HEADER + 'X1,g,2026-01-01,2026-01-01,2026-01-03,failure\n'
                'X2,g,2026-01-05,2026-01-05,2026-01-07,failure\n')
        g = self.build(text, as_of='2026-01-10', horizon=2)['cohorts']['g']
        n0, n2 = g['risk_table']
        self.assertEqual(n0['at_risk'], ['X1', 'X2'])
        self.assertEqual(n2['failures'], ['X1', 'X2'])
        self.assertEqual(n2['at_risk_count'], 2)
        self.assertEqual(frac(n2['failure_cumulative']), 1)
        self.assertEqual(frac(n2['survival']), 0)
        self.assertEqual(g['horizon']['rmst_days']['fraction'], '2')

    def test_entry_after_horizon_never_joins_and_no_fake_censoring(self):
        text = (HEADER + 'Y1,g,2026-01-01,2026-01-01,,\n'
                'Y2,g,2026-01-01,2026-01-06,,\n')
        r = self.build(text, as_of='2026-01-10', horizon=3)
        g = r['cohorts']['g']
        self.assertEqual([n['day'] for n in g['risk_table']], [0, 3])
        for n in g['risk_table']:
            self.assertEqual(n['at_risk'], ['Y1'])
        last = g['risk_table'][-1]
        self.assertEqual(last['at_risk_after'], 1)  # horizon 不伪造行政删失
        self.assertEqual(g['horizon']['rmst_days']['fraction'], '3')
        dev = {d['asset_id']: d for d in r['devices']}
        self.assertEqual(dev['Y2']['entry_age_days'], 5)

    def test_horizon_validation(self):
        p = self.write(HEADER + 'K1,g,2026-01-01,2026-01-01,,\n')
        for bad in (True, False, 0, -3, 2.5, '6'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                competing.build_report(p, AS_OF, bad)

    def test_all_excluded_fails(self):
        p = self.write(HEADER + 'K1,g,2026-03-01,2026-03-01,,\n')
        with self.assertRaisesRegex(ValueError, 'no enrolled'):
            competing.build_report(p, AS_OF, 6)

    def test_contract_errors_rejected(self):
        cases = {
            'duplicate_asset': HEADER + 'K1,g,2026-01-01,2026-01-01,,\nK1,g,2026-03-01,2026-03-01,,\n',
            'unknown_kind': HEADER + 'K1,g,2026-01-01,2026-01-01,2026-01-02,retired\n',
            'date_without_kind': HEADER + 'K1,g,2026-01-01,2026-01-01,2026-01-02,\n',
            'kind_without_date': HEADER + 'K1,g,2026-01-01,2026-01-01,,failure\n',
            'exit_before_observed': HEADER + 'K1,g,2026-01-01,2026-01-03,2026-01-02,failure\n',
            'observed_before_installed': HEADER + 'K1,g,2026-01-03,2026-01-01,,\n',
            'missing_column': 'asset_id,cohort,installed,observed_from,exit_date\nK1,g,2026-01-01,2026-01-01,\n',
            'extra_column': HEADER[:-1] + ',note\nK1,g,2026-01-01,2026-01-01,,,x\n',
            'ragged_short': HEADER + 'K1,g,2026-01-01,2026-01-01,\n',
            'ragged_long': HEADER + 'K1,g,2026-01-01,2026-01-01,,,x\n',
            'bad_date': HEADER + 'K1,g,2026-1-1,2026-01-01,,\n',
            'bad_exit_date': HEADER + 'K1,g,2026-01-01,2026-01-01,2026-02-30,failure\n',
            'whitespace_id': HEADER + ' K1,g,2026-01-01,2026-01-01,,\n',
            'empty_cohort': HEADER + 'K1,,2026-01-01,2026-01-01,,\n',
            'unknown_kind_excluded_row': HEADER + 'K1,g,2026-01-01,2026-01-01,,\nK2,g,2026-03-01,2026-03-01,2026-03-02,retired\n',
            'empty_file': '',
            'header_only': HEADER,
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    competing.build_report(self.write(text, name + '.csv'), AS_OF, 6)

    def test_contract_error_preserves_old_report(self):
        good = self.write(HEADER + 'K1,g,2026-01-01,2026-01-01,,\n', 'good.csv')
        bad = self.write(HEADER + 'K1,g,2026-01-01,2026-01-01,,\nK1,g,2026-01-01,2026-01-01,,\n', 'bad.csv')
        competing.analyze(good, AS_OF, 6, self.out)
        before = self.out.read_bytes()
        with self.assertRaises(ValueError):
            competing.analyze(bad, AS_OF, 6, self.out)
        self.assertEqual(self.out.read_bytes(), before)
        # 修正后可直接重跑
        competing.analyze(good, AS_OF, 6, self.out)
        self.assertEqual(self.out.read_bytes(), before)

    def test_replace_failure_preserves_old_report_and_cleans_temp(self):
        good = self.write(HEADER + 'K1,g,2026-01-01,2026-01-01,,\n', 'good.csv')
        competing.analyze(good, AS_OF, 6, self.out)
        before = self.out.read_bytes()
        with mock.patch.object(competing.os, 'replace', side_effect=OSError('injected replace failure')):
            with self.assertRaises(OSError):
                competing.analyze(good, AS_OF, 6, self.out)
        self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual([p for p in self.root.iterdir() if p.suffix == '.tmp'], [])

    def test_write_failure_preserves_old_report_and_cleans_temp(self):
        good = self.write(HEADER + 'K1,g,2026-01-01,2026-01-01,,\n', 'good.csv')
        competing.analyze(good, AS_OF, 6, self.out)
        before = self.out.read_bytes()

        def bad_fdopen(fd, *a, **k):
            os.close(fd)
            raise OSError('injected write failure')

        with mock.patch.object(competing.os, 'fdopen', side_effect=bad_fdopen):
            with self.assertRaises(OSError):
                competing.analyze(good, AS_OF, 6, self.out)
        self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual([p for p in self.root.iterdir() if p.suffix == '.tmp'], [])


class CompetingCliTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.out = self.root / 'report.json'

    def run_cli(self, *args):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(SRC) + os.pathsep + env.get('PYTHONPATH', '')
        return subprocess.run([sys.executable, '-m', 'repair_survival.competing', *args],
                              capture_output=True, text=True, env=env, cwd=self.root)

    def test_cli_end_to_end_and_byte_identical_rerun(self):
        args = ('--source', str(FIX), '--as-of', AS_OF, '--horizon', '6', '--output', str(self.out))
        r1 = self.run_cli(*args)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        summary = json.loads(r1.stdout)
        self.assertEqual(summary['mode'], 'delayed_entry_competing_risks')
        self.assertEqual(summary['cohorts']['seal-C']['status'], 'estimable')
        self.assertEqual(summary['cohorts']['seal-Y']['status'], 'no_baseline_support')
        report = json.loads(self.out.read_text())
        self.assertEqual(report['schema_version'], 2)
        self.assertEqual(report['parameters']['source_sha256'], hashlib.sha256(FIX.read_bytes()).hexdigest())
        c = report['cohorts']['seal-C']
        self.assertEqual(c['horizon']['rmst_days']['fraction'], '13/3')
        self.assertEqual(c['horizon']['failure_cumulative']['fraction'], '1/2')
        self.assertEqual(c['horizon']['replacement_cumulative']['fraction'], '1/6')
        self.assertEqual(c['horizon']['survival']['fraction'], '1/3')
        first = self.out.read_bytes()
        r2 = self.run_cli(*args)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(self.out.read_bytes(), first)

    def test_cli_gap_is_success_with_diagnostics(self):
        src = self.root / 'gap.csv'
        src.write_text(HEADER + 'Q1,g,2026-01-05,2026-01-05,,\nQ2,g,2026-01-01,2026-01-05,,\n')
        r = self.run_cli('--source', str(src), '--as-of', AS_OF, '--horizon', '6', '--output', str(self.out))
        self.assertEqual(r.returncode, 0, r.stderr)
        summary = json.loads(r.stdout)
        self.assertEqual(summary['cohorts']['g']['status'], 'risk_set_gap')
        report = json.loads(self.out.read_text())
        self.assertIsNone(report['cohorts']['g']['horizon']['rmst_days'])
        self.assertIsNone(report['cohorts']['g']['horizon']['survival'])

    def test_cli_contract_error_keeps_old_report(self):
        r = self.run_cli('--source', str(FIX), '--as-of', AS_OF, '--horizon', '6', '--output', str(self.out))
        self.assertEqual(r.returncode, 0, r.stderr)
        before = self.out.read_bytes()
        bad = self.root / 'bad.csv'
        bad.write_text(HEADER + 'K1,g,2026-01-01,2026-01-01,2026-01-02,retired\n')
        r2 = self.run_cli('--source', str(bad), '--as-of', AS_OF, '--horizon', '6', '--output', str(self.out))
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn('unknown exit_kind', r2.stderr)
        self.assertEqual(self.out.read_bytes(), before)

    def test_cli_invalid_arguments_nonzero(self):
        r = self.run_cli('--source', str(FIX), '--as-of', AS_OF, '--horizon', '0', '--output', str(self.out))
        self.assertNotEqual(r.returncode, 0)
        r = self.run_cli('--source', str(self.root / 'missing.csv'), '--as-of', AS_OF, '--horizon', '6',
                         '--output', str(self.out))
        self.assertNotEqual(r.returncode, 0)
        r = self.run_cli('--source', str(FIX), '--as-of', '2026-1-7', '--horizon', '6', '--output', str(self.out))
        self.assertNotEqual(r.returncode, 0)


if __name__ == '__main__':
    unittest.main()
