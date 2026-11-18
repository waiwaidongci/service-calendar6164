import copy
import json

import pytest
from fastapi import HTTPException

from src.config.database import Base, engine, Session
from src.models.attribution import Attribution
from src.models.identity import User
from src.models.run import Run
from src.services import attribution as svc
from src.services.validation import validate_policy

POLICY = {
    'version': '工作时钟-v1', 'timezone': 'Asia/Shanghai', 'weekdays': [0, 1, 2, 3, 4],
    'windows': [[540, 720], [780, 1080]], 'holidays': ['2026-11-16'],
    'sla_minutes': {'普通': 480, '紧急': 120},
}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine.dispose()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(svc, '_enqueue', lambda *a, **k: None)
    with Session() as session:
        yield session
    engine.dispose()


def src(dept, priority='普通', day=3, opened='10:30', closed='18:30'):
    return {'period': '2026-11', 'department': dept,
            'opened': f'2026-11-{day:02}T{opened}:00+08:00',
            'closed': f'2026-11-{day:02}T{closed}:00+08:00', 'priority': priority}


def make_run(db, name, sources, policy, approved=True):
    run = Run(name=name, status='completed', snapshot={'policy': policy, 'sources': sources, 'period': '2026-11'},
              checksum='ck-' + name, result={'rows': []}, approved=approved, review='核对完成')
    db.add(run)
    db.commit()
    return run


def policy(version='v2', **changes):
    value = copy.deepcopy(POLICY)
    value.update(changes)
    value['version'] = version
    return value


class Body:
    def __init__(self, name, base, target):
        self.name, self.base_run_id, self.target_run_id = name, base.id, target.id


class Select:
    def __init__(self, base, target):
        self.base_run_id, self.target_run_id = base.id, target.id


def test_requires_approved_reports(db):
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    base = make_run(db, 'a', [src('甲组')], POLICY, approved=False)
    target = make_run(db, 'b', [src('甲组')], POLICY)
    with pytest.raises(HTTPException) as exc:
        svc.open_review(db, user, Body('复盘', base, target))
    assert exc.value.status_code == 409


def test_publishes_only_after_completeness(db):
    user = User(email='analyst@demo.local', role='analyst', password='x')
    db.add(user)
    base = make_run(db, 'a', [src('甲组'), src('乙组', '紧急')], POLICY)
    target = make_run(db, 'b', [src('甲组', day=5), src('丙组')], policy(weekdays=[0, 1, 2, 3, 4, 5],
                                                                        sla_minutes={'普通': 300, '紧急': 60}))
    review = svc.open_review(db, user, Body('十一月归因', base, target))
    job = {'id': review.id, 'revision': review.revision, 'base_run_id': base.id, 'target_run_id': target.id}
    outcome = svc.run_job(db, job)
    assert outcome['ok'] and outcome['reason'] == 'published'
    row = db.get(Attribution, review.id)
    assert row.status == 'completed' and row.result and row.checksum
    result = row.result
    assert len(result['coalitions']) == 8 and len(result['orders']) == 6
    assert result['checks']['factor_sum_pp'] == result['change_pp']
    assert result['checks']['group_sum_pp'] == result['change_pp']
    for coalition in result['coalitions']:
        assert sum(g['count'] for g in coalition['groups']) == coalition['count']
        assert sum(g['overdue'] for g in coalition['groups']) == coalition['overdue']
    assert result['basis']['base_run_id'] == base.id and result['basis']['target_run_id'] == target.id


def test_missing_threshold_rejects_and_publishes_nothing(db):
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    base = make_run(db, 'a', [src('甲组')], POLICY)
    bad = policy(sla_minutes={'普通': 480})
    validate_policy(bad)  # 口径本身合法，缺的只是底稿里出现的优先级
    target = make_run(db, 'b', [src('甲组'), src('乙组', '紧急')], bad)
    review = svc.open_review(db, user, Body('缺阈值', base, target))
    outcome = svc.run_job(db, {'id': review.id, 'revision': review.revision,
                               'base_run_id': base.id, 'target_run_id': target.id})
    row = db.get(Attribution, review.id)
    assert row.status == 'failed'
    assert row.result is None and row.checksum is None
    assert '整次拒绝' in row.error and '紧急' in row.error
    assert outcome['reason'] == 'failed'


def test_retry_does_not_add_results_or_duplicate(db):
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    base = make_run(db, 'a', [src('甲组')], POLICY)
    target = make_run(db, 'b', [src('乙组')], POLICY)
    review = svc.open_review(db, user, Body('重试', base, target))
    svc.run_job(db, {'id': review.id, 'revision': review.revision, 'base_run_id': base.id, 'target_run_id': target.id})
    first = db.get(Attribution, review.id)
    first_checksum, first_revision, first_updated = first.checksum, first.revision, first.updated
    returned = svc.retry_review(db, user, review.id)
    again = db.get(Attribution, review.id)
    assert returned.id == review.id
    assert again.checksum == first_checksum and again.revision == first_revision
    assert again.updated == first_updated
    assert db.query(Attribution).count() == 1


