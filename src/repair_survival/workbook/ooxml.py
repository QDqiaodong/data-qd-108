"""最小化、只读的普通 OOXML ``.xlsx`` 解析(仅标准库)。

设计约束:

- 不调用 Excel/LibreOffice,不联网,不执行宏或公式;公式单元格只识别其存在。
- 调用方传入已经读入内存的源文件字节,本模块不再接触源路径。
- 通过 workbook 与关系文件按名称定位 worksheet,不假定 sheet1.xml/sheet2.xml,
  也不按工作表顺序猜角色;不跟随外部关系。
- 按真实坐标扫描单元格,稀疏缺失不左移;不使用 dimension 或样式尾行代替扫描。
"""
from __future__ import annotations

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

NS_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
NS_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
NS_PKG_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT_REL = 'http://schemas.openxmlformats.org/package/2006/relationships+xml'
CT_WORKBOOK = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'
CT_WORKSHEET = 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'
CT_SHARED = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml'


class PackageError(ValueError):
    """工作簿包结构或 OOXML 内容非法。"""


def _ln(tag: str) -> str:
    """去掉命名空间前缀,返回本地名;对没有命名空间的标签原样返回。"""
    if tag.startswith('{'):
        return tag.split('}', 1)[1]
    return tag


def _children(element, name):
    return (child for child in list(element) if _ln(child.tag) == name)


def _child(element, name):
    for child in _children(element, name):
        return child
    return None


def _parse_xml(data: bytes, what: str):
    # 显式拒绝 DTD/实体,防止外部实体与实体展开;普通 xlsx 不含 DOCTYPE。
    if re.search(rb'<!DOCTYPE\b', data[:4096], re.IGNORECASE):
        raise PackageError(what + ' contains a DOCTYPE declaration')
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise PackageError(what + ' is not well-formed XML: ' + str(exc)) from exc


_CELL_REF_RE = re.compile(r'^([A-Z]+)([1-9][0-9]*)$')


def split_ref(ref: str):
    """把 ``A1`` 形式坐标拆成(1 起行号, 0 起列号);非法坐标抛错。"""
    match = _CELL_REF_RE.match(ref)
    if not match:
        raise PackageError('invalid cell reference ' + repr(ref))
    letters, row_text = match.groups()
    column = 0
    for char in letters:
        column = column * 26 + (ord(char) - ord('A') + 1)
    return int(row_text), column - 1


def column_letter(index: int) -> str:
    """0 起列号转 Excel 列字母。"""
    index += 1
    letters = ''
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord('A') + rem) + letters
    return letters


def _norm_target(base_dir: str, target: str) -> str:
    """按 OPC 规则把关系 Target 归一为包内绝对部件名(以 / 开头)。"""
    if target.startswith('/'):
        candidate = target
    elif base_dir:
        candidate = '/' + posixpath.normpath(posixpath.join(base_dir, target))
    else:
        candidate = '/' + posixpath.normpath(target)
    parts = [part for part in candidate.split('/') if part not in ('', '.')]
    if any(part == '..' for part in parts):
        raise PackageError('relationship target escapes the package: ' + target)
    return '/' + '/'.join(parts)


@dataclass
class RichString:
    """sharedStrings/inlineStr 的解析结果。

    text 为按正文文本段顺序连接后的原文(不含注音节点与 XML 缩进);
    runs 保留每段正文,rich 标明是否来自富文本结构。
    """

    text: str
    runs: list
    rich: bool


@dataclass
class SheetRef:
    name: str
    part: str
    rid: str


@dataclass
class Cell:
    ref: str
    row: int
    column: int
    kind: str                 # shared|inline|number|boolean|error|formula_string|empty|unknown
    raw: object               # 原始令牌:文本、数字字符串或 None
    shared_index: int | None = None
    rich: bool = False


@dataclass
class ScanResult:
    part: str
    sheet_name: str
    cells: dict               # (row, column) -> Cell
    merges: list = field(default_factory=list)  # [(ref, r1, c1, r2, c2)]


@dataclass
class Package:
    file_bytes: bytes
    sha256: str
    workbook_part: str
    date1904: bool
    sheets: list              # list[SheetRef],workbook 声明顺序
    shared: list              # list[RichString | None]
    content_types: dict
    _zip: zipfile.ZipFile

    def part_bytes(self, part: str) -> bytes:
        name = part.lstrip('/')
        try:
            info = self._zip.getinfo(name)
        except KeyError as exc:
            raise PackageError('referenced part is missing: ' + part) from exc
        if info.is_dir():
            raise PackageError('referenced part is not a file: ' + part)
        return self._zip.read(info)

    def close(self):
        self._zip.close()


