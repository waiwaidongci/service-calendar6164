import copy
import itertools
import random
from fractions import Fraction

import pytest

from src.services import attribution as svc
from src.services.calculation import calculate

POLICY = {
    'version': '工作时钟-v1', 'timezone': 'Asia/Shanghai', 'weekdays': [0, 1, 2, 3, 4],
    'windows': [[540, 720], [780, 1080]], 'holidays': ['2026-11-16'],
    'sla_minutes': {'普通': 480, '紧急': 120},
}


def source(dept, priority='普通', day=3, opened='10:30', closed='18:30'):
    return {'period': '2026-11', 'department': dept,
            'opened': f'2026-11-{day:02}T{opened}:00+08:00',
            'closed': f'2026-11-{day:02}T{closed}:00+08:00', 'priority': priority}


def run(rid, sources, policy, name=None):
    return {'id': rid, 'name': name or f'run-{rid}', 'checksum': f'ck-{rid}', 'review': '已核对',
            'snapshot': {'policy': policy, 'sources': sources, 'period': '2026-11'}}


def test_shapley_efficiency_and_orders():
    base = run(1, [source('甲组'), source('乙组'), source('甲组', '紧急')], POLICY)
    target_policy = copy.deepcopy(POLICY)
    target_policy['version'] = '工作时钟-v2'
    target_policy['windows'] = [[540, 720], [780, 1140]]
    target_policy['holidays'] = []
    target_policy['sla_minutes'] = {'普通': 300, '紧急': 60}
    target = run(2, [source('甲组'), source('丙组'), source('乙组', '紧急', day=5)], target_policy)

    result = svc.attribute(base, target)

    assert result['checks']['passed']
    assert len(result['coalitions']) == 8
    assert len(result['orders']) == 6
    assert [set(o['order']) for o in result['orders']] == [set(svc.FACTOR_KEYS)] * 6
    assert result['checks']['factor_sum_pp'] == result['change_pp']
    assert result['checks']['group_sum_pp'] == result['change_pp']
    assert sum(f['contribution_pp'] for f in result['factors']) == pytest.approx(result['change_pp'], abs=1e-9)
    assert sum(g['contribution_pp'] for g in result['groups']) == pytest.approx(result['change_pp'], abs=1e-9)


def test_endpoints_match_real_calculation():
    base = run(1, [source('甲组'), source('乙组'), source('甲组', '紧急')], POLICY)
    target_policy = copy.deepcopy(POLICY)
    target_policy['version'] = '工作时钟-v2'
    target_policy['sla_minutes'] = {'普通': 300, '紧急': 30}
    target = run(2, [source('甲组'), source('丙组'), source('乙组', '紧急', day=5)], target_policy)

    result = svc.attribute(base, target)
    base_calc = calculate(base['snapshot']['sources'], base['snapshot']['policy'])
    target_calc = calculate(target['snapshot']['sources'], target_policy)
    assert result['base']['overdue_count'] == base_calc['summary']['超时数']
    assert result['target']['overdue_count'] == target_calc['summary']['超时数']
    assert result['base']['sample_count'] == base_calc['summary']['样本数']
    assert result['target']['sample_count'] == target_calc['summary']['样本数']


def test_group_contributions_include_added_and_removed_groups():
    base = run(1, [source('甲组'), source('甲组'), source('乙组')], POLICY)
    target = run(2, [source('甲组'), source('丙组'), source('丙组')], POLICY)
    result = svc.attribute(base, target)
    groups = {g['group']: g for g in result['groups']}
    assert groups['乙组']['status'] == 'removed'
    assert groups['丙组']['status'] == 'added'
    assert groups['乙组']['target']['count'] == 0
    assert groups['丙组']['base']['count'] == 0
    assert result['checks']['group_sum_pp'] == result['change_pp']


def test_group_game_matches_definition():
    """小组贡献 = 各组合 组内超时数/总样本数 的 Shapley 平均值，单位百分点。"""
    base = run(1, [source('甲组'), source('甲组', '紧急'), source('乙组')], POLICY)
    target_policy = copy.deepcopy(POLICY)
    target_policy['version'] = 'v2'
    target_policy['weekdays'] = [0, 1, 2, 3, 4, 5]
    target = run(2, [source('甲组', day=5), source('乙组'), source('丙组')], target_policy)
    result = svc.attribute(base, target)

    coalitions = result['coalitions']
    for group in result['groups']:
        by_key = {c['key']: next((g for g in c['groups'] if g['group'] == group['group']), {'overdue': 0}) for c in coalitions}
        expected = Fraction(0)
        for order in itertools.permutations(svc.FACTOR_KEYS):
            prev = '000'
            chosen = set()
            for key in order:
                chosen.add(key)
                cur = ''.join('1' if k in chosen else '0' for k in svc.FACTOR_KEYS)
                stat_cur = next(c for c in coalitions if c['key'] == cur)
                stat_prev = next(c for c in coalitions if c['key'] == prev)
                cur_v = Fraction(by_key[cur]['overdue'], stat_cur['count']) * 100
                prev_v = Fraction(by_key[prev]['overdue'], stat_prev['count']) * 100
                expected += cur_v - prev_v
                prev = cur
        expected /= 6
        assert group['contribution_pp'] == pytest.approx(float(expected), abs=1e-6)


