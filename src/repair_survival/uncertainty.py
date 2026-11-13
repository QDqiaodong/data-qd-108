"""KM 生存估计与 RMST 的逐点不确定性报告(schema_version=1)。

独立入口 ``python -m repair_survival.uncertainty``，在不改变既有
subjects/estimate/report/cli 行为与输出的前提下，为每个 cohort 补充：

- 逐风险节点的 Greenwood 累计量与 log(-log S) 变换双侧正态逐点区间；
- horizon 处限制平均寿命(RMST)的 Greenwood 型方差、标准误与正态区间。

全部计算仅使用 Python 标准库；生存率与方差中间递推使用精确 Fraction，
浮点仅用于展示与变换后的区间，不参与任何精确递推。区间均为逐点置信
区间，不是全曲线同时置信带；报告不提供因果解释，亦不得用于安全、保修
或因果结论。
"""
import argparse
import csv
import hashlib
import io
import json
import math
import os
import tempfile
from collections import defaultdict
from datetime import date
from fractions import Fraction
from pathlib import Path

COLUMNS = ['asset_id', 'cohort', 'installed', 'failed']
SCHEMA_VERSION = 1
MODE = 'kaplan_meier_uncertainty'
DEFAULT_CONFIDENCE = 0.95
MIN_CONFIDENCE = 0.80
MAX_CONFIDENCE = 0.99

# 区间端点退化/变换方式标记，便于分析人员区分区间来源。
INTERVAL_LOG_LOG = 'log_log'
INTERVAL_DEGENERATE_ONE = 'degenerate_one'
INTERVAL_DEGENERATE_ZERO = 'degenerate_zero'

# horizon RMST 摘要状态。
RMST_OK = 'ok'
RMST_GREENWOOD_SINGULAR = 'greenwood_singular'
RMST_RISK_SET_GAP = 'risk_set_gap'
RMST_NO_BASELINE_SUPPORT = 'no_baseline_support'


def day(value):
    d = date.fromisoformat(value)
    if d.isoformat() != value:
        raise ValueError('expected YYYY-MM-DD')
    return d


