#!/usr/bin/env python3
"""可重复生成原创合成 OOXML .xlsx 台账的命令行脚本(仅标准库)。

生成的全部是确定性合成数据,不含真实设备台账或凭据;同一参数重复运行
输出字节一致。脚本只依赖 repair_survival.workbook.synth,不调用
Excel/LibreOffice,不联网,不执行宏或公式。

示例::

    python3 scripts/generate_workbook_fixtures.py --output-dir run/workbooks
    python3 scripts/generate_workbook_fixtures.py --output-dir run/wb --kind acceptance_1904
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from repair_survival.workbook.synth import Sheet, Workbook, XCell  # noqa: E402

ASSET_HEADER = ('asset_id', 'cohort', 'installed')
FAULT_HEADER = ('ticket_id', 'asset_id', 'failed_on', 'status')


def _asset_sheet(rows, name='Assets'):
    sheet = Sheet(name)
    sheet.row(1, [XCell.text(h) for h in ASSET_HEADER])
    for index, values in enumerate(rows, start=2):
        sheet.row(index, values)
    return sheet


def _fault_sheet(rows, name='Faults'):
    sheet = Sheet(name)
    sheet.row(1, [XCell.text(h) for h in FAULT_HEADER])
    for index, values in enumerate(rows, start=2):
        sheet.row(index, values)
    return sheet


def acceptance_workbook(date1904=False, tie=False):
    """验收台账:0007/7/B 同 cohort、同日安装;0007 含确认票、同日并列票与取消票。"""
    if date1904:
        jan1, jan2, jan6, jan8 = 44561, 44562, 44566, 44568
    else:
        jan1, jan2, jan6, jan8 = 46023, 46024, 46028, 46030
    wb = Workbook(date1904=date1904)
    assets = _asset_sheet([
        [XCell.text('0007'), XCell.text('seal-A'), XCell.number(jan1)],
        [XCell.text('7'), XCell.text('seal-A'), XCell.number(jan1)],
        [XCell.text('B'), XCell.text('seal-A'), XCell.number(jan1)],
    ])
    fault_rows = [
        [XCell.text('T2'), XCell.text('0007'), XCell.number(jan8), XCell.text('CONFIRMED')],
        [XCell.text('T1'), XCell.text('0007'), XCell.number(jan6), XCell.text('CONFIRMED')],
        [XCell.text('T0'), XCell.text('0007'), XCell.number(jan2), XCell.text('CANCELLED')],
    ]
    if tie:
        fault_rows.append(
            [XCell.text('A1'), XCell.text('0007'), XCell.number(jan6), XCell.text('CONFIRMED')])
    faults = _fault_sheet(fault_rows)
    wb.add_sheet(assets).add_sheet(faults)
    return wb


def reordered_workbook():
    """表头换列、行号留空档、隐藏行、远处仅样式单元格的结构反例。"""
    wb = Workbook()
    assets = Sheet('Assets')
    assets.put('A1', XCell.text('installed'))
    assets.put('C1', XCell.text('asset_id'))
    assets.put('E1', XCell.text('cohort'))
    assets.put('A2', XCell.number(46023))
    assets.put('C2', XCell.text('0007'))
    assets.put('E2', XCell.text('seal-A'))
    # 第 4 行(跳过第 3 行),且该行隐藏——隐藏行仍是事实
    assets.put('A4', XCell.number(46023))
    assets.put('C4', XCell.text('7'))
    assets.put('E4', XCell.text('seal-A'))
    assets.hide_row(4)
    assets.put('Z88', XCell.styled_blank(0))
    assets.set_dimension('A1:Z88')
    faults = Sheet('Faults')
    faults.put('B1', XCell.text('ticket_id'))
    faults.put('D1', XCell.text('asset_id'))
    faults.put('F1', XCell.text('failed_on'))
    faults.put('H1', XCell.text('status'))
    faults.put('B2', XCell.text('T1'))
    faults.put('D2', XCell.text('0007'))
    faults.put('F2', XCell.number(46028))
    faults.put('H2', XCell.text('CONFIRMED'))
    wb.add_sheet(assets).add_sheet(faults)
    return wb


BUILDERS = {
    'acceptance_1900': lambda: acceptance_workbook(False, tie=False),
    'acceptance_1900_tie': lambda: acceptance_workbook(False, tie=True),
    'acceptance_1904': lambda: acceptance_workbook(True, tie=False),
    'reordered': reordered_workbook,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog='generate_workbook_fixtures',
        description='Generate deterministic synthetic OOXML .xlsx maintenance ledgers')
    parser.add_argument('--output-dir', required=True,
                        help='directory for generated workbooks (created if absent)')
    parser.add_argument('--kind', choices=sorted(BUILDERS),
                        help='generate only one workbook kind; default generates all')
    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    kinds = [args.kind] if args.kind else sorted(BUILDERS)
    for kind in kinds:
        target = output_dir / (kind + '.xlsx')
        target.write_bytes(BUILDERS[kind]().build())
        print('%s -> %s (%d bytes)' % (kind, target, target.stat().st_size))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
