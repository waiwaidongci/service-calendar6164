# 初始结构

SQLAlchemy启动创建以下业务表：service_periods, service_cases, business_calendars, sla_runs, sla_attributions, delivery_schedules, deliveries、users、audits。Source专属JSON字段由对应validation严格校验；Policy为不可覆盖的版本记录；Run保存source/policy冻结快照；Period仅用于已有期间选择器，不计入核心复杂度。归因复盘结构与升级脚本见`002_attribution.md`。已有数据库升级需先备份，后续字段演进应以独立迁移执行。
