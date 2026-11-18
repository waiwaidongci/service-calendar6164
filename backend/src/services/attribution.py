import hashlib
import itertools
import json
import time
from fractions import Fraction
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from src.config.settings import REDIS_URL
from src.models.attribution import Attribution
from src.models.run import Run
from src.services.audit import log
from src.services.calculation import business_minutes
from src.services.validation import validate_policy, validate_source
from src.utils.serialize import row as serialize_row

MICRO = 1_000_000
FACTORS = (('sources', '底稿'), ('calendar', '营业时段'), ('threshold', 'SLA阈值'))
FACTOR_KEYS = tuple(key for key, _ in FACTORS)
FACTOR_LABELS = dict(FACTORS)
CALENDAR_KEYS = ('timezone', 'weekdays', 'windows', 'holidays')
METHOD = (
    '以两份已复核报告的冻结快照为基准/目标，对底稿、营业时段、SLA阈值三项枚举 2³=8 种因素组合，'
    '按 3!=6 种替换顺序逐一替换并计算整体超时率，取各因素边际变化的平均值（Shapley）。'
    '小组贡献采用 v_g(S)=组内超时数/组合总样本数 的份额口径，包含小组新增、移除与样本结构变化，'
    '因此各小组贡献合计严格等于全局超时率变化。贡献以百分点计，以百万分之一百分点为整数单元分配后折算为 6 位小数，'
    '展示值加总与全局变化一致。'
)


def _round_half_up(value):
    """Fraction -> 四舍五入整数（避免 Python round 的银行家舍入）。"""
    sign = -1 if value < 0 else 1
    value = abs(value)
    whole, rest = divmod(value.numerator, value.denominator)
    if 2 * rest >= value.denominator:
        whole += 1
    return sign * whole


def _allocate(total_units, weights):
    """把 total_units 按权重累计取整分给各项；最后一项吸收尾差，保证分项和恰等于 total_units。"""
    if not weights:
        if total_units:
            raise ValueError('归因结果不完整：没有可分配的小组')
        return []
    units, cumulative, previous = [], Fraction(0), 0
    for weight in weights[:-1]:
        cumulative += weight
        current = _round_half_up(cumulative * MICRO)
        units.append(current - previous)
        previous = current
    units.append(total_units - previous)
    if sum(units) != total_units:
        raise ValueError('归因结果不完整：分项取整后合计与全局变化不一致')
    return units


def _pp(units):
    return round(units / MICRO, 6)


def _num(value):
    return round(float(value), 6)


def _coalition_key(flags):
    return ''.join('1' if flags[key] else '0' for key in FACTOR_KEYS)


def _side(flag):
    return '目标报告' if flag else '基准报告'


def _coalition_label(flags):
    return '·'.join(f'{FACTOR_LABELS[key]}={_side(flags[key])}' for key in FACTOR_KEYS)


def _coalition_policy(base_policy, target_policy, flags):
    calendar = target_policy if flags['calendar'] else base_policy
    label = f"{base_policy.get('version', '基准口径')}→{target_policy.get('version', '目标口径')}"
    policy = {'version': label[:120]}
    for key in CALENDAR_KEYS:
        if key in calendar:
            policy[key] = calendar[key]
    policy['sla_minutes'] = target_policy['sla_minutes'] if flags['threshold'] else base_policy['sla_minutes']
    return policy


def _coalitions(base_snapshot, target_snapshot):
    coalitions = []
    for bits in itertools.product((0, 1), repeat=len(FACTOR_KEYS)):
        flags = dict(zip(FACTOR_KEYS, bits))
        coalitions.append({
            'key': _coalition_key(flags),
            'flags': flags,
            'label': _coalition_label(flags),
            'sources': target_snapshot['sources'] if flags['sources'] else base_snapshot['sources'],
            'policy': _coalition_policy(base_snapshot['policy'], target_snapshot['policy'], flags),
        })
    return coalitions


