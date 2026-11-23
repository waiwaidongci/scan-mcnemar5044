# 002 票量计划与运行版本

应用启动仍以 `Base.metadata.create_all` 补建新表；对已存在的 `runs` 表采用显式补列。

- 新增 `quota_plans` 表：财务为各扫描批次、各票种定票量的计划，`version` 唯一递增，
  `superseded` 标记是否已被更正，`quotas` 保存 `{batches, types}` 两套票量的快照。
- `runs` 新增 `plan_id`（外键到 `quota_plans`，可空）、`plan_version`（计划版本快照）。
  统计验收运行两者为空；票量凑批运行携带计划版本，确保中断续跑沿用原计划与配对数据。
- 新增运行状态 `superseded`：计划更正后，旧计划下排队中或计算中的运行被原子作废，
  结果与导出文件一律清空，验收页面与下载都不会再呈现旧计划结果。
- 已存在数据库的补列语句（PostgreSQL）：

```sql
ALTER TABLE runs ADD COLUMN IF NOT EXISTS plan_id INTEGER REFERENCES quota_plans(id);
ALTER TABLE runs ADD COLUMN IF NOT EXISTS plan_version INTEGER;
CREATE INDEX IF NOT EXISTS ix_runs_plan_id ON runs (plan_id);
```