def _load_content_types(zf: zipfile.ZipFile) -> dict:
    try:
        root = _parse_xml(zf.read('[Content_Types].xml'), '[Content_Types].xml')
    except KeyError as exc:
        raise PackageError('missing [Content_Types].xml') from exc
    if _ln(root.tag) != 'Types':
        raise PackageError('[Content_Types].xml has unexpected root element')
    mapping = {}
    defaults = {}
    for child in list(root):
        local = _ln(child.tag)
        if local == 'Default':
            defaults[child.get('Extension', '').lower()] = child.get('Content-Type', '')
        elif local == 'Override':
            mapping[child.get('PartName', '')] = child.get('ContentType', '')
    for info in zf.infolist():
        if info.is_dir() or info.filename == '[Content_Types].xml':
            continue
        part_name = '/' + info.filename
        if part_name not in mapping:
            ext = info.filename.rsplit('.', 1)[-1].lower() if '.' in info.filename else ''
            ct = defaults.get(ext)
            if ct:
                mapping[part_name] = ct
    return mapping


def _rels_path(part: str) -> str:
    """绝对部件名对应的关系部件名,如 /xl/workbook.xml -> /xl/_rels/workbook.xml.rels。"""
    if part == '/_rels/.rels':
        return '/_rels/.rels'
    part = part.lstrip('/')
    directory, base = posixpath.split(part)
    return ('/' + posixpath.join(directory, '_rels', base + '.rels')) if directory else '/_rels/' + base + '.rels'


def _load_rels(zf: zipfile.ZipFile, part: str, what: str, base_dir: str | None = None) -> dict:
    rel_path = _rels_path(part).lstrip('/')
    try:
        root = _parse_xml(zf.read(rel_path), rel_path)
    except KeyError as exc:
        raise PackageError(what + ' is missing its relationship part ' + rel_path) from exc
    rels = {}
    if _ln(root.tag) != 'Relationships':
        raise PackageError(rel_path + ' has unexpected root element')
    if base_dir is None:
        base_dir = posixpath.dirname(part.lstrip('/'))
    for rel in _children(root, 'Relationship'):
        rid = rel.get('Id')
        target = rel.get('Target')
        mode = rel.get('TargetMode')
        if not rid or target is None:
            raise PackageError(rel_path + ' contains a malformed relationship')
        if mode == 'External':
            # 外部关系不参与采集,记录但绝不解析其目标。
            rels[rid] = {'external': True, 'type': rel.get('Type', ''), 'target': target}
            continue
        if rid in rels:
            raise PackageError(rel_path + ' has duplicate relationship id ' + rid)
        rels[rid] = {
            'external': False,
            'type': rel.get('Type', ''),
            'target': _norm_target(base_dir, target),
        }
    return rels


def _text_from_rich(parent) -> RichString:
    """连接 si/inlineStr 内的正文文本段。

    只取 ``t`` 元素的直接文本;``rPh``(注音)、``phoneticPr`` 等节点忽略,
    XML 缩进与元素间空白不会进入结果。
    """
    runs = []
    rich = False
    for node in list(parent):
        local = _ln(node.tag)
        if local == 'r':
            rich = True
            pieces = []
            for sub in _children(node, 't'):
                pieces.append(sub.text or '')
            runs.append(''.join(pieces))
        elif local == 't':
            runs.append(node.text or '')
        # rPh / phoneticPr / rPr 等一律不参与正文
    return RichString(text=''.join(runs), runs=runs, rich=rich)


def _load_shared_strings(zf: zipfile.ZipFile, part: str):
    try:
        root = _parse_xml(zf.read(part.lstrip('/')), part)
    except KeyError as exc:
        raise PackageError('declared shared string part is missing: ' + part) from exc
    if _ln(root.tag) != 'sst':
        raise PackageError(part + ' has unexpected root element')
    strings = []
    for item in _children(root, 'si'):
        strings.append(_text_from_rich(item))
    return strings


