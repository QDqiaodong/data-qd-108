"""延迟进入竞争风险退出分析(schema_version=2)。

在现有预防性更换策略下,按 cohort 估计指定使用年龄前首次失效与失效前
更换的累计退出概率,以及失效或更换之前的限制平均在役时间(RMST)。
设备可从安装后的监测日才纳入(延迟进入);声明退出在未来或未声明时
行政右删失于截止日。全程精确有理数计算,浮点仅用于展示,不参与递推。
"""
import argparse
import csv
import hashlib
import io
import json
import os
import tempfile
from collections import defaultdict
from datetime import date
from fractions import Fraction
from pathlib import Path

COLUMNS = ['asset_id', 'cohort', 'installed', 'observed_from', 'exit_date', 'exit_kind']
EXIT_KINDS = ('failure', 'replacement')
SCHEMA_VERSION = 2
MODE = 'delayed_entry_competing_risks'


def day(value):
    d = date.fromisoformat(value)
    if d.isoformat() != value:
        raise ValueError('expected YYYY-MM-DD')
    return d


def parse_devices(raw):
    """解析并整体验证 CSV 字节;任何契约错误都拒绝整份输入。

    记录序号 record 从 1 开始,表头不计。所有行(包括随后会被截止日
    排除的行)都先通过契约校验,再进入截止日转换。
    """
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as e:
        raise ValueError('source is not valid UTF-8') from e
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError('empty source')
    if rows[0] != COLUMNS:
        raise ValueError('unexpected columns: expected ' + ','.join(COLUMNS))
    devices = []
    seen = set()
    for record, fields in enumerate(rows[1:], start=1):
        if len(fields) != len(COLUMNS):
            raise ValueError('ragged record at data row %d' % record)
        asset_id, cohort, installed, observed_from, exit_date, exit_kind = fields
        for label, value in (('asset_id', asset_id), ('cohort', cohort)):
            if not value or value != value.strip():
                raise ValueError('%s must be non-empty without surrounding whitespace at data row %d' % (label, record))
        if asset_id in seen:
            raise ValueError('duplicate asset_id ' + asset_id)
        seen.add(asset_id)
        installed_day = day(installed)
        observed_day = day(observed_from)
        if observed_day < installed_day:
            raise ValueError('observed_from precedes installed for ' + asset_id)
        if (exit_date == '') != (exit_kind == ''):
            raise ValueError('exit_date and exit_kind must be both empty or both set for ' + asset_id)
        if exit_kind and exit_kind not in EXIT_KINDS:
            raise ValueError('unknown exit_kind ' + exit_kind + ' for ' + asset_id)
        exit_day = day(exit_date) if exit_date else None
        if exit_day is not None and exit_day < observed_day:
            raise ValueError('exit precedes observed_from for ' + asset_id)
        devices.append({
            'record': record,
            'asset_id': asset_id,
            'cohort': cohort,
            'installed': installed_day,
            'observed_from': observed_day,
            'exit_date': exit_day,
            'exit_kind': exit_kind or None,
        })
    if not devices:
        raise ValueError('no device records')
    return devices


def transform(devices, cutoff):
    """按包含当天的截止日转换为年龄轴记录;返回(全部设备记录, 已纳入记录)。"""
    records = []
    enrolled = []
    for d in devices:
        rec = {
            'record': d['record'],
            'asset_id': d['asset_id'],
            'cohort': d['cohort'],
            'installed': d['installed'].isoformat(),
            'observed_from': d['observed_from'].isoformat(),
            'declared_exit_date': d['exit_date'].isoformat() if d['exit_date'] else None,
            'declared_exit_kind': d['exit_kind'],
            'status': 'enrolled',
            'exclusion_reason': None,
            'entry_age_days': None,
            'exit_age_days': None,
            'exit_kind': None,
            'censor_reason': None,
        }
        if d['installed'] > cutoff:
            rec['status'] = 'excluded'
            rec['exclusion_reason'] = 'not_installed_by_cutoff'
        elif d['observed_from'] > cutoff:
            rec['status'] = 'excluded'
            rec['exclusion_reason'] = 'not_enrolled_by_cutoff'
        else:
            rec['entry_age_days'] = (d['observed_from'] - d['installed']).days
            if d['exit_date'] is not None and d['exit_date'] <= cutoff:
                rec['exit_age_days'] = (d['exit_date'] - d['installed']).days
                rec['exit_kind'] = d['exit_kind']
            else:
                rec['exit_age_days'] = (cutoff - d['installed']).days
                rec['exit_kind'] = 'censor'
                rec['censor_reason'] = 'exit_after_cutoff' if d['exit_date'] is not None else 'no_declared_exit'
            enrolled.append(rec)
        records.append(rec)
    if not enrolled:
        raise ValueError('no enrolled devices: all excluded by cutoff')
    return records, enrolled


def _display(frac):
    """规范约分数字符串 + 有限浮点展示值;展示值不参与后续计算。"""
    return {'fraction': str(frac), 'value': float(frac)}


