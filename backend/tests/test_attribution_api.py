import copy

import pytest
from fastapi.testclient import TestClient

from src.config.database import Base, engine, Session
from src.main import app
from src.models.attribution import Attribution
from src.models.identity import User
from src.models.run import Run
from src.services import attribution as svc
from src.utils.security import password_hash

POLICY = {
    'version': '工作时钟-v1', 'timezone': 'Asia/Shanghai', 'weekdays': [0, 1, 2, 3, 4],
    'windows': [[540, 720], [780, 1080]], 'holidays': ['2026-11-16'],
    'sla_minutes': {'普通': 480, '紧急': 120},
}


def src(dept, priority='普通', day=3, opened='10:30', closed='18:30'):
    return {'period': '2026-11', 'department': dept,
            'opened': f'2026-11-{day:02}T{opened}:00+08:00',
            'closed': f'2026-11-{day:02}T{closed}:00+08:00', 'priority': priority}


@pytest.fixture()
def client(monkeypatch):
    engine.dispose()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(svc, '_enqueue', lambda *a, **k: None)
    with Session() as db:
        for role in ('admin', 'viewer'):
            db.add(User(email=f'{role}@demo.local', role=role, password=password_hash('Demo2026!')))
        db.commit()
        base = Run(name='十月基线', status='completed', checksum='ck-a', approved=True, review='已核对基线',
                   snapshot={'policy': POLICY, 'sources': [src('甲组'), src('乙组', '紧急')], 'period': '2026-11'},
                   result={'rows': []})
        changed = copy.deepcopy(POLICY)
        changed['version'] = '工作时钟-v2'
        changed['sla_minutes'] = {'普通': 300, '紧急': 60}
        target = Run(name='十一月', status='completed', checksum='ck-b', approved=True, review='已核对目标',
                     snapshot={'policy': changed, 'sources': [src('甲组', day=5), src('丙组')], 'period': '2026-11'},
                     result={'rows': []})
        unapproved = Run(name='待复核', status='completed', checksum='ck-c', approved=False, review='',
                         snapshot={'policy': POLICY, 'sources': [src('甲组')], 'period': '2026-11'},
                         result={'rows': []})
        db.add_all([base, target, unapproved])
        db.commit()
        ids = {'base': base.id, 'target': target.id, 'unapproved': unapproved.id}
    with TestClient(app) as test_client:
        test_client.ids = ids
        yield test_client
    engine.dispose()


def login(client, email='admin@demo.local'):
    response = client.post('/auth/login', json={'email': email, 'password': 'Demo2026!'})
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.json()['token']}


def test_viewer_can_read_basis_but_not_create(client):
    viewer = login(client, 'viewer@demo.local')
    admin = login(client)
    ids = client.ids
    created = client.post('/attributions', headers=admin,
                          json={'name': '十一月归因', 'base_run_id': ids['base'], 'target_run_id': ids['target']})
    assert created.status_code == 200
    row = created.json()
    with Session() as db:
        svc.run_job(db, {'id': row['id'], 'revision': row['revision'],
                         'base_run_id': ids['base'], 'target_run_id': ids['target']})
    detail = client.get(f"/attributions/{row['id']}", headers=viewer)
    assert detail.status_code == 200
    body = detail.json()
    assert body['status'] == 'completed'
    assert body['result']['basis']['base_checksum'] == 'ck-a'
    assert body['result']['basis']['target_checksum'] == 'ck-b'
    assert body['result']['checks']['factor_sum_pp'] == body['result']['change_pp']
    listed = client.get('/attributions', headers=viewer)
    assert listed.status_code == 200 and len(listed.json()) == 1
    denied = client.post('/attributions', headers=viewer,
                         json={'name': '越权', 'base_run_id': ids['base'], 'target_run_id': ids['target']})
    assert denied.status_code == 403


def test_rejects_unreviewed_and_same_report(client):
    headers = login(client)
    ids = client.ids
    unreviewed = client.post('/attributions', headers=headers,
                             json={'name': '未复核', 'base_run_id': ids['base'], 'target_run_id': ids['unapproved']})
    assert unreviewed.status_code == 409
    same = client.post('/attributions', headers=headers,
                       json={'name': '同一份', 'base_run_id': ids['base'], 'target_run_id': ids['base']})
    assert same.status_code == 422
    assert client.get('/attributions', headers=headers).json() == []


def test_reselect_endpoint_bumps_revision(client):
    headers = login(client)
    ids = client.ids
    created = client.post('/attributions', headers=headers,
                          json={'name': '换报告', 'base_run_id': ids['base'], 'target_run_id': ids['target']})
    assert created.status_code == 200
    row = created.json()
    reselected = client.put(f"/attributions/{row['id']}/selection", headers=headers,
                            json={'base_run_id': ids['target'], 'target_run_id': ids['base']})
    assert reselected.status_code == 200
    body = reselected.json()
    assert body['revision'] == row['revision'] + 1
    assert body['base_run_id'] == ids['target'] and body['target_run_id'] == ids['base']
    assert body['status'] == 'queued'


def test_retry_endpoint_is_idempotent_on_completed(client):
    headers = login(client)
    ids = client.ids
    row = client.post('/attributions', headers=headers,
                      json={'name': '幂等重试', 'base_run_id': ids['base'], 'target_run_id': ids['target']}).json()
    with Session() as db:
        svc.run_job(db, {'id': row['id'], 'revision': row['revision'],
                         'base_run_id': ids['base'], 'target_run_id': ids['target']})
    before = client.get(f"/attributions/{row['id']}", headers=headers).json()
    after = client.post(f"/attributions/{row['id']}/retry", headers=headers).json()
    assert after['checksum'] == before['checksum']
    assert after['revision'] == before['revision']
    with Session() as db:
        assert db.query(Attribution).count() == 1


def test_missing_threshold_reports_reason_in_api(client):
    headers = login(client)
    ids = client.ids
    with Session() as db:
        bad_policy = copy.deepcopy(POLICY)
        bad_policy['version'] = '缺阈值-v3'
        bad_policy['sla_minutes'] = {'普通': 480}
        bad = Run(name='缺阈值', status='completed', checksum='ck-d', approved=True, review='已复核',
                  snapshot={'policy': bad_policy,
                            'sources': [src('甲组'), src('乙组', '紧急')], 'period': '2026-11'},
                  result={'rows': []})
        db.add(bad)
        db.commit()
        bad_id = bad.id
    row = client.post('/attributions', headers=headers,
                      json={'name': '缺阈值归档', 'base_run_id': ids['base'], 'target_run_id': bad_id}).json()
    with Session() as db:
        svc.run_job(db, {'id': row['id'], 'revision': row['revision'],
                         'base_run_id': ids['base'], 'target_run_id': bad_id})
    body = client.get(f"/attributions/{row['id']}", headers=headers).json()
    assert body['status'] == 'failed'
    assert body['result'] is None
    assert '整次拒绝' in body['error'] and '紧急' in body['error']