def open_package(file_bytes: bytes, sha256: str) -> Package:
    """从已读入内存的字节打开并校验普通 xlsx 包,返回 :class:`Package`。"""
    if not zipfile.is_zipfile(io.BytesIO(file_bytes)):
        raise PackageError('source is not a valid OOXML .xlsx package (not a zip container)')
    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile as exc:
        raise PackageError('corrupt xlsx package: ' + str(exc)) from exc
    if zf.testzip() is not None:
        zf.close()
        raise PackageError('corrupt xlsx package: CRC failure in package entry')
    names = set(zf.namelist())
    required = {'[Content_Types].xml', '_rels/.rels', 'xl/workbook.xml'}
    missing = required - names
    if missing:
        zf.close()
        raise PackageError('not an xlsx workbook: missing package part(s) ' + ', '.join(sorted(missing)))
    content_types = _load_content_types(zf)
    macro_parts = [name for name in names
                   if content_types.get('/' + name, '').endswith('sheet.macroEnabled.main+xml')
                   or content_types.get('/' + name, '') == 'application/vnd.ms-excel.sheet.macroEnabled+xml']
    if macro_parts:
        zf.close()
        raise PackageError('macro-enabled workbooks are not supported')
    root_rels = _load_rels(zf, '/_rels/.rels', 'package root', base_dir='')
    workbook_part = None
    for rel in root_rels.values():
        if not rel['external'] and rel['type'].endswith('/officeDocument'):
            if workbook_part is not None:
                zf.close()
                raise PackageError('multiple office document relationships found')
            workbook_part = rel['target']
    if workbook_part is None:
        zf.close()
        raise PackageError('no office document relationship found in package root')
    if content_types.get(workbook_part) != CT_WORKBOOK:
        zf.close()
        raise PackageError('workbook part has an unexpected content type: ' + workbook_part)
    wb_root = _parse_xml(zf.read(workbook_part.lstrip('/')), workbook_part)
    if _ln(wb_root.tag) != 'workbook':
        zf.close()
        raise PackageError(workbook_part + ' has unexpected root element')
    date1904 = False
    wb_pr = _child(wb_root, 'workbookPr')
    if wb_pr is not None and wb_pr.get('date1904') in ('1', 'true', 'True'):
        date1904 = True
    sheets_node = _child(wb_root, 'sheets')
    if sheets_node is None or not list(_children(sheets_node, 'sheet')):
        zf.close()
        raise PackageError('workbook declares no worksheets')
    wb_rels = _load_rels(zf, workbook_part, workbook_part)
    sheets = []
    seen_parts = set()
    for sheet_node in _children(sheets_node, 'sheet'):
        name = sheet_node.get('name')
        rid = sheet_node.get('{%s}id' % NS_REL)
        if not name or not rid:
            zf.close()
            raise PackageError('workbook contains a sheet declaration without name or relationship id')
        if rid not in wb_rels:
            zf.close()
            raise PackageError('sheet %r references an unknown relationship %s' % (name, rid))
        rel = wb_rels[rid]
        if rel['external']:
            zf.close()
            raise PackageError('sheet %r is an external reference and cannot be collected' % name)
        part = rel['target']
        if part in seen_parts:
            zf.close()
            raise PackageError('relationship %s resolves to worksheet part %s more than once' % (rid, part))
        seen_parts.add(part)
        if part.lstrip('/') not in names:
            zf.close()
            raise PackageError('worksheet part %s referenced by sheet %r does not exist in the package' % (part, name))
        if content_types.get(part) != CT_WORKSHEET:
            zf.close()
            raise PackageError('sheet %r target %s is not a worksheet part' % (name, part))
        sheets.append(SheetRef(name=name, part=part, rid=rid))
    shared = []
    for rel in wb_rels.values():
        if not rel['external'] and rel['type'].endswith('/sharedStrings'):
            part = rel['target']
            if part.lstrip('/') not in names:
                zf.close()
                raise PackageError('declared shared strings part does not exist: ' + part)
            if content_types.get(part) not in (CT_SHARED, ''):
                zf.close()
                raise PackageError('shared strings part has an unexpected content type: ' + part)
            shared = _load_shared_strings(zf, part)
            break
    return Package(
        file_bytes=file_bytes,
        sha256=sha256,
        workbook_part=workbook_part,
        date1904=date1904,
        sheets=sheets,
        shared=shared,
        content_types=content_types,
        _zip=zf,
    )


def _merge_bounds(ref: str):
    if ':' not in ref:
        first = last = ref
    else:
        first, last = ref.split(':', 1)
    r1, c1 = split_ref(first)
    r2, c2 = split_ref(last)
    if (r2, c2) < (r1, c1):
        raise PackageError('invalid merged cell range ' + ref)
    return ref, r1, c1, r2, c2