def _preflight(coalitions):
    problems = []
    seen = set()

    def add(message):
        if message not in seen:
            seen.add(message)
            problems.append(message)

    for coalition in coalitions:
        try:
            validate_policy(coalition['policy'])
        except ValueError as exc:
            add(f"{coalition['label']} 口径无效：{exc}")
    for side, sources in (('基准报告', coalitions[0]['sources']), ('目标报告', coalitions[-1]['sources'])):
        for index, source in enumerate(sources, 1):
            try:
                validate_source(source)
            except ValueError as exc:
                add(f'{side}第{index}行底稿无效：{exc}')
    for coalition in coalitions:
        if not coalition['sources']:
            add(f"{coalition['label']} 没有底稿样本，无法计算超时率")
    for coalition in coalitions:
        thresholds = coalition['policy'].get('sla_minutes')
        if not isinstance(thresholds, dict) or not thresholds:
            add(f"{coalition['label']} 的 SLA 阈值为空")
            continue
        missing = sorted({source.get('priority') for source in coalition['sources']} - set(thresholds))
        for priority in missing:
            add(f"{coalition['label']} 缺少优先级「{priority}」的 SLA 阈值")
    if problems:
        shown = '；'.join(problems[:8])
        if len(problems) > 8:
            shown += f'；等共 {len(problems)} 项'
        raise ValueError(f'归因整次拒绝，未发布任何部分结果：{shown}')


def _evaluate(sources, policy):
    groups = {}
    overdue_total = 0
    for source in sources:
        validate_source(source)
        priority = source['priority']
        if priority not in policy['sla_minutes']:
            raise ValueError(f"缺少优先级「{priority}」的 SLA 阈值，归因整次拒绝")
        minutes = business_minutes(source['opened'], source['closed'], policy)
        overdue = int(minutes > policy['sla_minutes'][priority])
        overdue_total += overdue
        group = groups.setdefault(source['department'], {'count': 0, 'overdue': 0})
        group['count'] += 1
        group['overdue'] += overdue
    return {'count': len(sources), 'overdue': overdue_total, 'groups': groups}


def _shapley(keys, values, group_values):
    """values[key] 为 Fraction；返回因素与分组在各替换顺序下的平均边际贡献。"""
    factor = {key: Fraction(0) for key in FACTOR_KEYS}
    group = {name: {key: Fraction(0) for key in FACTOR_KEYS} for name in group_values}
    orders = []
    for order in itertools.permutations(FACTOR_KEYS):
        previous_key, chosen = '0' * len(FACTOR_KEYS), set()
        steps = []
        for key in order:
            chosen.add(key)
            current_key = ''.join('1' if factor_key in chosen else '0' for factor_key in FACTOR_KEYS)
            marginal = values[current_key] - values[previous_key]
            factor[key] += marginal
            for name in group_values:
                group[name][key] += group_values[name][current_key] - group_values[name][previous_key]
            steps.append({
                'factor': key,
                'factor_label': FACTOR_LABELS[key],
                'from': previous_key,
                'to': current_key,
                'marginal_pp': _num(marginal),
                'marginal_fraction': marginal,
            })
            previous_key = current_key
        orders.append({'order': list(order), 'labels': [FACTOR_LABELS[key] for key in order], 'steps': steps})
    total_orders = len(orders)
    for key in factor:
        factor[key] /= total_orders
    for name in group:
        for key in group[name]:
            group[name][key] /= total_orders
    return factor, group, orders


def _verify_internal(stats, groups, factor_exact, group_exact, factor_units, matrix_units, group_units, delta_units, orders):
    problems = []
    expected_keys = {_coalition_key(dict(zip(FACTOR_KEYS, bits))) for bits in itertools.product((0, 1), repeat=3)}
    if set(stats) != expected_keys or len(stats) != 8:
        problems.append('因素组合不足 8 种')
    for key, stat in stats.items():
        if set(stat['groups']) - set(groups):
            problems.append(f'组合 {key} 出现未知小组')
        if sum(item['count'] for item in stat['groups'].values()) != stat['count']:
            problems.append(f'组合 {key} 分组样本数合计不等于总样本数')
        if sum(item['overdue'] for item in stat['groups'].values()) != stat['overdue']:
            problems.append(f'组合 {key} 分组超时数合计不等于总超时数')
    if abs(sum(factor_exact.values()) - sum(group_exact[name][key] for name in groups for key in FACTOR_KEYS)) > Fraction(1, 10**9):
        problems.append('因素贡献与小组贡献的精确合计不一致')
    if sum(factor_units) != delta_units:
        problems.append('因素贡献加总不等于全局变化')
    if sum(group_units) != delta_units:
        problems.append('小组贡献加总不等于全局变化')
    for index, key in enumerate(FACTOR_KEYS):
        if sum(matrix_units[key]) != factor_units[index]:
            problems.append(f'{FACTOR_LABELS[key]}的小组分配合计不等于该因素贡献')
        if abs(Fraction(factor_exact[key]) * MICRO - factor_units[index]) > 1:
            problems.append(f'{FACTOR_LABELS[key]}的取整偏差超出 1 个整数单元')
    if len(orders) != 6 or any(len(order['steps']) != 3 for order in orders):
        problems.append('替换顺序未覆盖 6 种全排列')
    if problems:
        raise ValueError('归因结果不完整，已拒绝发布：' + '；'.join(problems))


