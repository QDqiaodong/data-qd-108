"""离线工作簿采集适配器测试。

所有用例都围绕 :mod:`repair_survival.workbook.synth` 生成的真实 OOXML 包,
不手工构造解析器内部对象。测试覆盖验收清单中的身份/日期规则、包结构反例、
真实坐标审计、确定性发布与旧 CLI 贯通。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

from repair_survival.workbook import adapter, dates, synth
from repair_survival.workbook.adapter import WorkbookError, build_collection, collect, load_collection
from repair_survival.workbook.synth import RichText, Sheet, Workbook, XCell

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
FIXTURES = ROOT / 'fixtures'
ASSET_FIX = FIXTURES / 'assets.csv'

ASSET_HEADER = ['asset_id', 'cohort', 'installed']
FAULT_HEADER = ['ticket_id', 'asset_id', 'failed_on', 'status']


def asset_sheet(rows, name='Assets'):
    sheet = Sheet(name)
    sheet.row(1, [XCell.text(h) for h in ASSET_HEADER])
    for index, values in enumerate(rows, start=2):
        sheet.row(index, values)
    return sheet


def fault_sheet(rows, name='Faults'):
    sheet = Sheet(name)
    sheet.row(1, [XCell.text(h) for h in FAULT_HEADER])
    for index, values in enumerate(rows, start=2):
        sheet.row(index, values)
    return sheet


def acceptance_workbook(date1904=False, add_a1_tie=False, extra_sheet=False):
    """验收台账:0007、7、B 同 cohort 同日安装;0007 有 T2/T1 确认票与 T0 取消票。"""
    if date1904:
        jan1, jan2, jan6, jan8 = 44561, 44562, 44566, 44568
    else:
        jan1, jan2, jan6, jan8 = 46023, 46024, 46028, 46030
    wb = Workbook(date1904=date1904)
    assets = asset_sheet([
        [XCell.text('0007'), XCell.text('seal-A'), XCell.number(jan1)],
        [XCell.text('7'), XCell.text('seal-A'), XCell.number(jan1)],
        [XCell.text('B'), XCell.text('seal-A'), XCell.number(jan1)],
    ])
    faults = fault_sheet([
        [XCell.text('T2'), XCell.text('0007'), XCell.number(jan8), XCell.text('CONFIRMED')],
        [XCell.text('T1'), XCell.text('0007'), XCell.number(jan6), XCell.text('CONFIRMED')],
        [XCell.text('T0'), XCell.text('0007'), XCell.number(jan2), XCell.text('CANCELLED')],
    ])
    if add_a1_tie:
        faults.row(5, [XCell.text('A1'), XCell.text('0007'), XCell.number(jan6), XCell.text('CONFIRMED')])
    wb.add_sheet(assets)
    if extra_sheet:
        other = Sheet('Notes')
        other.row(1, [XCell.text('note')])
        other.row(2, [XCell.text('not collected')])
        wb.add_sheet(other)
    wb.add_sheet(faults)
    return wb


def csv_rows(text):
    import csv
    return list(csv.reader(io.StringIO(text)))


class DateSerialTest(unittest.TestCase):
    """日期规则独立核对:不通过生产转换函数生成预期,显式断言日历锚点。"""

    def test_1900_anchors(self):
        self.assertEqual(dates.serial_to_date(1, False), date(1900, 1, 1))
        self.assertEqual(dates.serial_to_date(2, False), date(1900, 1, 2))
        self.assertEqual(dates.serial_to_date(59, False), date(1900, 2, 28))
        self.assertEqual(dates.serial_to_date(61, False), date(1900, 3, 1))
        self.assertEqual(dates.serial_to_date(367, False), date(1901, 1, 1))
        self.assertEqual(dates.serial_to_date(46023, False), date(2026, 1, 1))
        self.assertEqual(dates.serial_to_date(46028, False), date(2026, 1, 6))

    def test_1900_fictitious_leap_and_bounds(self):
        with self.assertRaisesRegex(ValueError, 'fictitious'):
            dates.serial_to_date(60, False)
        for bad in (0, -1, -60):
            with self.assertRaises(ValueError, msg=repr(bad)):
                dates.serial_to_date(bad, False)

    def test_1904_anchors(self):
        self.assertEqual(dates.serial_to_date(0, True), date(1904, 1, 1))
        self.assertEqual(dates.serial_to_date(1, True), date(1904, 1, 2))
        self.assertEqual(dates.serial_to_date(44561, True), date(2026, 1, 1))
        self.assertEqual(dates.serial_to_date(44566, True), date(2026, 1, 6))
        with self.assertRaises(ValueError):
            dates.serial_to_date(-1, True)

    def test_leap_day_real_calendar(self):
        # 2024 是闰年:2024-02-29 合法,2023-02-29 不存在。
        self.assertEqual(dates.strict_iso_text('2024-02-29'), date(2024, 2, 29))
        with self.assertRaises(ValueError):
            dates.strict_iso_text('2023-02-29')

    def test_strict_iso_rejects_loose_forms(self):
        for bad in ('2026-1-1', '2026-01-1', '2026/01/01', '20260101', ' 2026-01-01', '2026-02-30'):
            with self.assertRaises(ValueError, msg=bad):
                dates.strict_iso_text(bad)

    def test_integer_token_exactness(self):
        self.assertEqual(dates.integer_token('46023'), 46023)
        self.assertEqual(dates.integer_token('-5'), -5)
        for bad in ('46023.5', '46023.0', '1e3', '+5', ' 5', '', '5.'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                dates.integer_token(bad)

    def test_fractional_serial_rejected_at_collection(self):
        wb = Workbook()
        assets = asset_sheet([[XCell.text('A'), XCell.text('g'), XCell.number('46023.5')]])
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        with self.assertRaisesRegex(WorkbookError, 'exact integer'):
            build_collection(wb.build())

    def test_out_of_range_serial(self):
        huge = '999999999999'
        for date1904 in (False, True):
            with self.subTest(date1904=date1904):
                wb = Workbook(date1904=date1904)
                assets = asset_sheet([[XCell.text('A'), XCell.text('g'), XCell.number(huge)]])
                wb.add_sheet(assets).add_sheet(fault_sheet([]))
                with self.assertRaises(WorkbookError):
                    build_collection(wb.build())

    def test_anchor_serials_through_real_workbooks(self):
        # 独立预期:59/61(1900)与 0(1904)经真实 xlsx 单元格转换为显式日历日。
        cases = [
            (False, 59, '1900-02-28'),
            (False, 61, '1900-03-01'),
            (True, 0, '1904-01-01'),
        ]
        for date1904, serial, iso in cases:
            with self.subTest(system=1904 if date1904 else 1900, serial=serial):
                wb = Workbook(date1904=date1904)
                assets = asset_sheet([[XCell.text('A'), XCell.text('g'), XCell.number(serial)]])
                wb.add_sheet(assets).add_sheet(fault_sheet([]))
                collection = build_collection(wb.build())
                self.assertEqual(collection['assets'][0]['installed'], iso)

    def test_real_leap_day_date_cell(self):
        # 2024-02-29 作为 ISO 文本与序列都合法;2023-02-29 文本拒绝。
        wb = Workbook()
        assets = asset_sheet([
            [XCell.text('L1'), XCell.text('g'), XCell.inline('2024-02-29')],
        ])
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        collection = build_collection(wb.build())
        self.assertEqual(collection['assets'][0]['installed'], '2024-02-29')
        bad = Workbook()
        bad_assets = asset_sheet([[XCell.text('L2'), XCell.text('g'), XCell.inline('2023-02-29')]])
        bad.add_sheet(bad_assets).add_sheet(fault_sheet([]))
        with self.assertRaises(WorkbookError):
            build_collection(bad.build())


class AcceptanceCollectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_identity_and_earliest_failure_selection(self):
        collection = build_collection(acceptance_workbook().build())
        ids = [asset['asset_id'] for asset in collection['assets']]
        self.assertEqual(ids, ['0007', '7', 'B'])  # 稳定排序且前导零保留
        chosen = {a['asset_id']: a['selected_ticket_id'] for a in collection['assets']}
        self.assertEqual(chosen, {'0007': 'T1', '7': None, 'B': None})
        audit_0007 = next(a for a in collection['assets'] if a['asset_id'] == '0007')
        self.assertEqual(audit_0007['candidate_confirmed_tickets'], ['T1', 'T2'])
        reasons = {d['ticket_id']: d for d in audit_0007['ticket_dispositions']}
        self.assertTrue(reasons['T1']['selected'])
        self.assertEqual(reasons['T1']['reason'], 'selected_earliest_confirmed')
        self.assertFalse(reasons['T2']['selected'])
        self.assertEqual(reasons['T2']['reason'], 'confirmed_later_than_selected')
        self.assertEqual(reasons['T0']['reason'], 'cancelled_not_eligible')

    def test_same_date_tie_uses_ticket_id_codepoint_order(self):
        collection = build_collection(acceptance_workbook(add_a1_tie=True).build())
        audit = next(a for a in collection['assets'] if a['asset_id'] == '0007')
        self.assertEqual(audit['selected_ticket_id'], 'A1')  # A1 < T1 按 Unicode 码点
        self.assertEqual(audit['candidate_confirmed_tickets'], ['A1', 'T1', 'T2'])
        disposition = {d['ticket_id']: d for d in audit['ticket_dispositions']}
        self.assertEqual(disposition['T1']['reason'], 'confirmed_same_date_lost_ticket_id_order')
        self.assertTrue(disposition['A1']['selected'])

    def test_1900_and_1904_workbooks_produce_identical_csv(self):
        wb1900 = acceptance_workbook(False).build()
        wb1904 = acceptance_workbook(True).build()
        out1 = self.root / 'a'; out2 = self.root / 'b'
        source1 = self.root / 'a.xlsx'; source1.write_bytes(wb1900)
        source2 = self.root / 'b.xlsx'; source2.write_bytes(wb1904)
        collect(source1, out1)
        collect(source2, out2)
        csv1 = (out1 / 'assets.csv').read_text(encoding='utf-8')
        csv2 = (out2 / 'assets.csv').read_text(encoding='utf-8')
        self.assertEqual(csv1, csv2)
        rows = csv_rows(csv1)
        self.assertEqual(rows[0], ['asset_id', 'cohort', 'installed', 'failed'])
        self.assertEqual(rows[1], ['0007', 'seal-A', '2026-01-01', '2026-01-06'])
        self.assertEqual(rows[2], ['7', 'seal-A', '2026-01-01', ''])
        self.assertEqual(rows[3], ['B', 'seal-A', '2026-01-01', ''])
        self.assertEqual(csv1[:1], 'a')  # 无 BOM

    def test_iso_text_dates_and_empty_faults(self):
        wb = Workbook()
        assets = asset_sheet([
            [XCell.text('X1'), XCell.text('g'), XCell.inline('2026-01-01')],
        ])
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        collection = build_collection(wb.build())
        self.assertEqual(collection['assets'][0]['installed'], '2026-01-01')
        self.assertIsNone(collection['assets'][0]['selected_ticket_id'])
        self.assertEqual(collection['faults'], [])

    def test_extra_sheet_is_ignored(self):
        collection = build_collection(acceptance_workbook(extra_sheet=True).build())
        roles = {s['name']: s['role'] for s in collection['workbook']['sheets']}
        self.assertEqual(roles['Assets'], 'assets')
        self.assertEqual(roles['Faults'], 'faults')
        self.assertEqual(roles['Notes'], 'ignored')

    def test_hidden_and_filtered_rows_are_still_facts(self):
        wb = acceptance_workbook()
        assets = wb.sheets[0]
        assets.hide_row(3)  # 设备 7 被隐藏
        faults = wb.sheets[-1]
        faults.hide_row(2)   # T2 被筛选隐藏
        collection = build_collection(wb.build())
        self.assertEqual([a['asset_id'] for a in collection['assets']], ['0007', '7', 'B'])
        ticket_ids = {t['ticket_id'] for t in collection['faults']}
        self.assertEqual(ticket_ids, {'T0', 'T1', 'T2'})


class StructureVariantTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _base_csv(self):
        source = self.root / 'base.xlsx'
        source.write_bytes(acceptance_workbook().build())
        out = self.root / 'base'
        collect(source, out)
        return (out / 'assets.csv').read_bytes()

    def test_reordered_columns_keep_coordinate_audit(self):
        wb = Workbook()
        assets = Sheet('Assets')
        # installed 在 A 列、asset_id 在 C 列,列序与默认不同
        assets.put('A1', XCell.text('installed'))
        assets.put('B1', XCell.text('cohort'))
        assets.put('C1', XCell.text('asset_id'))
        assets.put('A2', XCell.number(46023))
        assets.put('B2', XCell.text('seal-A'))
        assets.put('C2', XCell.text('0007'))
        faults = Sheet('Faults')
        faults.put('A1', XCell.text('status'))
        faults.put('B1', XCell.text('failed_on'))
        faults.put('C1', XCell.text('asset_id'))
        faults.put('D1', XCell.text('ticket_id'))
        faults.put('A2', XCell.text('CONFIRMED'))
        faults.put('B2', XCell.number(46028))
        faults.put('C2', XCell.text('0007'))
        faults.put('D2', XCell.text('T1'))
        wb.add_sheet(assets).add_sheet(faults)
        collection = build_collection(wb.build())
        asset = collection['assets'][0]
        by_field = {f['field']: f for f in asset['fields']}
        self.assertEqual(by_field['asset_id']['cell']['ref'], 'C2')
        self.assertEqual(by_field['installed']['cell']['ref'], 'A2')
        self.assertEqual(asset['installed'], '2026-01-01')
        ticket = collection['faults'][0]
        tfield = {f['field']: f for f in ticket['fields']}
        self.assertEqual(tfield['ticket_id']['cell']['ref'], 'D2')
        self.assertEqual(tfield['failed_on']['cell']['ref'], 'B2')

    def test_sparse_cells_do_not_shift_columns(self):
        wb = Workbook()
        assets = Sheet('Assets')
        assets.put('A1', XCell.text('asset_id'))
        assets.put('C1', XCell.text('cohort'))
        assets.put('E1', XCell.text('installed'))
        assets.put('A2', XCell.text('0007'))
        # B2/D2 真实缺失
        assets.put('C2', XCell.text('seal-A'))
        assets.put('E2', XCell.number(46023))
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        collection = build_collection(wb.build())
        fields = {f['field']: f for f in collection['assets'][0]['fields']}
        self.assertEqual(fields['asset_id']['cell']['ref'], 'A2')
        self.assertEqual(fields['cohort']['cell']['ref'], 'C2')
        self.assertEqual(fields['installed']['cell']['ref'], 'E2')

    def test_row_gaps_and_distant_styled_cell(self):
        wb = acceptance_workbook()
        assets = wb.sheets[0]
        # 远处仅样式单元格,不创造记录,dimension 故意夸大
        assets.put('Z99', XCell.styled_blank(0))
        assets.set_dimension('A1:Z99')
        collection = build_collection(wb.build())
        self.assertEqual(len(collection['assets']), 3)
        self.assertEqual(max(a['row'] for a in collection['assets']), 4)

    def test_rich_text_preserves_text_and_ignores_phonetic(self):
        wb = Workbook(shared_pool=[RichText(['00', '07'], phonetic='zero-zero-zero-seven')])
        assets = asset_sheet([
            [XCell.rich_shared(0, ['00', '07'], phonetic='zero-zero-zero-seven'),
             XCell.text('seal-A'), XCell.number(46023)],
        ])
        faults = fault_sheet([
            [XCell.rich_inline(['T', '1'], phonetic='tee-one'), XCell.inline('0007'),
             XCell.number(46028), XCell.inline('CONFIRMED')],
        ])
        wb.add_sheet(assets).add_sheet(faults)
        collection = build_collection(wb.build())
        asset = collection['assets'][0]
        id_field = next(f for f in asset['fields'] if f['field'] == 'asset_id')
        self.assertEqual(asset['asset_id'], '0007')
        self.assertTrue(id_field['cell']['rich'])
        self.assertNotIn('zero', json.dumps(collection))
        ticket_field = next(f for f in collection['faults'][0]['fields'] if f['field'] == 'ticket_id')
        self.assertEqual(ticket_field['cell']['raw'], 'T1')
        self.assertTrue(ticket_field['cell']['rich'])

    def test_shared_string_reorder_keeps_business_content(self):
        base = acceptance_workbook().build()
        # 重排字符串池:解析 pool 后用新顺序重写 sst 与索引。
        reordered = _reorder_shared_pool(base)
        base_csv = self._base_csv()
        source = self.root / 'reordered.xlsx'
        source.write_bytes(reordered)
        out = self.root / 'reordered-out'
        collect(source, out)
        self.assertEqual((out / 'assets.csv').read_bytes(), base_csv)
        reordered_audit = json.loads((out / 'cells.json').read_text())
        base_audit = build_collection(base)
        self.assertNotEqual(
            [h['cell'].get('shared_index') for h in base_audit['header_maps']['Assets']['columns']],
            [h['cell'].get('shared_index') for h in reordered_audit['header_maps']['Assets']['columns']])

    def test_worksheet_part_swap_does_not_change_roles_or_csv(self):
        wb = acceptance_workbook()
        wb.assign_parts({'Assets': '/xl/worksheets/sheet7.xml', 'Faults': '/xl/worksheets/sheet2.xml'})
        # 再加入一个按声明顺序排在前面的无关表,确保不按顺序猜角色
        notes = Sheet('Notes', part='/xl/worksheets/sheet1.xml')
        notes.put('A1', XCell.text('noise'))
        wb.sheets.insert(0, notes)
        collection = build_collection(wb.build())
        roles = {s['name']: s['part'] for s in collection['workbook']['sheets']}
        self.assertEqual(roles['Assets'], '/xl/worksheets/sheet7.xml')
        source = self.root / 'swap.xlsx'
        source.write_bytes(wb.build())
        out = self.root / 'swap-out'
        collect(source, out)
        self.assertEqual((out / 'assets.csv').read_bytes(), self._base_csv())

    def test_row_order_change_does_not_change_csv_but_audit_keeps_rows(self):
        wb = acceptance_workbook()
        assets = wb.sheets[0]
        # 交换数据行 2 与 4(0007 <-> B)
        cells_2 = dict(assets.rows[2].cells)
        cells_4 = dict(assets.rows[4].cells)
        assets.rows[2].cells, assets.rows[4].cells = cells_4, cells_2
        collection = build_collection(wb.build())
        self.assertEqual([a['asset_id'] for a in collection['assets']], ['0007', '7', 'B'])
        row_of = {a['asset_id']: a['row'] for a in collection['assets']}
        self.assertEqual(row_of['0007'], 4)
        self.assertEqual(row_of['B'], 2)

    def test_audit_coordinates_and_raw_tokens(self):
        collection = build_collection(acceptance_workbook().build())
        asset = next(a for a in collection['assets'] if a['asset_id'] == '0007')
        refs = {f['field']: f['cell']['ref'] for f in asset['fields']}
        self.assertEqual(refs, {'asset_id': 'A2', 'cohort': 'B2', 'installed': 'C2'})
        installed = next(f for f in asset['fields'] if f['field'] == 'installed')
        self.assertEqual(installed['cell']['kind'], 'number')
        self.assertEqual(installed['cell']['raw'], '46023')
        self.assertEqual(installed['serial_source'], 'excel_serial')
        ticket = next(t for t in collection['faults'] if t['ticket_id'] == 'T1')
        self.assertEqual(ticket['fields'][0]['cell']['ref'], 'A3')


def _reorder_shared_pool(blob: bytes) -> bytes:
    """重排 sharedStrings 池顺序并同步所有 t="s" 索引,生成同内容不同池的包。"""
    import xml.etree.ElementTree as ET
    zin = zipfile.ZipFile(io.BytesIO(blob))
    sst_root = ET.fromstring(zin.read('xl/sharedStrings.xml'))
    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    entries = list(sst_root.findall(ns + 'si'))
    old_texts = [''.join(t.text or '' for t in si.iter(ns + 't')) for si in entries]
    order = sorted(range(len(old_texts)), key=lambda i: old_texts[i], reverse=True)
    new_index = {old: new for new, old in enumerate(order)}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == 'xl/sharedStrings.xml':
                sst_root[:] = [entries[i] for i in order]
                data = ET.tostring(sst_root, encoding='utf-8')
            elif info.filename.startswith('xl/worksheets/'):
                data = _rewrite_worksheet_indices(data, new_index)
            zout.writestr(info.filename, data)
    return buffer.getvalue()


def _rewrite_worksheet_indices(data: bytes, new_index: dict) -> bytes:
    import re
    text = data.decode('utf-8')
    pattern = re.compile(r'(<c[^>]*t="s"[^>]*><v>)(\d+)(</v></c>)')
    return pattern.sub(lambda m: m.group(1) + str(new_index[int(m.group(2))]) + m.group(3),
                       text).encode('utf-8')


class RejectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def assert_rejected(self, blob_or_wb, needle=None):
        blob = blob_or_wb.build() if isinstance(blob_or_wb, Workbook) else blob_or_wb
        if needle is None:
            with self.assertRaises(WorkbookError):
                build_collection(blob)
        else:
            with self.assertRaisesRegex(WorkbookError, needle):
                build_collection(blob)

    def test_numeric_id_even_with_zero_padded_format_rejected(self):
        wb = Workbook(num_formats={164: '0000'}, style_numfmt={1: 164})
        assets = asset_sheet([
            [XCell.number(7, style=1), XCell.text('seal-A'), XCell.number(46023)],
        ])
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        self.assert_rejected(wb, 'text cell')

    def test_formula_in_required_field_rejected_even_with_cached_value(self):
        wb = Workbook()
        assets = asset_sheet([
            [XCell.formula('B2+1', 46023), XCell.text('seal-A'), XCell.number(46023)],
        ])
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        self.assert_rejected(wb, 'formula')
        wb2 = Workbook()
        faults = fault_sheet([
            [XCell.formula('"T"&1', 'T1', cached_type='str'), XCell.text('0007'),
             XCell.number(46028), XCell.text('CONFIRMED')],
        ])
        assets2 = asset_sheet([[XCell.text('0007'), XCell.text('g'), XCell.number(46023)]])
        wb2.add_sheet(assets2).add_sheet(faults)
        self.assert_rejected(wb2, 'formula')

    def test_boolean_and_error_cells_rejected(self):
        for bad_cell in (XCell.boolean(True), XCell.error('#DIV/0!')):
            wb = Workbook()
            assets = asset_sheet([[bad_cell, XCell.text('g'), XCell.number(46023)]])
            wb.add_sheet(assets).add_sheet(fault_sheet([]))
            self.assert_rejected(wb)

    def test_merged_required_cell_rejected(self):
        wb = Workbook()
        assets = asset_sheet([[XCell.text('0007'), XCell.text('g'), XCell.number(46023)]])
        assets.merge('A2:A3')
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        self.assert_rejected(wb, 'merged')

    def test_duplicate_coordinate_rejected(self):
        wb = acceptance_workbook()
        # 注入同一坐标的第二个单元格
        wb.sheets[0].put('A2', XCell.raw('<c r="A2" t="inlineStr"><is><t>dup</t></is></c>'))
        self.assert_rejected(wb, 'duplicate cell coordinate')

    def test_coordinate_row_mismatch_rejected(self):
        wb = Workbook()
        assets = Sheet('Assets')
        assets.put('A1', XCell.text('asset_id'))
        assets.put('B1', XCell.text('cohort'))
        assets.put('C1', XCell.text('installed'))
        # 坐标 A3 却放进 r=2 的行
        assets.rows.setdefault(2, synth._Row()).raw_cells.append(
            '<c r="A3" t="inlineStr"><is><t>x</t></is></c>')
        assets.put('B2', XCell.text('g'))
        assets.put('C2', XCell.number(46023))
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        self.assert_rejected(wb, 'coordinate row does not match')

    def test_duplicate_and_missing_headers(self):
        wb = Workbook()
        assets = Sheet('Assets')
        assets.put('A1', XCell.text('asset_id'))
        assets.put('B1', XCell.text('asset_id'))
        assets.put('C1', XCell.text('installed'))
        assets.put('A2', XCell.text('x'))
        assets.put('B2', XCell.text('g'))
        assets.put('C2', XCell.number(46023))
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        self.assert_rejected(wb, 'duplicate header')
        wb2 = Workbook()
        assets2 = asset_sheet([[XCell.text('x'), XCell.text('g'), XCell.number(46023)]])
        # 改表头使 cohort 缺失
        assets2.put('B1', XCell.text('batch'), replace=True)
        wb2.add_sheet(assets2).add_sheet(fault_sheet([]))
        self.assert_rejected(wb2, 'missing required header')

    def test_duplicate_asset_and_ticket_identity(self):
        wb = Workbook()
        assets = asset_sheet([
            [XCell.text('0007'), XCell.text('g'), XCell.number(46023)],
            [XCell.text('0007'), XCell.text('g'), XCell.number(46024)],
        ])
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        self.assert_rejected(wb, 'duplicate asset_id')
        wb2 = acceptance_workbook()
        wb2.sheets[-1].row(6, [XCell.text('T1'), XCell.text('0007'), XCell.number(46029),
                               XCell.text('CONFIRMED')])
        self.assert_rejected(wb2, 'duplicate ticket_id')

    def test_unknown_asset_ticket_rejected(self):
        wb = acceptance_workbook()
        wb.sheets[-1].row(6, [XCell.text('T9'), XCell.text('GHOST'), XCell.number(46029),
                              XCell.text('CONFIRMED')])
        self.assert_rejected(wb, 'unregistered asset_id')

    def test_cancelled_ticket_still_validated(self):
        wb = acceptance_workbook()
        # 取消票日期早于安装日,仍须拒绝
        wb.sheets[-1].row(6, [XCell.text('T8'), XCell.text('0007'), XCell.number(46022),
                              XCell.text('CANCELLED')])
        self.assert_rejected(wb, 'precedes installed')

    def test_whitespace_identity_rejected(self):
        wb = Workbook()
        assets = asset_sheet([[XCell.inline(' 0007'), XCell.text('g'), XCell.number(46023)]])
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        self.assert_rejected(wb, 'surrounding whitespace')

    def test_bad_status_rejected(self):
        wb = acceptance_workbook()
        wb.sheets[-1].row(6, [XCell.text('T7'), XCell.text('0007'), XCell.number(46029),
                              XCell.text('confirmed')])  # 小写
        self.assert_rejected(wb, 'CONFIRMED or CANCELLED')

    def test_extra_column_formula_does_not_break_collection(self):
        wb = acceptance_workbook()
        assets = wb.sheets[0]
        assets.put('D1', XCell.text('note'))
        assets.put('D2', XCell.formula('1+1', 2))
        assets.put('D3', XCell.formula('A3&""', '7', cached_type='str'))
        collection = build_collection(wb.build())
        self.assertEqual(len(collection['assets']), 3)

    def test_missing_required_cell_keeps_expected_coordinate_in_error(self):
        wb = Workbook()
        assets = asset_sheet([[XCell.text('0007'), XCell.text('g'), XCell.number(46023)]])
        # 第二行有 asset_id 与 installed,缺 cohort
        assets.put('A3', XCell.text('0008'))
        assets.put('C3', XCell.number(46023))
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        with self.assertRaisesRegex(WorkbookError, 'B3'):
            build_collection(wb.build())

    def test_empty_assets_rejected(self):
        wb = Workbook()
        wb.add_sheet(asset_sheet([])).add_sheet(fault_sheet([]))
        self.assert_rejected(wb, 'at least one asset')

    def test_extra_column_only_and_style_only_rows_ignored(self):
        wb = Workbook()
        assets = asset_sheet([[XCell.text('X1'), XCell.text('g'), XCell.number(46023)]])
        assets.put('D1', XCell.text('note'))
        assets.put('D3', XCell.text('remark without required fields'))
        assets.put('A5', XCell.styled_blank(0))
        assets.set_dimension('A1:D9')
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        collection = build_collection(wb.build())
        self.assertEqual(len(collection['assets']), 1)
        self.assertEqual(collection['assets'][0]['row'], 2)

    def test_audit_missing_cell_records_expected_coordinate_raw_null(self):
        # 缺失坐标的记录在错误路径上可核对:描述器生成函数给出 raw=null 与预期坐标。
        from repair_survival.workbook.adapter import _missing_descriptor
        descriptor = _missing_descriptor('B3')
        self.assertEqual(descriptor, {'ref': 'B3', 'kind': 'missing', 'raw': None})

    def test_1900_serial_60_rejected_in_real_workbook(self):
        wb = Workbook()
        assets = asset_sheet([[XCell.text('X1'), XCell.text('g'), XCell.number(60)]])
        wb.add_sheet(assets).add_sheet(fault_sheet([]))
        self.assert_rejected(wb, 'fictitious')


class PackageRejectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_non_xlsx_extension_refused_before_opening(self):
        path = self.root / 'legacy.xls'
        path.write_bytes(b'\xd0\xcf\x11\xe0fake')
        with self.assertRaisesRegex(WorkbookError, 'only ordinary OOXML'):
            load_collection(path)

    def test_corrupt_zip_and_encrypted_package(self):
        with self.assertRaisesRegex(WorkbookError, 'not a valid OOXML'):
            build_collection(b'not a zip at all')
        # 加密 xlsx 是 OLE 复合文件,不是 zip
        encrypted = self.root / 'secret.xlsx'
        encrypted.write_bytes(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'0' * 64)
        with self.assertRaisesRegex(WorkbookError, 'not a valid OOXML'):
            load_collection(encrypted)

    def test_missing_required_part(self):
        blob = acceptance_workbook().build()
        zin = zipfile.ZipFile(io.BytesIO(blob))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename == 'xl/workbook.xml':
                    continue
                zout.writestr(info.filename, zin.read(info.filename))
        with self.assertRaisesRegex(WorkbookError, 'missing package part'):
            build_collection(buffer.getvalue())

    def test_external_relationship_not_followed(self):
        blob = acceptance_workbook().build()
        zin = zipfile.ZipFile(io.BytesIO(blob))
        rels = zin.read('xl/_rels/workbook.xml.rels').decode('utf-8')
        # 把第一张工作表关系改成外部
        import re
        external = re.sub(
            r'<Relationship Id="rId1"[^>]*/>',
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="file:///etc/passwd" TargetMode="External"/>',
            rels, count=1)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = external.encode() if info.filename == 'xl/_rels/workbook.xml.rels' else zin.read(info.filename)
                zout.writestr(info.filename, data)
        with self.assertRaisesRegex(WorkbookError, 'external reference'):
            build_collection(buffer.getvalue())

    def test_sheet_located_by_name_not_order(self):
        wb = acceptance_workbook()
        wb.sheets[0].name = 'AssetsBackup'
        with self.assertRaisesRegex(WorkbookError, 'missing.*Assets'):
            build_collection(wb.build())

    def test_duplicate_required_sheet_name_rejected(self):
        wb = Workbook()
        first = asset_sheet([[XCell.text('X'), XCell.text('g'), XCell.number(46023)]], name='Assets')
        second = asset_sheet([[XCell.text('Y'), XCell.text('g'), XCell.number(46023)]], name='Assets')
        second.part = '/xl/worksheets/sheet9.xml'
        wb.add_sheet(first).add_sheet(second).add_sheet(fault_sheet([]))
        with self.assertRaisesRegex(WorkbookError, "declared more than once"):
            build_collection(wb.build())

    def test_worksheet_with_wrong_content_type_rejected(self):
        blob = acceptance_workbook().build()
        zin = zipfile.ZipFile(io.BytesIO(blob))
        content_types = zin.read('[Content_Types].xml').replace(
            b'spreadsheetml.worksheet+xml', b'application/octet-stream')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = content_types if info.filename == '[Content_Types].xml' else zin.read(info.filename)
                zout.writestr(info.filename, data)
        with self.assertRaisesRegex(WorkbookError, 'not a worksheet part'):
            build_collection(buffer.getvalue())

    def test_relationship_to_missing_part_rejected(self):
        blob = acceptance_workbook().build()
        zin = zipfile.ZipFile(io.BytesIO(blob))
        rels = zin.read('xl/_rels/workbook.xml.rels').replace(
            b'worksheets/sheet1.xml', b'worksheets/missing.xml')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = rels if info.filename == 'xl/_rels/workbook.xml.rels' else zin.read(info.filename)
                zout.writestr(info.filename, data)
        with self.assertRaisesRegex(WorkbookError, 'does not exist in the package'):
            build_collection(buffer.getvalue())

    def test_out_of_range_shared_index_rejected(self):
        blob = acceptance_workbook().build()
        zin = zipfile.ZipFile(io.BytesIO(blob))
        sheet_xml = zin.read('xl/worksheets/sheet1.xml').replace(b'<v>3</v>', b'<v>999</v>')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = sheet_xml if info.filename == 'xl/worksheets/sheet1.xml' else zin.read(info.filename)
                zout.writestr(info.filename, data)
        with self.assertRaisesRegex(WorkbookError, 'outside the string pool'):
            build_collection(buffer.getvalue())

    def test_crc_corruption_rejected(self):
        blob = bytearray(acceptance_workbook().build())
        blob[-40] ^= 0xFF
        with self.assertRaisesRegex(WorkbookError, 'CRC|zip'):
            build_collection(bytes(blob))


class PublishAndDeterminismTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'ledger.xlsx'
        self.source.write_bytes(acceptance_workbook().build())

    def test_existing_output_dir_refused(self):
        out = self.root / 'exists'
        out.mkdir()
        with self.assertRaisesRegex(WorkbookError, 'already exists'):
            collect(self.source, out)

    def test_reruns_byte_identical_and_no_runtime_fields(self):
        out1 = self.root / 'run1'
        out2 = self.root / 'run2'
        collect(self.source, out1)
        first = {p.name: p.read_bytes() for p in out1.iterdir()}
        collect(self.source, out2)
        second = {p.name: p.read_bytes() for p in out2.iterdir()}
        self.assertEqual(first, second)
        audit = json.loads(first['cells.json'])
        text = first['cells.json'].decode('utf-8')
        self.assertNotIn(str(self.root), text)
        self.assertEqual(audit['source']['sha256'],
                         __import__('hashlib').sha256(self.source.read_bytes()).hexdigest())

    def test_source_read_exactly_once(self):
        real_open = adapter.Path.open
        source_path = adapter.Path(self.source)
        calls = []

        def tracking_open(self, mode='r', *args, **kwargs):
            if 'b' in mode and self == source_path:
                calls.append(self)
            return real_open(self, mode, *args, **kwargs)

        with mock.patch.object(adapter.Path, 'open', tracking_open):
            collect(self.source, self.root / 'once')
        self.assertEqual(len(calls), 1)

    def test_injected_write_failure_leaves_no_published_or_staging(self):
        real_write = adapter.Path.write_text
        published = []

        def failing_write(self, data, *args, **kwargs):
            if self.name == 'cells.json':
                raise OSError('injected cells.json write failure')
            return real_write(self, data, *args, **kwargs)

        with mock.patch.object(adapter.Path, 'write_text', failing_write):
            with self.assertRaises(OSError):
                collect(self.source, self.root / 'broken')
        self.assertFalse((self.root / 'broken').exists())
        leftovers = [p.name for p in self.root.iterdir() if 'staging' in p.name]
        self.assertEqual(leftovers, [])
        # 源文件字节不变
        self.assertEqual(self.source.read_bytes(), acceptance_workbook().build())
        # 修正后可重跑
        collect(self.source, self.root / 'fixed')
        self.assertTrue((self.root / 'fixed' / 'assets.csv').exists())
        self.assertTrue((self.root / 'fixed' / 'cells.json').exists())

    def test_publish_failure_leaves_no_half_result(self):
        real_replace = adapter.os.replace

        def fail_replace(src, dst):
            if str(dst).endswith('late'):
                raise OSError('injected publish failure')
            return real_replace(src, dst)

        with mock.patch.object(adapter.os, 'replace', side_effect=fail_replace):
            with self.assertRaises(OSError):
                collect(self.source, self.root / 'late')
        self.assertFalse((self.root / 'late').exists())
        self.assertEqual([p.name for p in self.root.iterdir() if 'staging' in p.name], [])

    def test_no_partial_output_on_validation_error(self):
        bad_source = self.root / 'bad.xlsx'
        wb = acceptance_workbook()
        wb.sheets[-1].row(9, [XCell.text('TX'), XCell.text('UNKNOWN'), XCell.number(46029),
                              XCell.text('CONFIRMED')])
        bad_source.write_bytes(wb.build())
        with self.assertRaises(WorkbookError):
            collect(bad_source, self.root / 'should-not-exist')
        self.assertFalse((self.root / 'should-not-exist').exists())
        self.assertEqual([p.name for p in self.root.iterdir() if 'staging' in p.name], [])


class FixtureRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_legacy_fixture_round_trips_through_workbook(self):
        import csv
        with ASSET_FIX.open(newline='', encoding='utf-8') as handle:
            original = list(csv.reader(handle))
        wb = workbook_from_legacy_csv(ASSET_FIX)
        source = self.root / 'from-fixture.xlsx'
        source.write_bytes(wb.build())
        out = self.root / 'collected'
        collect(source, out)
        with (out / 'assets.csv').open(newline='', encoding='utf-8') as handle:
            collected = list(csv.reader(handle))
        # 表头完全一致;数据按 asset_id 稳定排序后逐行核对
        self.assertEqual(collected[0], original[0])
        original_rows = sorted(original[1:], key=lambda r: r[0])
        self.assertEqual(collected[1:], original_rows)

    def test_legacy_subjects_accept_collected_csv(self):
        from repair_survival.subjects import subjects
        wb = workbook_from_legacy_csv(ASSET_FIX)
        source = self.root / 'from-fixture.xlsx'
        source.write_bytes(wb.build())
        out = self.root / 'collected'
        collect(source, out)
        rows, excluded = subjects(out / 'assets.csv', '2026-01-11')
        self.assertEqual(excluded, ['F1'])
        self.assertEqual(len(rows), 6)
        ids = [r['asset_id'] for r in rows]
        self.assertEqual(ids, sorted(ids))


def workbook_from_legacy_csv(path: Path) -> Workbook:
    import csv
    with path.open(newline='', encoding='utf-8') as handle:
        rows = list(csv.reader(handle))
    wb = Workbook()
    assets = Sheet('Assets')
    assets.row(1, [XCell.text(h) for h in rows[0][:3]])
    for index, row in enumerate(rows[1:], start=2):
        assets.row(index, [XCell.text(row[0]), XCell.text(row[1]), XCell.inline(row[2])])
    # Faults 仅包含确认故障,失败日严格 ISO;空 failed 不建工单。
    faults = Sheet('Faults')
    faults.row(1, [XCell.text(h) for h in FAULT_HEADER])
    record = 2
    for row in rows[1:]:
        if row[3]:
            faults.row(record, [XCell.text('TK-' + row[0]), XCell.text(row[0]),
                                 XCell.inline(row[3]), XCell.text('CONFIRMED')])
            record += 1
    wb.add_sheet(assets).add_sheet(faults)
    return wb


class CliEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'ledger.xlsx'
        self.source.write_bytes(acceptance_workbook(add_a1_tie=True).build())

    def run_module(self, module, *args):
        # -I 隔离环境(忽略 PYTHONPATH/当前目录),用 runpy 显式注入 src,
        # 与"下载数据不可信、解释器不从数据目录加载代码"的运行约定一致。
        bootstrap = (
            "import runpy, sys; "
            "sys.path.insert(0, %r); "
            "sys.argv=[%r] + sys.argv[1:]; "
            "runpy.run_module(%r, run_name='__main__', alter_sys=True)"
        ) % (str(SRC), module, module)
        env = dict(os.environ)
        return subprocess.run([sys.executable, '-I', '-c', bootstrap, *args],
                              capture_output=True, text=True, env=env, cwd=self.root)

    def run_cli(self, *args):
        return self.run_module('repair_survival.workbook', *args)

    def test_workbook_cli_then_legacy_cli_full_chain(self):
        out_dir = self.root / 'collected'
        result = self.run_cli('--source', str(self.source), '--output-dir', str(out_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['assets'], 3)
        self.assertEqual(summary['selected_failures'], 1)
        # 旧 CLI 直接消费采集 CSV:Jan11 截止、horizon=10,三台、一次故障、RMST=25/3
        report_path = self.root / 'survival.json'
        legacy = self.run_module(
            'repair_survival.cli',
            '--source', str(out_dir / 'assets.csv'),
            '--as-of', '2026-01-11', '--horizon', '10', '--output', str(report_path))
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        report = json.loads(report_path.read_text())
        cohort = report['cohorts']['seal-A']
        self.assertEqual(cohort['subjects'], 3)
        self.assertEqual(cohort['events'], 1)
        self.assertEqual(cohort['rmst_days'], 25 / 3)
        self.assertEqual(len(report['subjects']), 3)

    def test_cli_refuses_existing_dir_and_bad_source(self):
        out_dir = self.root / 'collected'
        ok = self.run_cli('--source', str(self.source), '--output-dir', str(out_dir))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        again = self.run_cli('--source', str(self.source), '--output-dir', str(out_dir))
        self.assertNotEqual(again.returncode, 0)
        self.assertIn('already exists', again.stderr)
        missing = self.run_cli('--source', str(self.root / 'nope.xlsx'),
                               '--output-dir', str(self.root / 'x'))
        self.assertNotEqual(missing.returncode, 0)
        legacy = self.root / 'legacy.xls'
        legacy.write_bytes(b'fake')
        refused = self.run_cli('--source', str(legacy), '--output-dir', str(self.root / 'y'))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('.xlsx', refused.stderr)

    def test_cli_failure_publishes_nothing(self):
        bad = self.root / 'bad.xlsx'
        wb = acceptance_workbook()
        # 第 99 行真实数据行的 asset_id 用布尔单元格,必须拒绝且不发布目录
        wb.sheets[0].put('A99', XCell.boolean(False))
        wb.sheets[0].put('B99', XCell.text('g'))
        wb.sheets[0].put('C99', XCell.number(46023))
        bad.write_bytes(wb.build())
        target = self.root / 'no-output'
        result = self.run_cli('--source', str(bad), '--output-dir', str(target))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
