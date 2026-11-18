# 002 归因复盘表

新增 `sla_attributions`，用于保存月度超时率归因复盘：两端报告编号、状态、修订号、结果与校验摘要。

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `name` | 复盘名称，唯一；同名再次提交按幂等处理 |
| `base_run_id` / `target_run_id` | 基准/目标报告，必须是已完成且已复核报告 |
| `status` | queued/running/completed/failed，沿用既有状态枚举 |
| `revision` | 每次改选报告、失败重试递增；worker 按修订+选择比对，迟到旧任务不覆盖 |
| `result` | 归因结果 JSON（8 组合、6 顺序、因素与小组贡献、完整性校验、归因依据） |
| `error` | 失败缘由（缺阈值/校验不齐时为整次拒绝说明），成功时为 NULL |
| `checksum` | 结果摘要 SHA256，按“两端报告+结果”计算 |
| `actor` / `created` / `updated` | 发起人与时间戳 |

启动时 `Base.metadata.create_all` 会自动建表；已有数据库升级请先备份，然后：

```sql
CREATE TABLE sla_attributions (
  id SERIAL PRIMARY KEY,
  name VARCHAR(120) UNIQUE NOT NULL,
  base_run_id INTEGER NOT NULL,
  target_run_id INTEGER NOT NULL,
  status VARCHAR(20) NOT NULL,
  revision INTEGER DEFAULT 1,
  result JSON,
  error TEXT,
  checksum VARCHAR(64),
  actor VARCHAR(120),
  created DOUBLE PRECISION,
  updated DOUBLE PRECISION
);
```

不修改既有表；旧报告快照不受影响。
