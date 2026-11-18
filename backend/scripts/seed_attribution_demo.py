"""可选的归因演示数据：登记一个调整后口径，并生成两份已复核报告。

幂等：按版本名/任务名存在即跳过，不覆盖用户修改。不影响已有报告快照。
用法：docker compose exec -T backend python -m scripts.seed_attribution_demo
"""

import copy
import hashlib
import json
import time

from src.config.database import Base, engine, Session
from src.config.settings import CONFIG
from src.models.identity import User
from src.models.run import Run
from src.models.source import Policy, Source
from src.services.calculation import calculate
from src.services.validation import validate_policy

BASE_NAME = '演示：十一月超时率基线'
TARGET_NAME = '演示：十一月超时率调整后'
TARGET_VERSION = CONFIG['policy']['version'] + '-演示调整'


def target_policy():
    policy = copy.deepcopy(CONFIG['policy'])
    policy['version'] = TARGET_VERSION
    policy['holidays'] = []
    policy['windows'] = [[540, 720], [780, 1140]]
    policy['sla_minutes'] = {'普通': 240, '紧急': 180}
    validate_policy(policy)
    return policy


def make_run(db, name, sources, policy, reviewer):
    if db.query(Run).filter_by(name=name).first():
        return
    snapshot = {'policy': policy, 'sources': sources, 'period': '全部'}
    checksum = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    result = calculate(sources, policy)
    db.add(Run(name=name, status='completed', snapshot=snapshot, checksum=checksum, result=result,
               created=time.time(), approved=True, review=f'{reviewer} 演示复核：已核对汇总与口径边界'))
    db.commit()


def target_rows(sources):
    """目标报告底稿：部分记录改由新小组承接、上调优先级并延长到下班后，演示小组增减与底稿因素。"""
    rows = [dict(item) for item in sources]
    for index, row in enumerate(rows):
        if index % 9 == 0:
            row['department'] = '数据治理组'
        if index % 7 == 3:
            row['priority'] = '紧急'
        if index % 5 == 0 and row['closed'].endswith('16:30:00+08:00'):
            row['closed'] = row['closed'].replace('16:30:00', '17:15:00')
    return rows


def seed():
    Base.metadata.create_all(engine)
    with Session() as db:
        if not db.query(Policy).filter_by(version=TARGET_VERSION).first():
            db.add(Policy(version=TARGET_VERSION, data=target_policy()))
            db.commit()
        policy = db.query(Policy).filter_by(version=TARGET_VERSION).first().data
        sources = [item.data for item in db.query(Source).order_by(Source.id).all()]
        if not sources:
            print(json.dumps({'error': '请先执行 scripts.seed_demo 生成底稿'}, ensure_ascii=False))
            return
        make_run(db, BASE_NAME, sources, CONFIG['policy'], 'admin@demo.local')
        make_run(db, TARGET_NAME, target_rows(sources), policy, 'admin@demo.local')
        print(json.dumps({'policy': TARGET_VERSION, 'runs': db.query(Run).count()}, ensure_ascii=False))


if __name__ == '__main__':
    seed()
