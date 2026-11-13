from pathlib import Path
import json
import tempfile
import unittest
from repair_survival.report import analyze
from repair_survival.subjects import subjects
from repair_survival.estimate import estimate
FIX=Path(__file__).resolve().parents[1]/'fixtures/assets.csv'
class SurvivalTest(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);self.out=self.root/'report.json'
    def run_report(self):return analyze(FIX,'2026-01-11',10,self.out)
    def test_risk_ties_and_rmst(self):
        r=self.run_report();a=r['cohorts']['seal-A']
        self.assertEqual(a['median_days'],5);self.assertEqual(a['rmst_days'],6.75)
        self.assertEqual(a['curve'][1],dict(day=5,at_risk=3,failures=1,censored=1,survival=0.5,survival_fraction='1/2'))
        self.assertEqual(r['excluded_future_installations'],['F1'])
    def test_no_events(self):
        b=self.run_report()['cohorts']['seal-B'];self.assertEqual(b['events'],0);self.assertIsNone(b['median_days']);self.assertEqual(b['rmst_days'],10)
    def test_repeated_bytes(self):
        self.run_report();before=self.out.read_bytes();self.run_report();self.assertEqual(before,self.out.read_bytes())
    def test_no_extrapolation_preserves_output(self):
        self.run_report();before=self.out.read_bytes()
        with self.assertRaises(ValueError):analyze(FIX,'2026-01-11',11,self.out)
        self.assertEqual(before,self.out.read_bytes())
    def test_zero_day_and_cutoff_event(self):
        p=self.root/'data.csv';p.write_text('asset_id,cohort,installed,failed\nx,A,2026-01-11,2026-01-11\ny,A,2026-01-01,\n')
        rows,_=subjects(p,'2026-01-11');a=estimate(rows,10)['A']
        self.assertEqual(a['curve'][0]['day'],0);self.assertEqual(a['median_days'],0);self.assertEqual(a['rmst_days'],5)
    def test_duplicate_asset(self):
        p=self.root/'data.csv';p.write_text(FIX.read_text()+'A1,seal-A,2026-01-01,\n')
        with self.assertRaisesRegex(ValueError,'duplicate'):subjects(p,'2026-01-11')
    def test_failure_before_install(self):
        p=self.root/'data.csv';p.write_text('asset_id,cohort,installed,failed\nx,A,2026-01-03,2026-01-02\n')
        with self.assertRaises(ValueError):subjects(p,'2026-01-11')
    def test_all_future(self):
        with self.assertRaisesRegex(ValueError,'no observed'):subjects(FIX,'2025-01-01')
