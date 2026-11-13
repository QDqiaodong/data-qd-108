"""确定性合成 OOXML ``.xlsx`` 生成器(仅标准库,仅供测试与生成脚本)。

用途:可重复生成原创的合成工作簿,覆盖真实包结构里的列序交换、稀疏单元格、
隐藏行、富文本、sharedStrings 重排、worksheet 部件对调等情形。它不是采集
代码的一部分,绝不参与对台账的解析;所有输出字节确定,不含时间戳随机量。
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from .ooxml import column_letter, split_ref

ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


@dataclass
class RichText:
    """共享/内联富文本;phonetic 写入 rPh,解析时必须被忽略。"""

    runs: list
    phonetic: str | None = None


@dataclass
class XCell:
    kind: str
    value: object = None
    style: int | None = None
    formula: str | None = None
    cached_type: str | None = None
    runs: list | None = None
    phonetic: str | None = None
    shared_index: int | None = None

    @classmethod
    def text(cls, value, style=None):
        return cls('auto_text', value=value, style=style)

    @classmethod
    def shared(cls, index, style=None):
        return cls('shared', shared_index=index, style=style)

    @classmethod
    def inline(cls, value, style=None):
        return cls('inline', value=value, style=style)

    @classmethod
    def rich_inline(cls, runs, phonetic=None, style=None):
        return cls('inline_rich', runs=list(runs), phonetic=phonetic, style=style)

    @classmethod
    def rich_shared(cls, index, runs, phonetic=None, style=None):
        return cls('shared_rich', shared_index=index, runs=list(runs),
                   phonetic=phonetic, style=style)

    @classmethod
    def number(cls, token, style=None):
        return cls('number', value=str(token), style=style)

    @classmethod
    def boolean(cls, value, style=None):
        return cls('boolean', value='1' if value else '0', style=style)

    @classmethod
    def error(cls, token, style=None):
        return cls('error', value=token, style=style)

    @classmethod
    def formula(cls, formula, cached, cached_type='n', style=None):
        return cls('formula', value=str(cached), formula=formula,
                   cached_type=cached_type, style=style)

    @classmethod
    def styled_blank(cls, style):
        return cls('styled_blank', style=style)

    @classmethod
    def raw(cls, xml):
        return cls('raw', value=xml)


@dataclass
class _Row:
    hidden: bool = False
    cells: dict = field(default_factory=dict)
    raw_cells: list = field(default_factory=list)


class Sheet:
    def __init__(self, name, part=None, dimension=None):
        self.name = name
        self.part = part
        self.dimension = dimension
        self.rows = {}
        self.merges = []

    def put(self, ref, cell: XCell, replace: bool = False) -> 'Sheet':
        row_no, col_no = split_ref(ref)
        if cell.kind == 'raw':
            self.rows.setdefault(row_no, _Row()).raw_cells.append(cell.value)
            return self
        row = self.rows.setdefault(row_no, _Row())
        if col_no in row.cells and not replace:
            raise ValueError('duplicate cell %s in synthetic sheet %r' % (ref, self.name))
        row.cells[col_no] = cell
        return self

    def row(self, row_no, values, hidden=False, start_column=0) -> 'Sheet':
        """values 为 XCell 或 None(留空)的序列,从 start_column(0 起)开始写。"""
        row = self.rows.setdefault(row_no, _Row())
        row.hidden = hidden
        for offset, cell in enumerate(values):
            if cell is None:
                continue
            self.put('%s%d' % (column_letter(start_column + offset), row_no), cell)
        return self

    def hide_row(self, row_no):
        self.rows.setdefault(row_no, _Row()).hidden = True
        return self

    def merge(self, ref):
        self.merges.append(ref)
        return self

    def set_dimension(self, ref):
        self.dimension = ref
        return self


class Workbook:
    def __init__(self, date1904=False, shared_pool=None, num_formats=None, style_numfmt=None):
        self.date1904 = date1904
        self.sheets = []
        # 显式字符串池:元素为 str 或 RichText;自动文本追加在其后。
        self.shared_pool = list(shared_pool or [])
        self.num_formats = dict(num_formats or {})      # numFmtId -> formatCode
        self.style_numfmt = dict(style_numfmt or {})    # cellXfs 样式索引 -> numFmtId
        self._auto_text = {}

    def add_sheet(self, sheet: Sheet, part=None) -> 'Workbook':
        if part:
            sheet.part = part
        if sheet.part is None:
            sheet.part = '/xl/worksheets/sheet%d.xml' % (len(self.sheets) + 1)
        self.sheets.append(sheet)
        return self

    def assign_parts(self, mapping) -> 'Workbook':
        """按工作表名重排部件名,用于部件对调反例。"""
        for sheet in self.sheets:
            if sheet.name in mapping:
                sheet.part = mapping[sheet.name]
        return self

    def _pool_index(self, text):
        if text not in self._auto_text:
            index = len(self.shared_pool)
            self.shared_pool.append(text)
            self._auto_text[text] = index
        return self._auto_text[text]

    def build(self) -> bytes:
        # 先按工作表、行、列的首次出现顺序给自动文本分配字符串池索引,
        # 保证跨表索引稳定且与读取顺序一致。
        for sheet in self.sheets:
            self._prepare_sheet(sheet)
        parts = []
        parts.append(('[Content_Types].xml', self._content_types()))
        parts.append(('_rels/.rels', self._root_rels()))
        parts.append(('xl/workbook.xml', self._workbook_xml()))
        parts.append(('xl/_rels/workbook.xml.rels', self._workbook_rels()))
        if self.shared_pool:
            parts.append(('xl/sharedStrings.xml', self._shared_strings_xml()))
        parts.append(('xl/styles.xml', self._styles_xml()))
        for sheet in self.sheets:
            parts.append((sheet.part.lstrip('/'), self._worksheet_xml(sheet)))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for name, data in parts:
                info = zipfile.ZipInfo(name, ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, data)
        return buffer.getvalue()

    def _prepare_sheet(self, sheet):
        for row_no in sorted(sheet.rows):
            row = sheet.rows[row_no]
            for col_no in sorted(row.cells):
                cell = row.cells[col_no]
                if cell.kind == 'auto_text':
                    cell.shared_index = self._pool_index(cell.value)
                    cell.kind = 'shared'

    def _content_types(self):
        overrides = [
            ('/xl/workbook.xml',
             'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'),
            ('/xl/styles.xml',
             'application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml'),
        ]
        if self.shared_pool:
            overrides.append(('/xl/sharedStrings.xml',
                             'application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml'))
        for sheet in self.sheets:
            overrides.append((sheet.part,
                             'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'))
        body = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                '<Default Extension="xml" ContentType="application/xml"/>']
        for part, content_type in overrides:
            body.append('<Override PartName="%s" ContentType="%s"/>' % (escape(part), escape(content_type)))
        body.append('</Types>')
        return ''.join(body).encode('utf-8')

    def _root_rels(self):
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>').encode('utf-8')

    def _workbook_xml(self):
        attr = ' date1904="1"' if self.date1904 else ''
        body = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
                '<workbookPr%s/>' % attr, '<sheets>']
        for index, sheet in enumerate(self.sheets, start=1):
            body.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
                        % (escape(sheet.name), index, index))
        body.append('</sheets></workbook>')
        return ''.join(body).encode('utf-8')

    def _workbook_rels(self):
        body = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
        for index, sheet in enumerate(self.sheets, start=1):
            target = sheet.part
            if target.startswith('/xl/'):
                target = target[len('/xl/'):]
            body.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="%s"/>'
                        % (index, escape(target)))
        next_id = len(self.sheets) + 1
        if self.shared_pool:
            body.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
                        % next_id)
        body.append('</Relationships>')
        return ''.join(body).encode('utf-8')

    def _shared_strings_xml(self):
        body = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="%d" uniqueCount="%d">'
                % (len(self.shared_pool), len(self.shared_pool))]
        for entry in self.shared_pool:
            if isinstance(entry, RichText):
                body.append('<si>')
                for run in entry.runs:
                    body.append('<r><t xml:space="preserve">%s</t></r>' % escape(run))
                if entry.phonetic:
                    body.append('<rPh sb="0" eb="1"><t>%s</t></rPh>' % escape(entry.phonetic))
                body.append('</si>')
            else:
                body.append('<si><t xml:space="preserve">%s</t></si>' % escape(entry))
        body.append('</sst>')
        return ''.join(body).encode('utf-8')

    def _styles_xml(self):
        body = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
        if self.num_formats:
            body.append('<numFmts count="%d">' % len(self.num_formats))
            for num_fmt_id, code in sorted(self.num_formats.items()):
                body.append('<numFmt numFmtId="%d" formatCode="%s"/>' % (num_fmt_id, escape(code)))
            body.append('</numFmts>')
        max_style = 0
        for sheet in self.sheets:
            for row in sheet.rows.values():
                for cell in row.cells.values():
                    if cell.style is not None:
                        max_style = max(max_style, cell.style)
        body.append('<cellXfs count="%d">' % (max_style + 1))
        for index in range(max_style + 1):
            num_fmt_id = self.style_numfmt.get(index, 0)
            if num_fmt_id:
                body.append('<xf numFmtId="%d" fontId="0" fillId="0" borderId="0" applyNumberFormat="1"/>' % num_fmt_id)
            else:
                body.append('<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>')
        body.append('</cellXfs></styleSheet>')
        return ''.join(body).encode('utf-8')

    def _worksheet_xml(self, sheet: Sheet):
        body = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">']
        dimension = sheet.dimension
        if dimension is None and sheet.rows:
            max_row = max(sheet.rows)
            max_col = max((max(row.cells) for row in sheet.rows.values() if row.cells), default=0)
            dimension = 'A1:%s%d' % (column_letter(max_col), max_row)
        body.append('<dimension ref="%s"/>' % escape(dimension or 'A1'))
        body.append('<sheetData>')
        for row_no in sorted(sheet.rows):
            row = sheet.rows[row_no]
            hidden = ' hidden="1"' if row.hidden else ''
            body.append('<row r="%d"%s>' % (row_no, hidden))
            for col_no in sorted(row.cells):
                body.append(self._cell_xml('%s%d' % (column_letter(col_no), row_no), row.cells[col_no]))
            for raw in row.raw_cells:
                body.append(raw)
            body.append('</row>')
        body.append('</sheetData>')
        if sheet.merges:
            body.append('<mergeCells count="%d">' % len(sheet.merges))
            for ref in sheet.merges:
                body.append('<mergeCell ref="%s"/>' % escape(ref))
            body.append('</mergeCells>')
        body.append('</worksheet>')
        return ''.join(body).encode('utf-8')

    def _cell_xml(self, ref, cell: XCell):
        style = ' s="%d"' % cell.style if cell.style is not None else ''
        if cell.kind in ('shared', 'shared_rich'):
            return '<c r="%s"%s t="s"><v>%d</v></c>' % (ref, style, cell.shared_index)
        if cell.kind == 'inline':
            return ('<c r="%s"%s t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                    % (ref, style, escape(cell.value)))
        if cell.kind == 'inline_rich':
            inner = ['<is>']
            for run in cell.runs:
                inner.append('<r><t xml:space="preserve">%s</t></r>' % escape(run))
            if cell.phonetic:
                inner.append('<rPh sb="0" eb="1"><t>%s</t></rPh>' % escape(cell.phonetic))
            inner.append('</is>')
            return '<c r="%s"%s t="inlineStr">%s</c>' % (ref, style, ''.join(inner))
        if cell.kind == 'number':
            return '<c r="%s"%s><v>%s</v></c>' % (ref, style, escape(cell.value))
        if cell.kind == 'boolean':
            return '<c r="%s"%s t="b"><v>%s</v></c>' % (ref, style, escape(cell.value))
        if cell.kind == 'error':
            return '<c r="%s"%s t="e"><v>%s</v></c>' % (ref, style, escape(cell.value))
        if cell.kind == 'formula':
            type_attr = ' t="%s"' % cell.cached_type if cell.cached_type and cell.cached_type != 'n' else ''
            return ('<c r="%s"%s%s><f>%s</f><v>%s</v></c>'
                    % (ref, style, type_attr, escape(cell.formula), escape(cell.value)))
        if cell.kind == 'styled_blank':
            return '<c r="%s"%s/>' % (ref, style)
        raise ValueError('unsupported synthetic cell kind %s at %s' % (cell.kind, ref))
