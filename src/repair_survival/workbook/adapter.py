"""把 Assets/Faults 两张清册采集为原四列输入与逐单元格审计。

本模块只做上游采集与关联校验:不计算生存曲线、竞争风险、RMST 或置信区间,
不接受 as-of 参数,也不因"未来"日期过滤台账。输出目录原先必须不存在;
两份产物先写入目标同目录的私有暂存目录,全部成功后一次原子发布。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path

from . import dates as date_rules
from .ooxml import PackageError, column_letter, open_package, scan_worksheet

SCHEMA_VERSION = 1
MODE = 'workbook_collection'
ASSET_SHEET = 'Assets'
FAULT_SHEET = 'Faults'
ASSET_FIELDS = ('asset_id', 'cohort', 'installed')
FAULT_FIELDS = ('ticket_id', 'asset_id', 'failed_on', 'status')
TEXT_FIELDS = {'asset_id', 'ticket_id', 'cohort', 'status'}
STATUSES = ('CONFIRMED', 'CANCELLED')
CSV_HEADER = ('asset_id', 'cohort', 'installed', 'failed')


class WorkbookError(ValueError):
    """输入、OOXML 结构、字段契约或关联校验失败。"""


def _where(sheet_name: str, ref: str | None = None) -> str:
    text = 'worksheet ' + sheet_name
    if ref:
        text += ' cell ' + ref
    return text


def _cell_descriptor(cell):
    """生成可复核的原始单元格令牌描述;缺失单元格给 raw=null。"""
    if cell is None:
        return None
    descriptor = {'ref': cell.ref, 'kind': cell.kind, 'raw': cell.raw}
    if cell.kind == 'shared':
        descriptor['shared_index'] = cell.shared_index
        descriptor['rich'] = cell.rich
    elif cell.kind == 'inline':
        descriptor['rich'] = cell.rich
    return descriptor


def _missing_descriptor(ref: str):
    return {'ref': ref, 'kind': 'missing', 'raw': None}


def _header_map(sheet_ref, scan, required):
    """按第 1 行表头文本(而非列号)建立表头→列号映射。"""
    header_cells = [cell for (row, _col), cell in scan.cells.items() if row == 1]
    mapping = {}
    headers = []
    for cell in sorted(header_cells, key=lambda c: c.column):
        if cell.kind == 'empty' or cell.raw is None:
            continue
        if cell.kind not in ('shared', 'inline'):
            raise WorkbookError(
                '%s header cell %s must be text, got %s' % (_where(sheet_ref.name), cell.ref, cell.kind))
        text = cell.raw
        if not text:
            continue
        if text in mapping:
            raise WorkbookError(
                '%s has duplicate header %r at row 1' % (sheet_ref.name, text))
        mapping[text] = cell.column
        headers.append({'header': text, 'column': column_letter(cell.column),
                        'column_index': cell.column, 'cell': _cell_descriptor(cell)})
    missing = [name for name in required if name not in mapping]
    if missing:
        raise WorkbookError(
            'worksheet %s is missing required header(s): %s' % (sheet_ref.name, ', '.join(missing)))
    return mapping, headers


def _check_merges(sheet_name, scan, required_columns):
    """拒绝实际使用区域内覆盖任一必需列的合并单元格。"""
    if not scan.cells:
        return
    used_rows = [row for (row, _col) in scan.cells]
    row_lo, row_hi = min(used_rows), max(used_rows)
    for ref, r1, c1, r2, c2 in scan.merges:
        if r2 < row_lo or r1 > row_hi:
            continue
        for field_name, col in required_columns:
            if c1 <= col <= c2:
                raise WorkbookError(
                    'worksheet %s merged range %s covers required column %s'
                    % (sheet_name, ref, field_name))


def _text_value(sheet_name, field_name, cell, expected_ref):
    if cell is None or cell.kind == 'empty':
        raise WorkbookError(
            '%s has an empty required text field %s at expected cell %s'
            % (sheet_name, field_name, expected_ref))
    if cell.kind in ('formula', 'formula_string'):
        raise WorkbookError(
            '%s field %s contains a formula; cached values are not accepted'
            % (_where(sheet_name, cell.ref), field_name))
    if cell.kind not in ('shared', 'inline'):
        raise WorkbookError(
            '%s field %s requires a text cell, got %s (display formatting is not identity)'
            % (_where(sheet_name, cell.ref), field_name, cell.kind))
    text = cell.raw
    if not text or text != text.strip():
        raise WorkbookError(
            '%s field %s must be non-empty without surrounding whitespace'
            % (_where(sheet_name, cell.ref), field_name))
    return text


def _date_value(sheet_name, field_name, cell, expected_ref, date1904):
    if cell is None or cell.kind == 'empty':
        raise WorkbookError(
            'worksheet %s has an empty required date field %s at expected cell %s'
            % (sheet_name, field_name, expected_ref))
    if cell.kind in ('formula', 'formula_string'):
        raise WorkbookError(
            '%s field %s contains a formula; cached values are not accepted'
            % (_where(sheet_name, cell.ref), field_name))
    if cell.kind in ('shared', 'inline'):
        try:
            parsed = date_rules.strict_iso_text(cell.raw)
        except ValueError as exc:
            raise WorkbookError('%s field %s: %s' % (_where(sheet_name, cell.ref), field_name, exc)) from exc
        return parsed, 'iso_text'
    if cell.kind == 'number':
        try:
            serial = date_rules.integer_token(cell.raw)
            parsed = date_rules.serial_to_date(serial, date1904)
            date_rules.date_in_range(parsed)
        except ValueError as exc:
            raise WorkbookError('%s field %s: %s' % (_where(sheet_name, cell.ref), field_name, exc)) from exc
        return parsed, 'excel_serial'
    raise WorkbookError(
        '%s field %s requires an ISO text cell or an integer Excel serial, got %s'
        % (_where(sheet_name, cell.ref), field_name, cell.kind))


def _field_entry(sheet_name, field_name, scan, row, column, date1904):
    ref = '%s%d' % (column_letter(column), row)
    cell = scan.cells.get((row, column))
    descriptor = _cell_descriptor(cell) if cell is not None else _missing_descriptor(ref)
    if field_name in TEXT_FIELDS:
        value = _text_value(sheet_name, field_name, cell, ref)
        return {'field': field_name, 'cell': descriptor, 'type': 'text', 'value': value}
    parsed, source_kind = _date_value(sheet_name, field_name, cell, ref, date1904)
    return {'field': field_name, 'cell': descriptor, 'type': 'date',
            'value': parsed.isoformat(), 'serial_source': source_kind}


def _has_any_content(scan, row, columns):
    for column in columns:
        cell = scan.cells.get((row, column))
        if cell is not None and cell.kind != 'empty' and cell.raw is not None and cell.raw != '':
            return True
    return False


def _collect_sheet(pkg, sheet_ref, field_names, date1904):
    scan = scan_worksheet(pkg, sheet_ref)
    mapping, headers = _header_map(sheet_ref, scan, field_names)
    ordered_columns = [(name, mapping[name]) for name in field_names]
    _check_merges(sheet_ref.name, scan, ordered_columns)
    required_column_set = [column for _name, column in ordered_columns]
    data_rows = sorted({row for (row, _col) in scan.cells if row >= 2})
    records = []
    record_no = 0
    for row in data_rows:
        if not _has_any_content(scan, row, required_column_set):
            # 只有样式、批注或额外列内容的行不创造记录。
            continue
        record_no += 1
        fields = [
            _field_entry(sheet_ref.name, name, scan, row, column, date1904)
            for name, column in ordered_columns
        ]
        records.append({'record': record_no, 'sheet': sheet_ref.name, 'row': row,
                        'part': sheet_ref.part, 'fields': fields})
    return records, headers, mapping, scan


def _value_map(record):
    return {entry['field']: entry['value'] for entry in record['fields']}


def build_collection(file_bytes: bytes) -> dict:
    """从已读入内存的 xlsx 字节完成全部解析、校验与工作簿摘要。"""
    digest = hashlib.sha256(file_bytes).hexdigest()
    try:
        pkg = open_package(file_bytes, digest)
    except PackageError as exc:
        raise WorkbookError(str(exc)) from exc
    try:
        by_name = {}
        for sheet_ref in pkg.sheets:
            if sheet_ref.name in (ASSET_SHEET, FAULT_SHEET):
                if sheet_ref.name in by_name:
                    raise WorkbookError('worksheet named %r is declared more than once' % sheet_ref.name)
                by_name[sheet_ref.name] = sheet_ref
        missing_sheets = [name for name in (ASSET_SHEET, FAULT_SHEET) if name not in by_name]
        if missing_sheets:
            raise WorkbookError(
                'workbook must contain worksheets exactly named: %s (missing %s)'
                % (', '.join((ASSET_SHEET, FAULT_SHEET)), ', '.join(missing_sheets)))
        date1904 = pkg.date1904
        asset_records, asset_headers, _, _ = _collect_sheet(
            pkg, by_name[ASSET_SHEET], ASSET_FIELDS, date1904)
        fault_records, fault_headers, _, _ = _collect_sheet(
            pkg, by_name[FAULT_SHEET], FAULT_FIELDS, date1904)
        if not asset_records:
            raise WorkbookError('worksheet %s must contain at least one asset data record' % ASSET_SHEET)

        assets = []
        asset_ids = set()
        installed_by_id = {}
        for record in asset_records:
            values = _value_map(record)
            asset_id = values['asset_id']
            if asset_id in asset_ids:
                raise WorkbookError(
                    '%s duplicate asset_id %r (text 0007 and 7 are different identities)'
                    % (_where(ASSET_SHEET), asset_id))
            asset_ids.add(asset_id)
            installed_by_id[asset_id] = date.fromisoformat(values['installed'])
            assets.append({
                'record': record['record'], 'sheet': record['sheet'], 'row': record['row'],
                'asset_id': asset_id, 'cohort': values['cohort'],
                'installed': values['installed'], 'fields': record['fields'],
                'candidate_confirmed_tickets': [], 'ticket_dispositions': [],
                'selected_ticket_id': None,
            })

        tickets = []
        ticket_ids = set()
        for record in fault_records:
            values = _value_map(record)
            ticket_id = values['ticket_id']
            if ticket_id in ticket_ids:
                raise WorkbookError(
                    '%s duplicate ticket_id %r even if content is identical'
                    % (_where(FAULT_SHEET), ticket_id))
            ticket_ids.add(ticket_id)
            asset_id = values['asset_id']
            if asset_id not in asset_ids:
                raise WorkbookError(
                    '%s ticket %r references unregistered asset_id %r'
                    % (_where(FAULT_SHEET), ticket_id, asset_id))
            failed_on = date.fromisoformat(values['failed_on'])
            if failed_on < installed_by_id[asset_id]:
                raise WorkbookError(
                    '%s ticket %r failed_on %s precedes installed %s of asset %r'
                    % (_where(FAULT_SHEET), ticket_id, failed_on.isoformat(),
                       installed_by_id[asset_id].isoformat(), asset_id))
            status = values['status']
            if status not in STATUSES:
                raise WorkbookError(
                    '%s ticket %r status must be exactly CONFIRMED or CANCELLED, got %r'
                    % (_where(FAULT_SHEET), ticket_id, status))
            tickets.append({
                'record': record['record'], 'sheet': record['sheet'], 'row': record['row'],
                'ticket_id': ticket_id, 'asset_id': asset_id,
                'failed_on': failed_on.isoformat(), 'status': status,
                'fields': record['fields'],
            })

        by_asset = {asset['asset_id']: asset for asset in assets}
        chosen_by_asset = {}
        for asset in assets:
            confirmed = [t for t in tickets
                         if t['asset_id'] == asset['asset_id'] and t['status'] == 'CONFIRMED']
            if confirmed:
                ordered = sorted(confirmed, key=lambda t: (date.fromisoformat(t['failed_on']),
                                                           t['ticket_id']))
                chosen = ordered[0]
                asset['selected_ticket_id'] = chosen['ticket_id']
                asset['candidate_confirmed_tickets'] = [t['ticket_id'] for t in ordered]
                chosen_by_asset[asset['asset_id']] = chosen
        for ticket in tickets:
            asset = by_asset[ticket['asset_id']]
            chosen = chosen_by_asset.get(asset['asset_id'])
            if ticket['status'] == 'CANCELLED':
                reason = 'cancelled_not_eligible'
                selected = False
            elif chosen is not None and ticket['ticket_id'] == chosen['ticket_id']:
                reason = 'selected_earliest_confirmed'
                selected = True
            elif chosen is not None and ticket['failed_on'] == chosen['failed_on']:
                reason = 'confirmed_same_date_lost_ticket_id_order'
                selected = False
            else:
                reason = 'confirmed_later_than_selected'
                selected = False
            ticket['disposition'] = {'selected': selected, 'reason': reason}
            asset['ticket_dispositions'].append(
                {'ticket_id': ticket['ticket_id'], 'selected': selected, 'reason': reason})

        sheet_roles = []
        collected_parts = {by_name[ASSET_SHEET].part, by_name[FAULT_SHEET].part}
        for sheet_ref in pkg.sheets:
            if sheet_ref.name == ASSET_SHEET:
                role = 'assets'
            elif sheet_ref.name == FAULT_SHEET:
                role = 'faults'
            else:
                role = 'ignored'
            sheet_roles.append({'name': sheet_ref.name, 'part': sheet_ref.part,
                                'relationship_id': sheet_ref.rid, 'role': role})
        assets_sorted = sorted(assets, key=lambda item: item['asset_id'])
        collection = {
            'schema_version': SCHEMA_VERSION,
            'mode': MODE,
            'source': {'sha256': digest, 'byte_size': len(file_bytes)},
            'workbook': {
                'date_system': '1904' if date1904 else '1900',
                'date1904': date1904,
                'workbook_part': pkg.workbook_part,
                'shared_string_count': len(pkg.shared),
                'sheets': sheet_roles,
                'collected_parts': sorted(collected_parts),
            },
            'header_maps': {
                ASSET_SHEET: {'sheet': ASSET_SHEET, 'part': by_name[ASSET_SHEET].part,
                              'header_row': 1, 'columns': asset_headers},
                FAULT_SHEET: {'sheet': FAULT_SHEET, 'part': by_name[FAULT_SHEET].part,
                              'header_row': 1, 'columns': fault_headers},
            },
            'assets': assets_sorted,
            'faults': tickets,
            'csv': {'header': list(CSV_HEADER), 'row_count': len(assets_sorted)},
        }
        return collection
    except PackageError as exc:
        raise WorkbookError(str(exc)) from exc
    finally:
        pkg.close()


def load_collection(source) -> dict:
    """读取一次源文件字节后完成全部解析;后续不再从源路径打开文件。"""
    path = Path(source)
    if path.suffix.lower() != '.xlsx':
        raise WorkbookError('only ordinary OOXML .xlsx workbooks are supported; refuse %s' % path.suffix)
    with path.open('rb') as handle:
        file_bytes = handle.read()
    return build_collection(file_bytes)


def _csv_text(collection: dict) -> str:
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADER)
    failed_by_asset = {
        ticket['asset_id']: ticket['failed_on']
        for ticket in collection['faults']
        if ticket['disposition']['selected']
    }
    for asset in collection['assets']:
        writer.writerow([
            asset['asset_id'], asset['cohort'], asset['installed'],
            failed_by_asset.get(asset['asset_id'], ''),
        ])
    return buffer.getvalue()


def _json_text(collection: dict) -> str:
    return json.dumps(collection, indent=2, sort_keys=True, ensure_ascii=False,
                      allow_nan=False) + '\n'


def collect(source, output_dir) -> dict:
    """采集工作簿并原子发布 output_dir;返回采集 dict。"""
    target = Path(output_dir)
    if target.exists() or target.is_symlink():
        raise WorkbookError('output directory already exists, refusing to overwrite: %s' % target)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    collection = load_collection(source)
    staging = Path(tempfile.mkdtemp(prefix='.' + target.name + '.staging-', dir=parent))
    try:
        csv_path = staging / 'assets.csv'
        audit_path = staging / 'cells.json'
        csv_path.write_text(_csv_text(collection), encoding='utf-8', newline='')
        audit_path.write_text(_json_text(collection), encoding='utf-8', newline='')
        os.replace(staging, target)
    except BaseException:
        # 暂存目录在目标同目录;任何失败都清理本次暂存,绝不留下半套结果。
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return collection