def scan_worksheet(pkg: Package, sheet: SheetRef) -> ScanResult:
    """扫描一张 worksheet 的全部真实单元格与合并区域。"""
    root = _parse_xml(pkg.part_bytes(sheet.part), sheet.part)
    if _ln(root.tag) != 'worksheet':
        raise PackageError(sheet.part + ' has unexpected root element')
    sheet_data = _child(root, 'sheetData')
    cells = {}
    if sheet_data is not None:
        for row_node in _children(sheet_data, 'row'):
            row_attr = row_node.get('r')
            if row_attr is not None and not row_attr.isdigit():
                raise PackageError('%s contains a row with a non-numeric r attribute: %r'
                                   % (sheet.part, row_attr))
            for cell_node in _children(row_node, 'c'):
                ref = cell_node.get('r')
                if not ref:
                    raise PackageError('%s contains a cell without an r coordinate' % sheet.part)
                try:
                    row_no, col_no = split_ref(ref)
                except PackageError as exc:
                    raise PackageError('%s: %s' % (sheet.part, exc)) from exc
                if row_attr is not None and int(row_attr) != row_no:
                    raise PackageError(
                        '%s cell %s is inside row %s, coordinate row does not match'
                        % (sheet.part, ref, row_attr))
                if (row_no, col_no) in cells:
                    raise PackageError('%s contains duplicate cell coordinate %s' % (sheet.part, ref))
                cells[(row_no, col_no)] = _read_cell(pkg, sheet, cell_node, ref, row_no, col_no)
    merges = []
    merge_root = _child(root, 'mergeCells')
    if merge_root is not None:
        for merge in _children(merge_root, 'mergeCell'):
            ref = merge.get('ref')
            if not ref:
                raise PackageError('%s contains a mergeCell without a ref' % sheet.part)
            try:
                merges.append(_merge_bounds(ref))
            except PackageError as exc:
                raise PackageError('%s: %s' % (sheet.part, exc)) from exc
    return ScanResult(part=sheet.part, sheet_name=sheet.name, cells=cells, merges=merges)


def _read_cell(pkg: Package, sheet: SheetRef, node, ref: str, row_no: int, col_no: int) -> Cell:
    cell_type = node.get('t')
    formula = _child(node, 'f') is not None
    value_node = _child(node, 'v')
    inline_node = _child(node, 'is')
    if formula:
        kind = 'formula_string' if cell_type == 'str' else 'formula'
        raw = value_node.text if value_node is not None else None
        return Cell(ref, row_no, col_no, kind, raw)
    if cell_type is None or cell_type == 'n':
        if value_node is None:
            return Cell(ref, row_no, col_no, 'empty', None)
        return Cell(ref, row_no, col_no, 'number', value_node.text)
    if cell_type == 's':
        if value_node is None or value_node.text is None:
            raise PackageError('%s cell %s has shared string type but no index' % (sheet.part, ref))
        token = value_node.text
        if not token or not token.lstrip('-').isdigit():
            raise PackageError('%s cell %s has an invalid shared string index' % (sheet.part, ref))
        index = int(token)
        if index < 0 or not 0 <= index < len(pkg.shared):
            raise PackageError(
                '%s cell %s shared string index %s is outside the string pool (size %d)'
                % (sheet.part, ref, token, len(pkg.shared)))
        rich_string = pkg.shared[index]
        return Cell(ref, row_no, col_no, 'shared', rich_string.text, shared_index=index,
                    rich=rich_string.rich)
    if cell_type == 'inlineStr':
        if inline_node is None:
            raise PackageError('%s cell %s is an inlineStr without an is element' % (sheet.part, ref))
        rich_string = _text_from_rich(inline_node)
        return Cell(ref, row_no, col_no, 'inline', rich_string.text, rich=rich_string.rich)
    if cell_type == 'b':
        return Cell(ref, row_no, col_no, 'boolean', value_node.text if value_node is not None else None)
    if cell_type == 'e':
        return Cell(ref, row_no, col_no, 'error', value_node.text if value_node is not None else None)
    if cell_type == 'str':
        # 无 f 的缓存字符串类型不是合法的普通台账单元格。
        return Cell(ref, row_no, col_no, 'unknown', value_node.text if value_node is not None else None)
    return Cell(ref, row_no, col_no, 'unknown', value_node.text if value_node is not None else None)