def _verify_result(result):
    problems = []
    required = {'change_pp', 'change_micro', 'factors', 'groups', 'coalitions', 'orders'}
    missing = sorted(required - set(result))
    if missing:
        raise ValueError('归因结果不完整，已拒绝发布：缺少字段 ' + '、'.join(missing))
    coalitions = result['coalitions']
    if len(coalitions) != 8 or len({item['key'] for item in coalitions}) != 8:
        problems.append('发布的组合数不是 8')
    group_names = {group['group'] for group in result['groups']}
    for coalition in coalitions:
        rows = coalition['groups']
        if {row['group'] for row in rows} != group_names:
            problems.append(f"组合 {coalition['key']} 的小组不齐")
        if sum(row['count'] for row in rows) != coalition['count']:
            problems.append(f"组合 {coalition['key']} 样本数不齐")
        if sum(row['overdue'] for row in rows) != coalition['overdue']:
            problems.append(f"组合 {coalition['key']} 超时数不齐")
    micro = result['change_micro']
    if sum(item['contribution_micro'] for item in result['factors']) != micro:
        problems.append('因素贡献整数单元加总与全局变化不等')
    if sum(item['contribution_micro'] for item in result['groups']) != micro:
        problems.append('小组贡献整数单元加总与全局变化不等')
    for group_row in result['groups']:
        if sum(item['contribution_micro'] for item in group_row['by_factor'].values()) != group_row['contribution_micro']:
            problems.append(f"小组 {group_row['group']} 的因素拆解加总与小组贡献不等")
    if abs(sum(item['contribution_pp'] for item in result['factors']) - result['change_pp']) > 5e-10:
        problems.append('因素贡献加总与全局变化不等')
    if abs(sum(item['contribution_pp'] for item in result['groups']) - result['change_pp']) > 5e-10:
        problems.append('小组贡献加总与全局变化不等')
    if len(result.get('orders', [])) != 6:
        problems.append('替换顺序记录不齐')
    if problems:
        raise ValueError('归因结果不完整，已拒绝发布：' + '；'.join(problems))