def validate_confidence_level(value):
    """置信水平必须是有限十进制数且落在 [0.80, 0.99]；拒绝 NaN/Infinity。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('confidence_level must be a decimal number')
    level = float(value)
    if not math.isfinite(level):
        raise ValueError('confidence_level must be finite')
    if not (MIN_CONFIDENCE <= level <= MAX_CONFIDENCE):
        raise ValueError(
            'confidence_level must be between %.2f and %.2f inclusive'
            % (MIN_CONFIDENCE, MAX_CONFIDENCE))
    return level


def normal_quantile(p):
    """标准正态分位数 Phi^{-1}(p)，仅用标准库 math.erf 二分求逆。

    二分 80 次后括号宽度为 200/2^80，远低于双精度分辨率；不硬编码
    任何特定置信水平常数。p 必须严格位于 (0, 1)。
    """
    if not (0.0 < p < 1.0):
        raise ValueError('quantile probability must lie in (0, 1)')
    target = 2.0 * p - 1.0  # erf(z / sqrt(2)) = 2p - 1
    lo, hi = -100.0, 100.0
    scale = math.sqrt(2.0)
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if math.erf(mid / scale) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def two_sided_z(confidence_level):
    """双侧正态分位数 z，使 P(|Z| <= z) = confidence_level。"""
    return normal_quantile((1.0 + confidence_level) / 2.0)


def parse_subjects(raw, as_of):
    """从同一份输入字节解析四列 CSV，规则与 subjects.subjects 完全一致。

    返回 (已纳入个体列表, 未来安装排除 ID 列表)。UTF-8 解码失败、表头不
    匹配、参差记录、空必填字段、重复 asset、非法日期、故障早于安装或全
    部被排除时抛出 ValueError。
    """
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as e:
        raise ValueError('source is not valid UTF-8') from e
    cutoff = day(as_of)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != COLUMNS:
        raise ValueError('unexpected columns')
    rows = list(reader)
    seen = set()
    result = []
    excluded = []
    for r in rows:
        if None in r or any(v is None for v in r.values()):
            raise ValueError('ragged CSV')
        if any(not r[k].strip() for k in ('asset_id', 'cohort', 'installed')):
            raise ValueError('empty required field')
        if r['asset_id'] in seen:
            raise ValueError('duplicate asset')
        seen.add(r['asset_id'])
        start = day(r['installed'])
        end = day(r['failed']) if r['failed'] else None
        if end is not None and end < start:
            raise ValueError('failure precedes installation')
        if start > cutoff:
            excluded.append(r['asset_id'])
            continue
        event = end is not None and end <= cutoff
        result.append({
            'asset_id': r['asset_id'],
            'cohort': r['cohort'],
            'duration_days': ((end if event else cutoff) - start).days,
            'event': event,
        })
    if not result:
        raise ValueError('no observed subjects')
    return sorted(result, key=lambda r: r['asset_id']), sorted(excluded)


def _display(frac):
    """规范约分数字符串 + 有限浮点展示值；展示值不参与后续计算。"""
    return {'fraction': str(frac), 'value': float(frac)}


def _log_log_interval(survival, greenwood, z):
    """log(-log S) 变换的双侧正态逐点区间，返回 (lower, upper, method)。

    退化情形：S=1 且 G=0 -> [1,1]；S=0 -> [0,0]。其余情形要求
    0<S<1 且 G 有限。边界严格有序后裁剪到 [0,1]，不使用正态区间
    直接裁剪替代该变换。
    """
    if survival == 0:
        return 0.0, 0.0, INTERVAL_DEGENERATE_ZERO
    if survival == 1 and greenwood == 0:
        return 1.0, 1.0, INTERVAL_DEGENERATE_ONE
    if not (Fraction(0) < survival < Fraction(1)):
        raise AssertionError('survival outside (0,1) with non-degenerate Greenwood')
    sigma = math.sqrt(float(greenwood))
    w = math.log(-math.log(float(survival)))
    # W 的区间 [w - zσ, w + zσ]，反向变换 exp(-exp(·)) 单调递减。
    lower = math.exp(-math.exp(w + z * sigma))
    upper = math.exp(-math.exp(w - z * sigma))
    lower = min(1.0, max(0.0, lower))
    upper = min(1.0, max(0.0, upper))
    if not lower <= upper:
        raise AssertionError('interval bounds not ordered')
    return lower, upper, INTERVAL_LOG_LOG


def _cohort_uncertainty(members, horizon, z):
    """单 cohort 的 KM 点估计、逐节点 Greenwood 区间与 RMST 不确定性。

    风险集口径与 estimate.estimate 一致：所有个体年龄 0 进入，事件年龄 t
    的事件前完整风险集 Y(t) = {duration >= t}，同龄并列故障计为 d，
    删失不进入 Greenwood 分子；曲线为右连续阶梯，允许零日事件。
    """
    times = defaultdict(lambda: [0, 0])
    for r in members:
        times[r['duration_days']][0 if r['event'] else 1] += 1
    total = len(members)
    boundaries = sorted(set([0, horizon, *times]))

    risk = total
    survival = Fraction(1)
    greenwood = Fraction(0)
    greenwood_finite = True
    median = None
    area = Fraction(0)
    previous = 0
    nodes = []
    prefix_area = {}
    support_lost = False
    gap = None
    curve = []

    for index, t in enumerate(boundaries):
        failures_count, censored_count = times.get(t, (0, 0))
        entered = sorted(r['asset_id'] for r in members) if t == 0 else []
        at_risk_ids = sorted(r['asset_id'] for r in members if r['duration_days'] >= t)
        failure_ids = sorted(
            r['asset_id'] for r in members if r['duration_days'] == t and r['event'])
        censored_ids = sorted(
            r['asset_id'] for r in members if r['duration_days'] == t and not r['event'])
        before = risk

        # RMST 面积递推与 estimate.estimate 逐式一致（右连续阶梯）。
        if previous < horizon:
            area += survival * (min(t, horizon) - previous)
        previous = t
        prefix_area[t] = area

        term = None
        if failures_count:
            if risk == failures_count:
                # Y = d：Greenwood 分母为 0，该节点累计量不再有限。
                greenwood_finite = False
            else:
                term = Fraction(failures_count, risk * (risk - failures_count))
                greenwood += term
            survival *= Fraction(risk - failures_count, risk)
            if median is None and survival <= Fraction(1, 2):
                median = t
        risk -= failures_count + censored_count

        if t in times:
            curve.append({
                'day': t,
                'at_risk': before,
                'failures': failures_count,
                'censored': censored_count,
                'survival': float(survival),
                'survival_fraction': str(survival),
            })

        if greenwood_finite:
            cumulative = _display(greenwood)
            lower, upper, method = _log_log_interval(survival, greenwood, z)
        else:
            cumulative = None
            lower, upper, method = _log_log_interval(survival, Fraction(0), z)
        node = {
            'day': t,
            'entered': entered,
            'entered_count': len(entered),
            'at_risk': at_risk_ids,
            'at_risk_count': before,
            'failures': failure_ids,
            'failure_count': failures_count,
            'censored': censored_ids,
            'censored_count': censored_count,
            'at_risk_after': risk,
            'survival': _display(survival),
            'greenwood_term': None if term is None else _display(term),
            'greenwood_cumulative': cumulative,
            'interval': {'lower': lower, 'upper': upper, 'method': method},
        }
        nodes.append(node)

        # 支持检查：当日退出完成后，正长度区间内无在风险个体且 S>0，
        # 则自该区间起不可估计；S 精确归 0 的吸收尾部不算缺口。
        # horizon 已在 boundaries 中，下一节点即空档结束边界。
        if not support_lost and survival > 0 and risk == 0 and t < horizon:
            support_lost = True
            gap = {'start_day': t, 'end_day': boundaries[index + 1]}

    if gap is None:
        status = 'estimable'
        support_end_day = horizon
    else:
        status = ('no_baseline_support' if gap['start_day'] == 0
                  else 'risk_set_gap')
        support_end_day = gap['start_day']

    rmst_point = area
    horizon_block = {
        'day': horizon,
        'rmst_days': None,
        'variance': None,
        'standard_error': None,
        'interval': None,
        'status': RMST_OK,
        'reason': None,
    }

    if support_lost:
        reason = (RMST_NO_BASELINE_SUPPORT if gap['start_day'] == 0
                  else RMST_RISK_SET_GAP)
        horizon_block.update(status=reason, reason=reason)
    else:
        # 第二遍：A_j 为事件年龄 t_j 到 horizon 的 KM 曲线面积，
        # Var(RMST) = Σ A_j² · d_j / [Y_j (Y_j - d_j)]，仅 t_j < horizon。
        variance = Fraction(0)
        singular = False
        y_risk = total
        for t, (failures_count, _censored) in sorted(times.items()):
            if failures_count and t < horizon:
                area_before = prefix_area[t]
                a_j = rmst_point - area_before
                if y_risk == failures_count:
                    # Y=d：该事件项方差不可有限估计；点值仍保留。
                    singular = True
                else:
                    if a_j <= 0:
                        raise AssertionError('non-positive RMST Greenwood area term')
                    variance += (a_j ** 2) * Fraction(
                        failures_count, y_risk * (y_risk - failures_count))
            y_risk -= failures_count + _censored
        horizon_block['rmst_days'] = _display(rmst_point)
        if singular:
            horizon_block.update(
                status=RMST_GREENWOOD_SINGULAR,
                reason=RMST_GREENWOOD_SINGULAR)
        else:
            se = math.sqrt(float(variance))
            point = float(rmst_point)
            lower = point - z * se
            upper = point + z * se
            lower = min(float(horizon), max(0.0, lower))
            upper = min(float(horizon), max(0.0, upper))
            horizon_block.update(
                variance=_display(variance),
                standard_error=se,
                interval={'lower': lower, 'upper': upper},
                status=RMST_OK)

    # 既有 KM 点估计块：字段口径与 estimate.estimate 的 cohort 输出一致；
    # 支持缺口导致 horizon RMST 不可估计时 rmst_days 为 null。
    km = {
        'subjects': total,
        'events': sum(1 for r in members if r['event']),
        'median_days': median,
        'rmst_horizon_days': horizon,
        'rmst_days': None if support_lost else float(rmst_point),
        'curve': curve,
    }
    return {
        'status': status,
        'subjects': total,
        'support_end_day': support_end_day,
        'gap': gap,
        'km': km,
        'risk_table': nodes,
        'horizon': horizon_block,
    }


def build_report(source, as_of, horizon, confidence_level=DEFAULT_CONFIDENCE):
    """读取一次输入字节，完成校验、分析与序列化对象构建，返回报告 dict。"""
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
        raise ValueError('horizon must be positive integer days')
    level = validate_confidence_level(confidence_level)
    raw = Path(source).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    rows, excluded = parse_subjects(raw, as_of)
    z = two_sided_z(level)
    groups = defaultdict(list)
    for row in rows:
        groups[row['cohort']].append(row)
    cohorts = {
        name: _cohort_uncertainty(members, horizon, z)
        for name, members in sorted(groups.items())
    }
    return {
        'schema_version': SCHEMA_VERSION,
        'mode': MODE,
        'parameters': {
            'as_of': as_of,
            'horizon_days': horizon,
            'confidence_level': level,
            'source_sha256': digest,
        },
        'excluded_future_installations': excluded,
        'interpretation': {
            'interval_type': 'pointwise',
            'simultaneous_confidence_band': False,
            'survival_transform': 'log(-log(survival))',
            'greenwood_term': 'd_j/(Y_j*(Y_j-d_j))',
            'rmst_variance': 'sum(A_j^2*d_j/(Y_j*(Y_j-d_j)))',
            'causal_interpretation_provided': False,
            'synthetic_analysis_only': True,
            'not_for_safety_warranty_or_causal_conclusions': True,
        },
        'cohorts': cohorts,
    }


def analyze(source, as_of, horizon, output, confidence_level=DEFAULT_CONFIDENCE):
    """与 CLI 对应的 Python 调用：构建报告并经同目录临时文件原子替换。"""
    report = build_report(source, as_of, horizon, confidence_level)
    payload = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n'
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.uncertainty-', suffix='.tmp',
                                dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(payload)
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='repair_survival.uncertainty',
        description='Kaplan-Meier pointwise uncertainty and RMST variance report')
    parser.add_argument('--source', required=True)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--horizon', required=True, type=int)
    parser.add_argument('--confidence-level', type=float,
                        default=DEFAULT_CONFIDENCE)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        report = analyze(args.source, args.as_of, args.horizon, args.output,
                         args.confidence_level)
    except (ValueError, OSError) as e:
        parser.exit(2, str(e) + '\n')
    summary = {
        'mode': report['mode'],
        'schema_version': report['schema_version'],
        'cohorts': {
            name: {
                'status': c['status'],
                'support_end_day': c['support_end_day'],
                'rmst': c['horizon']['status'],
                'reason': c['horizon']['reason'],
            }
            for name, c in report['cohorts'].items()
        },
    }
    print(json.dumps(summary, sort_keys=True))


if __name__ == '__main__':
    main()