def estimate_cohort(members, horizon):
    """单 cohort 的延迟进入竞争风险估计;只扫描真实年龄边界,不逐日展开。

    年龄 t 先加入 e=t 的设备,当日失效、更换、删失共享退出前完整风险集;
    两类事件使用同一个事件前 S_prev。删失不增加任何累计概率。
    """
    entries = defaultdict(list)
    exits = defaultdict(list)
    for m in members:
        entries[m['entry_age_days']].append(m)
        exits[m['exit_age_days']].append(m)
    boundaries = sorted(b for b in {0, horizon, *entries, *exits} if b <= horizon)
    survival = Fraction(1)
    failure = Fraction(0)
    replacement = Fraction(0)
    area = Fraction(0)
    at_risk = []
    previous = None
    support_lost = False
    gap = None
    state_at = {}
    nodes = []
    for index, age in enumerate(boundaries):
        if previous is not None and not support_lost:
            area += survival * (age - previous)
        entered = sorted(entries.get(age, ()), key=lambda m: m['asset_id'])
        at_risk.extend(entered)
        left = sorted(exits.get(age, ()), key=lambda m: m['asset_id'])
        failures = [m['asset_id'] for m in left if m['exit_kind'] == 'failure']
        replacements = [m['asset_id'] for m in left if m['exit_kind'] == 'replacement']
        censored = [m['asset_id'] for m in left if m['exit_kind'] == 'censor']
        n = len(at_risk)
        if not support_lost:
            if n:
                failure += survival * len(failures) / n
                replacement += survival * len(replacements) / n
                survival *= 1 - Fraction(len(failures) + len(replacements), n)
                if survival + failure + replacement != 1:
                    raise AssertionError('probability mass not conserved')
            state_at[age] = (survival, failure, replacement, area)
        nodes.append({
            'day': age,
            'entered': [m['asset_id'] for m in entered],
            'entered_count': len(entered),
            'at_risk': sorted(m['asset_id'] for m in at_risk),
            'at_risk_count': n,
            'failures': failures,
            'failure_count': len(failures),
            'replacements': replacements,
            'replacement_count': len(replacements),
            'censored': censored,
            'censored_count': len(censored),
            'at_risk_after': n - len(left),
            'survival': None if support_lost else _display(survival),
            'failure_cumulative': None if support_lost else _display(failure),
            'replacement_cumulative': None if support_lost else _display(replacement),
        })
        if left:
            gone = {m['asset_id'] for m in left}
            at_risk = [m for m in at_risk if m['asset_id'] not in gone]
        previous = age
        # 支持检查:当日退出完成后,若后续正长度区间内无在风险设备且 S>0,
        # 则自该区间起失去观测支持;后续再进入不能恢复。S 精确为 0 时两类
        # 吸收概率已承接全部质量,零在役尾部不算缺口。
        if not support_lost and survival > 0 and not at_risk and index + 1 < len(boundaries):
            support_lost = True
            gap = {'start_day': age, 'end_day': boundaries[index + 1]}
    if gap is None:
        status = 'estimable'
        support_end_day = horizon
    else:
        status = 'no_baseline_support' if gap['start_day'] == 0 else 'risk_set_gap'
        support_end_day = gap['start_day']
    prefix_survival, prefix_failure, prefix_replacement, prefix_area = state_at[support_end_day]
    prefix = {
        'day': support_end_day,
        'survival': _display(prefix_survival),
        'failure_cumulative': _display(prefix_failure),
        'replacement_cumulative': _display(prefix_replacement),
        'rmst_days': _display(prefix_area),
    }
    horizon_section = {'day': horizon, 'estimable': gap is None}
    if gap is None:
        h_survival, h_failure, h_replacement, h_area = state_at[horizon]
        horizon_section.update({
            'survival': _display(h_survival),
            'failure_cumulative': _display(h_failure),
            'replacement_cumulative': _display(h_replacement),
            'rmst_days': _display(h_area),
        })
    else:
        horizon_section.update({
            'survival': None,
            'failure_cumulative': None,
            'replacement_cumulative': None,
            'rmst_days': None,
        })
    return {
        'status': status,
        'subjects': len(members),
        'support_end_day': support_end_day,
        'gap': gap,
        'prefix': prefix,
        'horizon': horizon_section,
        'risk_table': nodes,
    }


def build_report(source, as_of, horizon):
    """读取一次输入字节,完成校验、转换与各 cohort 估计,返回报告 dict。"""
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
        raise ValueError('horizon must be positive integer days')
    cutoff = day(as_of)
    raw = Path(source).read_bytes()
    devices = parse_devices(raw)
    records, enrolled = transform(devices, cutoff)
    groups = defaultdict(list)
    for rec in enrolled:
        groups[rec['cohort']].append(rec)
    cohorts = {name: estimate_cohort(members, horizon) for name, members in sorted(groups.items())}
    return {
        'schema_version': SCHEMA_VERSION,
        'mode': MODE,
        'parameters': {
            'as_of': cutoff.isoformat(),
            'horizon_days': horizon,
            'source_sha256': hashlib.sha256(raw).hexdigest(),
        },
        'devices': sorted(records, key=lambda r: r['asset_id']),
        'cohorts': cohorts,
    }


def analyze(source, as_of, horizon, output):
    """与 CLI 对应的 Python 调用:构建报告并原子替换 output,返回报告 dict。"""
    report = build_report(source, as_of, horizon)
    payload = json.dumps(report, indent=2, sort_keys=True) + '\n'
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.competing-', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(payload)
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='repair_survival.competing',
        description='Delayed-entry competing-risks exit analysis (failure vs preventive replacement)')
    parser.add_argument('--source', required=True)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--horizon', required=True, type=int)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        report = analyze(args.source, args.as_of, args.horizon, args.output)
    except (ValueError, OSError) as e:
        parser.exit(2, str(e) + '\n')
    summary = {
        name: {
            'status': c['status'],
            'subjects': c['subjects'],
            'support_end_day': c['support_end_day'],
            'gap': c['gap'],
            'horizon': c['horizon'],
        }
        for name, c in report['cohorts'].items()
    }
    print(json.dumps({'mode': report['mode'], 'cohorts': summary}, sort_keys=True))


if __name__ == '__main__':
    main()