def attribute(base_run, target_run):
    """base_run/target_run 为含 id/name/checksum/review/snapshot 的冻结快照元数据。"""
    base_snapshot, target_snapshot = base_run['snapshot'], target_run['snapshot']
    coalitions = _coalitions(base_snapshot, target_snapshot)
    _preflight(coalitions)

    stats = {coalition['key']: _evaluate(coalition['sources'], coalition['policy']) for coalition in coalitions}
    groups = sorted({name for stat in stats.values() for name in stat['groups']})
    values = {key: Fraction(stat['overdue'], stat['count']) * 100 for key, stat in stats.items()}
    group_values = {
        name: {
            key: Fraction(stat['groups'].get(name, {}).get('overdue', 0), stat['count']) * 100
            for key, stat in stats.items()
        }
        for name in groups
    }

    factor_exact, group_exact, orders = _shapley(FACTOR_KEYS, values, group_values)
    delta = values['1' * 3] - values['0' * 3]
    delta_units = _round_half_up(delta * MICRO)
    # 一次累计取整覆盖「因素×小组」全部单元：全表加总严格等于全局变化，
    # 因素合计与小组合计都由同一批整数单元相加得出，避免两层取整互相对不上。
    cells = _allocate(delta_units, [group_exact[name][key] for key in FACTOR_KEYS for name in groups])
    matrix_units = {
        key: cells[index * len(groups):(index + 1) * len(groups)]
        for index, key in enumerate(FACTOR_KEYS)
    }
    factor_units = [sum(matrix_units[key]) for key in FACTOR_KEYS]
    group_units = [sum(matrix_units[key][index] for key in FACTOR_KEYS) for index in range(len(groups))]
    _verify_internal(stats, groups, factor_exact, group_exact, factor_units, matrix_units, group_units, delta_units, orders)

    base_stat, target_stat = stats['0' * 3], stats['1' * 3]
    base_meta = {
        'run_id': base_run['id'], 'name': base_run['name'], 'checksum': base_run.get('checksum'),
        'policy_version': base_snapshot['policy'].get('version'), 'period': base_snapshot.get('period'),
        'review': base_run.get('review') or '', 'sample_count': base_stat['count'],
        'overdue_count': base_stat['overdue'],
        'rate_pct': _num(Fraction(base_stat['overdue'], base_stat['count']) * 100),
    }
    target_meta = {
        'run_id': target_run['id'], 'name': target_run['name'], 'checksum': target_run.get('checksum'),
        'policy_version': target_snapshot['policy'].get('version'), 'period': target_snapshot.get('period'),
        'review': target_run.get('review') or '', 'sample_count': target_stat['count'],
        'overdue_count': target_stat['overdue'],
        'rate_pct': _num(Fraction(target_stat['overdue'], target_stat['count']) * 100),
    }

    factor_rows = [
        {
            'factor': key, 'label': FACTOR_LABELS[key], 'contribution_pp': _pp(factor_units[index]),
            'contribution_micro': factor_units[index], 'exact_pp': _num(factor_exact[key]),
        }
        for index, key in enumerate(FACTOR_KEYS)
    ]

    group_rows = []
    for index, name in enumerate(groups):
        base_group = base_stat['groups'].get(name, {'count': 0, 'overdue': 0})
        target_group = target_stat['groups'].get(name, {'count': 0, 'overdue': 0})
        if base_group['count'] and target_group['count']:
            status = 'both'
        elif target_group['count']:
            status = 'added'
        else:
            status = 'removed'
        group_rows.append({
            'group': name,
            'status': status,
            'base': {
                'count': base_group['count'], 'overdue': base_group['overdue'],
                'rate_pct': _num(Fraction(base_group['overdue'], base_group['count']) * 100) if base_group['count'] else None,
            },
            'target': {
                'count': target_group['count'], 'overdue': target_group['overdue'],
                'rate_pct': _num(Fraction(target_group['overdue'], target_group['count']) * 100) if target_group['count'] else None,
            },
            'contribution_pp': _pp(group_units[index]),
            'contribution_micro': group_units[index],
            'by_factor': {
                key: {
                    'contribution_pp': _pp(matrix_units[key][index]),
                    'contribution_micro': matrix_units[key][index],
                    'exact_pp': _num(group_exact[name][key]),
                }
                for key in FACTOR_KEYS
            },
        })

    coalition_rows = []
    for coalition in coalitions:
        stat = stats[coalition['key']]
        coalition_rows.append({
            'key': coalition['key'],
            'label': coalition['label'],
            'flags': coalition['flags'],
            'sources_side': _side(coalition['flags']['sources']),
            'calendar_side': _side(coalition['flags']['calendar']),
            'threshold_side': _side(coalition['flags']['threshold']),
            'policy_version': coalition['policy']['version'],
            'count': stat['count'],
            'overdue': stat['overdue'],
            'rate_pct': _num(Fraction(stat['overdue'], stat['count']) * 100),
            'groups': [
                {
                    'group': name,
                    'count': stat['groups'].get(name, {}).get('count', 0),
                    'overdue': stat['groups'].get(name, {}).get('overdue', 0),
                    'rate_pct': _num(Fraction(stat['groups'][name]['overdue'], stat['groups'][name]['count']) * 100) if name in stat['groups'] else None,
                }
                for name in groups
            ],
        })

    order_rows = [
        {
            'order': order['order'],
            'labels': order['labels'],
            'steps': [
                {key: step[key] for key in ('factor', 'factor_label', 'from', 'to', 'marginal_pp')}
                for step in order['steps']
            ],
            'sum_pp': _num(sum(step['marginal_fraction'] for step in order['steps'])),
        }
        for order in orders
    ]

    warnings = []
    if base_meta['period'] != target_meta['period']:
        warnings.append(f"两份报告期间不同（{base_meta['period']} → {target_meta['period']}），底稿因素同时承载期间构成变化。")
    if base_meta['policy_version'] == target_meta['policy_version'] and base_snapshot['sources'] == target_snapshot['sources']:
        warnings.append('两份报告的冻结来源与口径完全一致，归因变化应为 0。')

    result = {
        'base': base_meta,
        'target': target_meta,
        'change_pp': _pp(delta_units),
        'change_micro': delta_units,
        'change_unit': '百分点',
        'factors': factor_rows,
        'groups': group_rows,
        'coalitions': coalition_rows,
        'orders': order_rows,
        'method': METHOD,
        'warnings': warnings,
        'completeness': [
            f'因素组合 {len(coalition_rows)}/8 齐备',
            f'六种替换顺序 {len(order_rows)}/6 齐备',
            f'小组 {len(group_rows)} 个，各组合均给出样本数与超时数',
            f'三项因素贡献合计 = 全局变化（{_pp(delta_units)} 个百分点）',
            '各小组贡献合计 = 全局变化（同上）',
        ],
        'checks': {
            'passed': True,
            'factor_sum_pp': _pp(sum(factor_units)),
            'group_sum_pp': _pp(sum(group_units)),
            'global_change_pp': _pp(delta_units),
            'factor_sum_micro': sum(factor_units),
            'group_sum_micro': sum(group_units),
            'global_change_micro': delta_units,
            'coalition_count': len(coalition_rows),
            'order_count': len(order_rows),
            'factor_count': len(factor_rows),
            'group_count': len(group_rows),
        },
        'basis': {
            'base_run_id': base_run['id'], 'target_run_id': target_run['id'],
            'base_checksum': base_run.get('checksum'), 'target_checksum': target_run.get('checksum'),
            'base_sample_count': base_stat['count'], 'target_sample_count': target_stat['count'],
            'policy_versions': {'base': base_meta['policy_version'], 'target': target_meta['policy_version']},
            'factor_definition': {
                'sources': '底稿：冻结来源行集合',
                'calendar': '营业时段：时区、工作日、工作时窗与假日',
                'threshold': 'SLA阈值：各优先级业务分钟阈值',
            },
            'coalition_keys': [item['key'] for item in coalition_rows],
            'group_game': 'v_g(S)=组内超时数/组合总样本数×100（百分点份额，含新增/移除小组）',
            'shapley_order_count': len(order_rows),
            'rounding': '贡献以百万分之一百分点为整数单元分配（contribution_micro），展示值再折算为 6 位小数；因素合计与小组合计都取同一批整数单元之和，严格等于全局变化。',
            'frozen': True,
        },
    }
    _verify_result(result)
    return result


