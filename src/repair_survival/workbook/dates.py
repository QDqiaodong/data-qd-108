"""Excel 日期序列与严格 ISO 文本的整数转换(不经 float)。

规则(与 Excel 兼容口径,但只接受整数日):

- 1900 制:序列 1 = 1900-01-01;59 = 1900-02-28;60 是 Excel 虚构的
  1900-02-29,一律拒绝;61 = 1900-03-01。序列 0、负数、分数日拒绝。
- 1904 制:序列 0 = 1904-01-01;负数、分数日拒绝。
- 不以单元格 numFmt 判断日期有效性;转换结果超出 :class:`datetime.date`
  范围时拒绝。
"""
from __future__ import annotations

from datetime import date, timedelta

_EPOCH_1900 = date(1900, 1, 1)
_EPOCH_1904 = date(1904, 1, 1)
_DATE_MIN = date.min
_DATE_MAX = date.max
_EXCEL_1900_FAKE_LEAP = 60


def strict_iso_text(token: str) -> date:
    """严格 ``YYYY-MM-DD`` 文本;``date.fromisoformat`` 的宽松写法一律拒绝。"""
    if len(token) != 10 or token[4] != '-' or token[7] != '-':
        raise ValueError('expected strict YYYY-MM-DD text, got ' + repr(token))
    year_text, month_text, day_text = token.split('-')
    if not (year_text.isdigit() and month_text.isdigit() and day_text.isdigit()):
        raise ValueError('expected strict YYYY-MM-DD text, got ' + repr(token))
    if len(month_text) != 2 or len(day_text) != 2:
        raise ValueError('expected strict YYYY-MM-DD text, got ' + repr(token))
    try:
        parsed = date(int(year_text), int(month_text), int(day_text))
    except ValueError as exc:
        raise ValueError('not a real calendar date: ' + repr(token)) from exc
    if parsed.isoformat() != token:
        raise ValueError('expected strict YYYY-MM-DD text, got ' + repr(token))
    return parsed


def integer_token(token: str) -> int:
    """把数值单元格的文本令牌精确解析为整数,不经 float、不四舍五入。

    允许可选负号与一位以上十进制数字;正号、指数、小数、空白及其他写法拒绝。
    """
    if not isinstance(token, str) or not token:
        raise ValueError('empty numeric token')
    body = token[1:] if token[0] == '-' else token
    if not body.isdigit():
        raise ValueError('numeric token is not an exact integer: ' + repr(token))
    return int(token)


def serial_to_date(serial: int, date1904: bool) -> date:
    """按工作簿日期制把整数 Excel 序列转换为日历日。

    1900 制与 Excel 口径一致:序列 1 = 1900-01-01,即 epoch+serial-1 天;
    序列 60 对应 Excel 虚构的 1900-02-29,显式拒绝,其后序列因少一天而回补。
    """
    if date1904:
        if serial < 0:
            raise ValueError('negative date serial in 1904 system: %d' % serial)
        try:
            return _EPOCH_1904 + timedelta(days=serial)
        except OverflowError as exc:
            raise ValueError('date serial %d is outside the datetime.date range' % serial) from exc
    if serial <= 0:
        raise ValueError('1900-system date serial must be >= 1, got %d' % serial)
    if serial == _EXCEL_1900_FAKE_LEAP:
        raise ValueError('serial 60 is the fictitious Excel 1900-02-29 and is rejected')
    if serial < _EXCEL_1900_FAKE_LEAP:
        days = serial - 1
    else:
        # 虚构闰日之后的序列比真实日历多占一天,回补后再按 epoch+serial-1 换算。
        days = serial - 2
    try:
        return _EPOCH_1900 + timedelta(days=days)
    except OverflowError as exc:
        raise ValueError('date serial %d is outside the datetime.date range' % serial) from exc


def date_in_range(value: date) -> None:
    if not (_DATE_MIN <= value <= _DATE_MAX):
        raise ValueError('date outside the datetime.date range')