def test_retry_after_failure_reuses_same_row(db):
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    base = make_run(db, 'a', [src('甲组')], POLICY)
    target = make_run(db, 'b', [src('乙组')], POLICY)
    review = svc.open_review(db, user, Body('重试失败', base, target))
    svc.claim(db, review.id, review.revision, base.id, target.id)
    svc.finish(db, review.id, review.revision, base.id, target.id, error='临时错误')
    failed = db.get(Attribution, review.id)
    assert failed.status == 'failed'
    revision = failed.revision
    svc.retry_review(db, user, review.id)
    queued = db.get(Attribution, review.id)
    assert queued.status == 'queued' and queued.revision == revision + 1
    assert db.query(Attribution).count() == 1


def test_late_old_job_cannot_overwrite_new_selection(db):
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    base = make_run(db, 'a', [src('甲组')], POLICY)
    mid = make_run(db, 'b', [src('乙组')], POLICY)
    new = make_run(db, 'c', [src('丙组')], POLICY)
    review = svc.open_review(db, user, Body('并发换报告', base, mid))
    old_job = {'id': review.id, 'revision': review.revision, 'base_run_id': base.id, 'target_run_id': mid.id}
    svc.claim(db, old_job['id'], old_job['revision'], old_job['base_run_id'], old_job['target_run_id'])

    svc.open_review(db, user, Body('并发换报告', base, new))  # 并发换报告，修订+1
    row = db.get(Attribution, review.id)
    assert row.revision == old_job['revision'] + 1
    assert row.target_run_id == new.id and row.status == 'queued'

    # 旧任务迟到，带着旧目标与旧修订提交结果
    stale_result = {'fake': True}
    outcome = svc.finish(db, review.id, old_job['revision'], base.id, mid.id, result=stale_result)
    row = db.get(Attribution, review.id)
    assert outcome['ok'] is False and outcome['reason'] == 'stale'
    assert row.status == 'queued' and row.result is None and row.target_run_id == new.id

    # 旧任务甚至不能重新领取
    assert svc.claim(db, review.id, old_job['revision'], base.id, mid.id) is None

    # 新任务正常完成并发布
    new_job = {'id': review.id, 'revision': row.revision, 'base_run_id': base.id, 'target_run_id': new.id}
    assert svc.run_job(db, new_job)['ok'] is True
    row = db.get(Attribution, review.id)
    assert row.status == 'completed' and row.result['basis']['target_run_id'] == new.id


def test_same_selection_resubmit_keeps_existing_result(db):
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    base = make_run(db, 'a', [src('甲组')], POLICY)
    target = make_run(db, 'b', [src('乙组')], policy())
    review = svc.open_review(db, user, Body('重复提交', base, target))
    svc.run_job(db, {'id': review.id, 'revision': review.revision, 'base_run_id': base.id, 'target_run_id': target.id})
    checksum = db.get(Attribution, review.id).checksum
    again = svc.open_review(db, user, Body('重复提交', base, target))
    assert again.id == review.id
    assert db.get(Attribution, review.id).checksum == checksum
    assert db.query(Attribution).count() == 1


def test_worker_claim_is_single_use(db):
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    base = make_run(db, 'a', [src('甲组')], POLICY)
    target = make_run(db, 'b', [src('乙组')], POLICY)
    review = svc.open_review(db, user, Body('单次领取', base, target))
    job = {'id': review.id, 'revision': review.revision, 'base_run_id': base.id, 'target_run_id': target.id}
    assert svc.claim(db, job['id'], job['revision'], job['base_run_id'], job['target_run_id']) is not None
    assert svc.claim(db, job['id'], job['revision'], job['base_run_id'], job['target_run_id']) is None
    assert svc.run_job(db, job)['ok'] is False


def test_tampered_result_is_rejected_not_published(db):
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    base = make_run(db, 'a', [src('甲组')], POLICY)
    target = make_run(db, 'b', [src('乙组')], policy())
    review = svc.open_review(db, user, Body('被篡改', base, target))
    svc.claim(db, review.id, review.revision, base.id, target.id)
    tampered = {'change_micro': 5, 'coalitions': [], 'groups': [], 'factors': [], 'orders': []}
    outcome = svc.finish(db, review.id, review.revision, base.id, target.id, result=tampered)
    row = db.get(Attribution, review.id)
    assert outcome['reason'] == 'failed'
    assert row.status == 'failed' and row.result is None and row.checksum is None
    assert '拒绝发布' in row.error


def test_failed_retry_recomputes_and_completes(db):
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    base = make_run(db, 'a', [src('甲组')], POLICY)
    target = make_run(db, 'b', [src('乙组')], policy())
    review = svc.open_review(db, user, Body('失败后重试', base, target))
    svc.claim(db, review.id, review.revision, base.id, target.id)
    svc.finish(db, review.id, review.revision, base.id, target.id, error='临时错误')
    svc.retry_review(db, user, review.id)
    row = db.get(Attribution, review.id)
    job = {'id': row.id, 'revision': row.revision, 'base_run_id': base.id, 'target_run_id': target.id}
    assert svc.run_job(db, job)['reason'] == 'published'
    final = db.get(Attribution, review.id)
    assert final.status == 'completed' and final.result and db.query(Attribution).count() == 1