def _frozen_meta(run):
    return {
        'id': run.id, 'name': run.name, 'checksum': run.checksum,
        'review': run.review, 'snapshot': run.snapshot,
    }


def _enqueue(attribution_id, revision, base_run_id, target_run_id):
    import redis
    redis.Redis.from_url(REDIS_URL).rpush('attribution-jobs', json.dumps({
        'id': attribution_id, 'revision': revision,
        'base_run_id': base_run_id, 'target_run_id': target_run_id,
    }))


def _reviewed(db, run_id):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, '报告不存在')
    if run.status != 'completed' or not run.approved:
        raise HTTPException(409, '归因只能选择已完成且已复核的报告')
    return run


def _apply_selection(db, user, row, base, target):
    same_selection = row.base_run_id == base.id and row.target_run_id == target.id
    if same_selection:
        if row.status == 'failed':
            row.revision += 1
            row.status = 'queued'
            row.error = None
            row.updated = time.time()
            log(db, user.email, 'attribution.retry', f'{row.id} 修订{row.revision}')
            db.commit()
            _enqueue(row.id, row.revision, row.base_run_id, row.target_run_id)
        return row
    row.base_run_id, row.target_run_id = base.id, target.id
    row.revision += 1
    row.status = 'queued'
    row.result = None
    row.error = None
    row.checksum = None
    row.updated = time.time()
    log(db, user.email, 'attribution.reselect', f'{row.id} 修订{row.revision} 基准{base.id}/目标{target.id}')
    db.commit()
    _enqueue(row.id, row.revision, row.base_run_id, row.target_run_id)
    return row


def open_review(db, user, body):
    base = _reviewed(db, body.base_run_id)
    target = _reviewed(db, body.target_run_id)
    if base.id == target.id:
        raise HTTPException(422, '基准报告与目标报告不能相同')
    row = db.query(Attribution).filter_by(name=body.name).with_for_update().populate_existing().first()
    if row:
        return _apply_selection(db, user, row, base, target)
    row = Attribution(
        name=body.name, base_run_id=base.id, target_run_id=target.id, status='queued', revision=1,
        actor=user.email, created=time.time(), updated=time.time(),
    )
    db.add(row)
    log(db, user.email, 'attribution.queue', body.name)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.query(Attribution).filter_by(name=body.name).with_for_update().populate_existing().first()
        if not row:
            raise
        return _apply_selection(db, user, row, base, target)
    _enqueue(row.id, row.revision, row.base_run_id, row.target_run_id)
    return row