def test_missing_threshold_rejects_whole_attribution():
    bad_policy = copy.deepcopy(POLICY)
    bad_policy['version'] = 'v2'
    bad_policy['sla_minutes'] = {'普通': 480}
    base = run(1, [source('甲组')], POLICY)
    target = run(2, [source('甲组'), source('甲组', '紧急')], bad_policy)
    with pytest.raises(ValueError) as exc:
        svc.attribute(base, target)
    message = str(exc.value)
    assert '整次拒绝' in message
    assert '紧急' in message
    assert 'SLA' in message


def test_missing_threshold_only_in_calendar_mix_rejects():
    """阈值本身没变，但替换营业时段后出现新优先级时会缺阈值。"""
    base = run(1, [source('甲组')], POLICY)
    target_policy = copy.deepcopy(POLICY)
    target_policy['version'] = 'v2'
    target = run(2, [source('甲组'), source('乙组', '加急')], target_policy)
    with pytest.raises(ValueError):
        svc.attribute(base, target)


def test_zero_change_when_snapshots_identical():
    data = [source('甲组'), source('乙组', '紧急')]
    base = run(1, list(map(dict, data)), POLICY)
    target = run(2, list(map(dict, data)), POLICY)
    result = svc.attribute(base, target)
    assert result['change_pp'] == 0
    assert all(f['contribution_pp'] == 0 for f in result['factors'])
    assert all(g['contribution_pp'] == 0 for g in result['groups'])
    assert result['warnings']


def test_randomized_efficiency_and_integrity():
    rng = random.Random(7)
    depts = ['甲组', '乙组', '丙组', '丁组']
    prios = ['普通', '紧急']
    for _ in range(25):
        def make():
            rows = [source(rng.choice(depts), rng.choice(prios), day=rng.randint(1, 30),
                           opened=f'{rng.randint(0, 23):02}:{rng.choice([0, 15, 30, 45]):02}',
                           closed=f'{rng.randint(0, 23):02}:{rng.choice([0, 15, 30, 45]):02}')
                    for _ in range(rng.randint(2, 9))]
            for row in rows:
                if row['closed'] < row['opened']:
                    row['closed'] = row['opened']
            return rows
        base_rows, target_rows = make(), make()
        policy_a = copy.deepcopy(POLICY)
        policy_b = copy.deepcopy(POLICY)
        policy_b['version'] = 'v2'
        policy_b['weekdays'] = rng.choice([[0, 1, 2, 3, 4], [0, 1, 2, 3, 4, 5, 6], [1, 3, 5]])
        policy_b['windows'] = rng.choice([[[540, 720], [780, 1080]], [[480, 1200]], [[540, 720], [780, 1080], [1140, 1200]]])
        policy_b['holidays'] = rng.choice([[], ['2026-11-16'], ['2026-11-05', '2026-11-20']])
        policy_b['sla_minutes'] = rng.choice([{'普通': 480, '紧急': 120}, {'普通': 300, '紧急': 60}, {'普通': 60, '紧急': 20}])
        result = svc.attribute(run(1, base_rows, policy_a), run(2, target_rows if rng.random() < .5 else make(), policy_b))
        assert result['checks']['factor_sum_pp'] == result['change_pp']
        assert result['checks']['group_sum_pp'] == result['change_pp']
        for coalition in result['coalitions']:
            assert sum(g['count'] for g in coalition['groups']) == coalition['count']
            assert sum(g['overdue'] for g in coalition['groups']) == coalition['overdue']


def test_each_coalition_sample_and_overdue_counts_published():
    base = run(1, [source('甲组'), source('乙组')], POLICY)
    target = run(2, [source('甲组', '紧急'), source('丙组')], POLICY)
    result = svc.attribute(base, target)
    for coalition in result['coalitions']:
        assert isinstance(coalition['count'], int) and isinstance(coalition['overdue'], int)
        assert {g['group'] for g in coalition['groups']} == {'甲组', '乙组', '丙组'}
        covered = set()
        for group in coalition['groups']:
            assert isinstance(group['count'], int) and isinstance(group['overdue'], int)
        covered.add(coalition['key'])
    assert {c['key'] for c in result['coalitions']} == set(svc_coalition_keys())


def svc_coalition_keys():
    return [''.join('1' if flag else '0' for flag in bits) for bits in itertools.product((0, 1), repeat=3)]


def test_basis_contains_frozen_evidence():
    base = run(7, [source('甲组')], POLICY)
    target = run(9, [source('乙组')], POLICY)
    result = svc.attribute(base, target)
    basis = result['basis']
    assert basis['base_run_id'] == 7 and basis['target_run_id'] == 9
    assert basis['base_checksum'] == 'ck-7' and basis['target_checksum'] == 'ck-9'
    assert basis['coalition_keys'] == sorted(basis['coalition_keys']) or len(basis['coalition_keys']) == 8
    assert basis['shapley_order_count'] == 6
    assert set(basis['factor_definition']) == set(svc.FACTOR_KEYS)


def test_empty_side_rejected_with_reason():
    base = run(1, [], POLICY)
    target = run(2, [source('甲组')], POLICY)
    with pytest.raises(ValueError) as exc:
        svc.attribute(base, target)
    assert '整次拒绝' in str(exc.value)
    assert '没有底稿样本' in str(exc.value)