def retry_review(db, user, attribution_id):
    row = db.query(Attribution).filter_by(id=attribution_id).with_for_update().populate_existing().first()
    if not row:
        raise HTTPException(404, '复盘不存在')
    if row.status == 'failed':
        row.revision += 1
        row.status = 'queued'
        row.error = None
        row.result = None
        row.checksum = None
        row.updated = time.time()
        log(db, user.email, 'attribution.retry', f'{row.id} 修订{row.revision}')
        db.commit()
        _enqueue(row.id, row.revision, row.base_run_id, row.target_run_id)
    # 已完成 / 排队中 / 计算中：重试不新增结果，直接返回原复盘
    return row


def claim(db, attribution_id, revision, base_run_id, target_run_id):
    row = db.query(Attribution).filter_by(id=attribution_id).with_for_update().populate_existing().first()
    if not row:
        return None
    if row.revision != revision or row.base_run_id != base_run_id or row.target_run_id != target_run_id:
        return None
    if row.status != 'queued':
        return None
    row.status = 'running'
    row.updated = time.time()
    db.commit()
    return row


def finish(db, attribution_id, revision, base_run_id, target_run_id, result=None, error=None):
    row = db.query(Attribution).filter_by(id=attribution_id).with_for_update().populate_existing().first()
    if not row:
        db.commit()
        return {'ok': False, 'reason': 'missing'}
    stale = (
        row.revision != revision
        or row.base_run_id != base_run_id
        or row.target_run_id != target_run_id
        or row.status != 'running'
    )
    if stale:
        log(db, 'worker', 'attribution.discard', f'{attribution_id} 修订{revision} 迟到，当前修订{row.revision}，未覆盖新选择')
        db.commit()
        return {'ok': False, 'reason': 'stale'}
    if error:
        row.status = 'failed'
        row.error = str(error)
        row.result = None
        row.checksum = None
        log(db, 'worker', 'attribution.failed', f'{attribution_id} 修订{revision}')
    else:
        try:
            if not result:
                raise ValueError('归因结果为空，拒绝发布')
            _verify_result(result)
        except ValueError as exc:
            row.status = 'failed'
            row.error = str(exc)
            row.result = None
            row.checksum = None
            log(db, 'worker', 'attribution.rejected', f'{attribution_id} 修订{revision}')
            row.updated = time.time()
            db.commit()
            return {'ok': True, 'reason': 'failed'}
        row.status = 'completed'
        row.error = None
        row.result = result
        payload = {
            'base_run_id': base_run_id, 'target_run_id': target_run_id,
            'base_checksum': result['basis']['base_checksum'],
            'target_checksum': result['basis']['target_checksum'],
            'result': result,
        }
        row.checksum = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        log(db, 'worker', 'attribution.complete', f'{attribution_id} 修订{revision}')
    row.updated = time.time()
    db.commit()
    return {'ok': True, 'reason': 'published' if not error else 'failed'}


def compute_for_job(db, job):
    """在 worker 内按当前选择读取冻结快照并计算；不写结果。"""
    base = db.get(Run, job['base_run_id'])
    target = db.get(Run, job['target_run_id'])
    if not base or not target:
        return None, '归因所选报告不存在'
    if base.status != 'completed' or not base.approved or target.status != 'completed' or not target.approved:
        return None, '归因所选报告未完成或未复核'
    try:
        result = attribute(_frozen_meta(base), _frozen_meta(target))
        return result, None
    except Exception as exc:
        return None, str(exc)


def run_job(db, job):
    claimed = claim(db, job['id'], job['revision'], job['base_run_id'], job['target_run_id'])
    if not claimed:
        return {'ok': False, 'reason': 'not_claimable'}
    result, error = compute_for_job(db, job)
    return finish(db, job['id'], job['revision'], job['base_run_id'], job['target_run_id'], result=result, error=error)


def listing(db):
    return [serialize_row(row) for row in db.query(Attribution).order_by(Attribution.id.desc()).all()]
